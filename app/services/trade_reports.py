"""Read ForexConnect Combined Account Statement XML, including archived trades."""
import math
import logging
import socket
from time import monotonic
from datetime import datetime, timedelta
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import getproxies, proxy_bypass, urlopen
from xml.etree import ElementTree as ET

import pytz


NS = {"r": "http://re.fxcm.com/2008/2.0/format-xml"}
REPORT_TIMEZONE = pytz.timezone("America/New_York")
logger = logging.getLogger(__name__)


class StatementError(Exception):
    """Safe report diagnostics: never include a signed URL or raw SDK error."""
    def __init__(self, stage, cause, elapsed, using_proxy=False):
        reason = cause.reason if isinstance(cause, URLError) else cause
        token_timeout = stage == "url" and str(cause) == "Token not received"
        self.timed_out = token_timeout or isinstance(reason, (socket.timeout, TimeoutError))
        self.details = {
            "stage": stage, "error_type": type(cause).__name__,
            "reason_type": type(reason).__name__,
            "http_status": cause.code if isinstance(cause, HTTPError) else None,
            "elapsed_seconds": round(elapsed, 2), "using_proxy": using_proxy,
            "reason_code": "report_token_timeout" if token_timeout else None,
        }
        self.message = {
            "url": "福汇历史报表授权超时，请重试" if token_timeout else "获取福汇历史报表链接失败，请重试",
            "download": "福汇历史报表下载超时，请重试" if self.timed_out else "福汇历史报表下载失败，请重试",
            "parse": "福汇历史报表格式或账户校验失败，请重试",
        }[stage]
        super().__init__(self.message)


def report_number(value):
    if value is None or not value.strip():
        return None
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("Invalid statement number")
    return result


def report_timestamp(value):
    if not value:
        raise ValueError("Missing statement trade date")
    # CAS XML uses New York wall time, unlike the SDK's UTC table dates.
    # Localize each date separately so winter and summer offsets are correct.
    return REPORT_TIMEZONE.localize(datetime.strptime(value, "%Y-%m-%d %H:%M:%S")).timestamp()


def parse_statement(content, account_number):
    root = ET.fromstring(content)
    if root.tag != "{%s}report" % NS["r"]:
        raise ValueError("Invalid FXCM statement")
    account = root.find("./r:parameters/r:parameter[@name='account']", NS)
    if account is None or account.text != account_number:
        raise ValueError("Statement account mismatch")
    table = root.find("./r:data/r:table[@name='closed_trades']/r:body", NS)
    if table is None:
        raise ValueError("Statement has no closed trades table")
    currency = root.find("./r:data/r:constants/r:constant[@name='base_currency']", NS)
    items = []
    for row in table.findall("r:row", NS):
        fields = {cell.get("name"): cell.text for cell in row.findall("r:cell", NS)}
        # 2 is a trade; grouping/subtotal/total rows must not become trades.
        if fields.get("grouping_type") != "2":
            continue
        quantity = report_number(fields.get("quantity"))
        if not quantity or not quantity.is_integer() or not fields.get("ticket_id") or not fields.get("symbol"):
            raise ValueError("Invalid statement trade")
        commission = report_number(fields.get("comm"))
        items.append({
            "trade_id": fields["ticket_id"], "symbol": fields["symbol"],
            "side": "Buy" if quantity > 0 else "Sell", "amount": int(abs(quantity)),
            "open_rate": report_number(fields.get("open_rate")),
            "close_rate": report_number(fields.get("close_rate")),
            "open_time": report_timestamp(fields.get("open_date")),
            "close_time": report_timestamp(fields.get("close_date")),
            "stop": None, "limit": None,
            "gross_pl": report_number(fields.get("gross_pl")),
            # The statement expresses fees as signed cash flows; the table/API
            # expresses commission as a cost. Keep broker net P/L (including
            # dividends/adjustments), rather than recomputing it from fees.
            "commission": -commission if commission is not None else None,
            "rollover": report_number(fields.get("rollover")),
            "net_pl": report_number(fields.get("net_pl")),
            "open_order_id": fields.get("open_order_id"),
            "close_order_id": fields.get("close_order_id"),
            "custom_id": None,
        })
    return items, currency.text if currency is not None else None


def fetch_statement(fx, account_id, from_ts=None, to_ts=None, account_name=None):
    # Start before FXCM existed; the report service clamps this to the account
    # opening date. Pad bounded requests for the report's New York time zone;
    # the caller applies the exact UTC [from, to) filter after parsing.
    start = datetime(1990, 1, 1) if from_ts is None else datetime.utcfromtimestamp(from_ts) - timedelta(days=1)
    end = (datetime.utcnow() if to_ts is None else datetime.utcfromtimestamp(to_ts)) + timedelta(days=1)
    stage, using_proxy, started = "url", False, monotonic()
    try:
        url = fx.session.get_report_url(account_id, start, end, "xml", "enu")
        if not url:
            raise ValueError("No FXCM statement URL")
        stage, started = "download", monotonic()
        target = urlsplit(url)
        using_proxy = bool(getproxies().get(target.scheme)) and not proxy_bypass(target.hostname)
        # The URL includes a short-lived token. Do not log or return it to clients.
        with urlopen(url, timeout=45) as response:
            content = response.read()
        stage, started = "parse", monotonic()
        # CAS identifies the account by its visible account number/name, while
        # get_report_url takes ForexConnect's distinct internal account ID.
        return parse_statement(content, account_name or account_id)
    except Exception as exc:
        error = StatementError(stage, exc, monotonic() - started, using_proxy)
        logger.warning("FXCM statement failed: %s", error.details)
        raise error from exc

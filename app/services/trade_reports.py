"""Read ForexConnect Combined Account Statement XML, including archived trades."""
import math
from datetime import datetime, timedelta
from urllib.request import urlopen
from xml.etree import ElementTree as ET

import pytz


NS = {"r": "http://re.fxcm.com/2008/2.0/format-xml"}
REPORT_TIMEZONE = pytz.timezone("America/New_York")


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
    url = fx.session.get_report_url(account_id, start, end, "xml", "enu")
    if not url:
        raise ValueError("No FXCM statement URL")
    # The URL includes a short-lived token. Do not log or return it to clients.
    with urlopen(url, timeout=45) as response:
        # CAS identifies the account by its visible account number/name, while
        # get_report_url takes ForexConnect's distinct internal account ID.
        return parse_statement(response.read(), account_name or account_id)

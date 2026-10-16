"""Demo execution adapter. Broker tables, never chart candles, own account state."""
import hashlib
import json
import math
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock

from forexconnect import ForexConnect, fxcorepy
from forexconnect.common import Common

from app.services.fxcm_service import FXCMServiceError, fxcm_history_service
from app.services.realtime import realtime_enabled


def require_demo_trading():
    if not realtime_enabled() or os.getenv("FXCM_TRADING_ENABLED", "false").lower() != "true":
        raise FXCMServiceError(404, "本地福汇交易未启用")
    if fxcm_history_service.settings.connection.strip().lower() != "demo":
        raise FXCMServiceError(403, "此功能只允许 ForexConnect Demo 连接")


def number(row, name, default=None):
    value = getattr(row, name, default)
    try:
        value = float(value)
        return value if math.isfinite(value) else default
    except (TypeError, ValueError):
        return default


def timestamp(value):
    if not isinstance(value, datetime):
        return None
    return value.replace(tzinfo=timezone.utc).timestamp() if value.tzinfo is None else value.timestamp()


class CommandJournal:
    """Write intent before network I/O; never resubmit an ambiguous request."""
    def __init__(self, path=None):
        self.path = path or Path(__file__).resolve().parents[2] / ".local" / "trading.sqlite3"

    def connect(self):
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(str(self.path), timeout=10)
        db.execute("CREATE TABLE IF NOT EXISTS commands (id TEXT PRIMARY KEY, digest TEXT NOT NULL, payload TEXT NOT NULL, result TEXT NOT NULL)")
        return db

    def get(self, key):
        with self.connect() as db:
            row = db.execute("SELECT digest, payload, result FROM commands WHERE id=?", (key,)).fetchone()
        return (row[0], json.loads(row[1]), json.loads(row[2])) if row else None

    def create(self, payload):
        encoded = json.dumps(payload, sort_keys=True, allow_nan=False)
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        result = {"client_id": payload["client_id"], "status": "pending", "message": "等待福汇确认"}
        with self.connect() as db:
            try:
                db.execute("INSERT INTO commands VALUES (?, ?, ?, ?)",
                           (payload["client_id"], digest, encoded, json.dumps(result)))
            except sqlite3.IntegrityError:
                old = self.get(payload["client_id"])
                if old[0] != digest:
                    raise FXCMServiceError(409, "重复请求编号对应的指令不同")
                return False, old[2]
        return True, result

    def save(self, key, result):
        with self.connect() as db:
            db.execute("UPDATE commands SET result=? WHERE id=?", (json.dumps(result), key))

    def unresolved(self, account_id):
        with self.connect() as db:
            rows = db.execute("SELECT payload, result FROM commands").fetchall()
        return [json.loads(result) for payload, result in rows
                if json.loads(payload)["account_id"] == account_id and json.loads(result)["status"] in {"pending", "unknown"}]


class DemoTradingService:
    def __init__(self, service=fxcm_history_service, journal=None):
        self.service = service
        self.journal = journal or CommandJournal()
        self.command_lock = Lock()

    def account(self, fx, account_id=None):
        rows = list(fx.get_table(ForexConnect.ACCOUNTS))
        if account_id is None and len(rows) == 1:
            return rows[0]
        for row in rows:
            if row.account_id == account_id:
                return row
        raise FXCMServiceError(400, "请选择当前 Demo 登录下的有效账户")

    def offer(self, fx, symbol):
        row = self.service._find_offer(fx, symbol)
        if row is None:
            raise FXCMServiceError(404, "福汇没有此交易品种")
        return row

    def currency(self, fx):
        try:
            reader = fx.create_reader(fx.login_rules.system_properties_response)
            return reader.find_property("BASE_CRNCY") or None
        except Exception:
            return None

    def instrument(self, fx, account, offer):
        settings = fx.login_rules.trading_settings_provider
        base = settings.get_base_unit_size(offer.instrument, account)
        def fee(fn, *args):
            try:
                value = float(fn(*args))
                return value if math.isfinite(value) else None
            except Exception:
                return None
        commissions, rollover = fx.session.commissions_provider, fx.session.rollover_provider
        return {
            "symbol": offer.instrument, "bid": number(offer, "bid"), "ask": number(offer, "ask"),
            "quote_time": timestamp(offer.time), "digits": int(offer.digits), "point_size": number(offer, "point_size"),
            "pip_cost": number(offer, "pip_cost"), "base_unit_size": base,
            "min_quantity": settings.get_min_quantity(offer.instrument, account),
            "max_quantity": settings.get_max_quantity(offer.instrument, account),
            "stop_distance_points": settings.get_cond_dist_stop_for_trade(offer.instrument),
            "limit_distance_points": settings.get_cond_dist_limit_for_trade(offer.instrument),
            "trading_status": offer.trading_status,
            "can_open": fx.login_rules.permission_checker.can_create_market_open_order(offer.instrument) == fxcorepy.O2GPermissionStatus.PERMISSION_ENABLED,
            "can_close": fx.login_rules.permission_checker.can_create_market_close_order(offer.instrument) == fxcorepy.O2GPermissionStatus.PERMISSION_ENABLED,
            "rollover_buy": fee(rollover.get_rollover_buy, offer, account),
            "rollover_sell": fee(rollover.get_rollover_sell, offer, account),
            "commission_buy": fee(commissions.calc_open_commission, offer, account, base, "B", offer.ask),
            "commission_sell": fee(commissions.calc_open_commission, offer, account, base, "S", offer.bid),
        }

    def position(self, row, offers, closed=False):
        offer = offers.get(row.offer_id)
        gross = number(row, "gross_pl")
        commission, rollover = number(row, "commission"), number(row, "rollover_interest")
        net = None if None in (gross, commission, rollover) else gross + rollover - commission
        return {
            "trade_id": row.trade_id, "symbol": getattr(offer, "instrument", row.offer_id),
            "side": "Buy" if row.buy_sell == "B" else "Sell", "amount": int(row.amount),
            "open_rate": number(row, "open_rate"), "open_time": timestamp(row.open_time),
            "close_rate": number(row, "close_rate" if closed else "close"),
            "close_time": timestamp(getattr(row, "close_time", None)),
            "stop": number(row, "stop") or None, "limit": number(row, "limit") or None,
            "gross_pl": gross, "commission": commission, "rollover": rollover, "net_pl": net,
            "open_order_id": getattr(row, "open_order_id", None),
            "close_order_id": getattr(row, "close_order_id", None),
            "custom_id": getattr(row, "close_order_request_txt" if closed else "open_order_request_txt", None),
        }

    def snapshot(self, symbol, account_id=None):
        require_demo_trading()
        fx = self.service._ensure_connected()
        account = self.account(fx, account_id)
        offer = self.offer(fx, symbol)
        offers = {row.offer_id: row for row in fx.get_table(ForexConnect.OFFERS)}
        positions = [self.position(row, offers) for row in fx.get_table(ForexConnect.TRADES) if row.account_id == account.account_id]
        closed = [self.position(row, offers, True) for row in fx.get_table(ForexConnect.CLOSED_TRADES) if row.account_id == account.account_id]
        orders = [{"order_id": row.order_id, "trade_id": row.trade_id, "type": row.type,
                   "status": row.status, "rate": number(row, "rate"), "amount": int(row.amount),
                   "custom_id": getattr(row, "request_txt", None)}
                  for row in fx.get_table(ForexConnect.ORDERS) if row.account_id == account.account_id]
        return {"type": "account", "connection": "Demo", "server_time": timestamp(fx.session.server_time),
                "accounts": [{"id": row.account_id, "name": row.account_name} for row in fx.get_table(ForexConnect.ACCOUNTS)],
                "account": {"id": account.account_id, "currency": self.currency(fx),
                            **{key: number(account, key) for key in ["balance", "equity", "used_margin", "usable_margin", "gross_pl", "day_pl"]}},
                "instrument": self.instrument(fx, account, offer), "positions": positions,
                "closed_positions": closed, "orders": orders,
                "unresolved": self.journal.unresolved(account.account_id)}

    def history(self, account_id=None, from_ts=None, to_ts=None):
        """Refresh and return the broker's closed-trade history for an account.

        ``CLOSED_TRADES`` is an FXCM account table rather than application
        history. Refreshing it through ForexConnect makes this endpoint read
        the broker's current records directly and keeps the local command
        journal out of the displayed trade history.
        """
        require_demo_trading()
        fx = self.service._ensure_connected()
        account = self.account(fx, account_id)
        offers = {row.offer_id: row for row in fx.get_table(ForexConnect.OFFERS)}
        try:
            reader = Common.refresh_table_by_account(
                fx, ForexConnect.CLOSED_TRADES, account.account_id
            )
            rows = list(reader)
        except Exception as exc:
            raise FXCMServiceError(502, f"查询福汇交易记录失败：{exc}") from exc

        items = []
        for row in rows:
            if getattr(row, "account_id", None) != account.account_id:
                continue
            item = self.position(row, offers, closed=True)
            close_ts = item.get("close_time")
            if from_ts is not None and (close_ts is None or close_ts < from_ts):
                continue
            if to_ts is not None and (close_ts is None or close_ts >= to_ts):
                continue
            items.append(item)

        items.sort(key=lambda item: item.get("close_time") or 0, reverse=True)
        return {
            "type": "history",
            "account": {"id": account.account_id, "currency": self.currency(fx)},
            "server_time": timestamp(fx.session.server_time),
            "from": from_ts,
            "to": to_ts,
            "items": items,
        }

    def _trade(self, fx, account_id, trade_id):
        return next((row for row in fx.get_table(ForexConnect.TRADES)
                     if row.trade_id == trade_id and row.account_id == account_id), None)

    def prepare(self, fx, command):
        account = self.account(fx, command["account_id"])
        if account.account_kind not in {"32", "36"} or account.margin_call_flag != "N":
            raise FXCMServiceError(409, "账户当前不允许交易")
        action = command["action"]
        offer = self.offer(fx, command["symbol"])
        instrument = self.instrument(fx, account, offer)
        trade = None if action == "open" else self._trade(fx, account.account_id, command.get("trade_id"))
        if action != "open" and (trade is None or trade.offer_id != offer.offer_id):
            raise FXCMServiceError(409, "持仓已变化，请刷新账户后操作")
        if action == "open" and self.journal.unresolved(account.account_id):
            raise FXCMServiceError(409, "存在待确认指令，请先核对执行结果后再开仓")
        if action in {"open", "close"}:
            amount = command.get("amount")
            if not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0 or amount % instrument["base_unit_size"]:
                raise FXCMServiceError(400, "数量必须是该品种基础单位的正整数倍")
            if amount < instrument["min_quantity"] or amount > instrument["max_quantity"] or (trade is not None and amount > trade.amount):
                raise FXCMServiceError(400, "交易数量超出福汇允许范围或当前持仓")
            if not instrument["can_" + action]:
                raise FXCMServiceError(409, "该账户不支持此品种的逐笔开平仓")
        if action == "open":
            if command.get("side") not in {"Buy", "Sell"}:
                raise FXCMServiceError(400, "无效的交易方向")
            if offer.trading_status != "O" or time.time() - (instrument["quote_time"] or 0) > 30:
                raise FXCMServiceError(409, "市场未开放或报价过期，暂不可开仓")
            side = "B" if command["side"] == "Buy" else "S"
            args = dict(ACCOUNT_ID=account.account_id, SYMBOL=offer.instrument, BUY_SELL=side,
                        AMOUNT=amount, CUSTOM_ID=command["client_id"], TIME_IN_FORCE="FOK")
            for key, param in [("stop", "RATE_STOP"), ("limit", "RATE_LIMIT")]:
                if command.get(key) is not None:
                    self.validate_risk(command[key], key, side, offer, instrument)
                    args[param] = command[key]
            return fx.create_order_request(fxcorepy.Constants.Orders.TRUE_MARKET_OPEN, **args), {}
        side = "S" if trade.buy_sell == "B" else "B"
        if action == "close":
            return fx.create_order_request(fxcorepy.Constants.Orders.TRUE_MARKET_CLOSE,
                ACCOUNT_ID=account.account_id, OFFER_ID=offer.offer_id, TRADE_ID=trade.trade_id,
                BUY_SELL=side, AMOUNT=amount, CUSTOM_ID=command["client_id"], TIME_IN_FORCE="FOK"), {"before_amount": int(trade.amount)}
        kind, rate = command["kind"], command.get("rate")
        order_id = getattr(trade, "stop_order_id" if kind == "stop" else "limit_order_id", None)
        if rate is None:
            if not order_id:
                return None, {}
            return fx.create_request({fxcorepy.O2GRequestParamsEnum.COMMAND: fxcorepy.Constants.Commands.DELETE_ORDER,
                                      fxcorepy.O2GRequestParamsEnum.ACCOUNT_ID: account.account_id,
                                      fxcorepy.O2GRequestParamsEnum.ORDER_ID: order_id}), {}
        self.validate_risk(rate, kind, trade.buy_sell, offer, instrument)
        order_type = fxcorepy.Constants.Orders.STOP if kind == "stop" else fxcorepy.Constants.Orders.LIMIT
        args = dict(ACCOUNT_ID=account.account_id, RATE=rate, CUSTOM_ID=command["client_id"])
        if order_id:
            args["ORDER_ID"] = order_id
            command_type = fxcorepy.Constants.Commands.EDIT_ORDER
        else:
            args.update(OFFER_ID=offer.offer_id, TRADE_ID=trade.trade_id, BUY_SELL=side, AMOUNT=int(trade.amount))
            command_type = fxcorepy.Constants.Commands.CREATE_ORDER
        return fx.create_order_request(order_type, command=command_type, **args), {}

    @staticmethod
    def validate_risk(rate, kind, side, offer, instrument):
        if not isinstance(rate, (int, float)) or not math.isfinite(rate) or rate <= 0:
            raise FXCMServiceError(400, "止损/止盈价格必须为有效正数")
        mark = offer.bid if side == "B" else offer.ask
        distance = instrument[("stop" if kind == "stop" else "limit") + "_distance_points"] * offer.point_size
        direction = (1 if side == "B" else -1) * (1 if kind == "limit" else -1)
        if (rate - mark) * direction < max(distance, 10 ** -offer.digits) - 1e-10:
            raise FXCMServiceError(400, "止损/止盈方向或最小距离不符合福汇规则")
        if abs(rate - round(rate, offer.digits)) > 1e-9:
            raise FXCMServiceError(400, "价格精度超出该品种限制")

    def confirmed(self, fx, command, result):
        account_id = command["account_id"]
        if command["action"] == "open":
            for table, field in [(ForexConnect.TRADES, "open_order_request_txt"), (ForexConnect.CLOSED_TRADES, "open_order_request_txt")]:
                for row in fx.get_table(table):
                    if row.account_id == account_id and (getattr(row, field, None) == command["client_id"] or
                       (result.get("order_id") and row.open_order_id == result["order_id"])):
                        result["trade_id"] = row.trade_id
                        return True
        elif command["action"] == "close":
            for row in fx.get_table(ForexConnect.CLOSED_TRADES):
                if row.account_id == account_id and row.trade_id == command["trade_id"] and (
                    getattr(row, "close_order_request_txt", None) == command["client_id"] or
                    (result.get("order_id") and row.close_order_id == result["order_id"])):
                    return True
        else:
            trade = self._trade(fx, account_id, command["trade_id"])
            if trade is None:
                return False
            actual = number(trade, command["kind"]) or None
            target = command.get("rate")
            return actual == target or (actual is not None and target is not None and abs(actual - target) < 1e-9)
        return False

    def result(self, key):
        require_demo_trading()
        item = self.journal.get(key)
        if item is None:
            raise FXCMServiceError(404, "指令不存在")
        _, command, result = item
        if result["status"] in {"unknown", "pending"}:
            fx = self.service._ensure_connected()
            if self.confirmed(fx, command, result):
                result.update(status="confirmed", message="福汇已确认执行")
                self.journal.save(key, result)
        return result

    def execute(self, command):
        require_demo_trading()
        with self.command_lock:
            old = self.journal.get(command["client_id"])
            if old:
                if old[1] != command:
                    raise FXCMServiceError(409, "重复请求编号对应的指令不同")
                return self.result(command["client_id"])
            fx = self.service._ensure_connected()
            request, extra = self.prepare(fx, command)
            if request is None and not (command["action"] == "risk" and command.get("rate") is None):
                raise FXCMServiceError(400, "福汇无法创建此请求")
            created, result = self.journal.create(command)
            if not created:
                return result
            result.update(extra)
            try:
                if request is not None:
                    response = fx.send_request(request)
                    result["order_id"] = getattr(response, "order_id", None)
                deadline = time.monotonic() + 5
                while True:
                    if self.confirmed(fx, command, result):
                        result.update(status="confirmed", message="福汇已确认执行")
                        break
                    if time.monotonic() >= deadline:
                        result.update(status="pending", message="福汇已接收，等待账户确认；请勿重复下单")
                        break
                    time.sleep(0.1)
            except Exception as exc:
                # A timeout is NOT a rejection: it may have executed. Keep its
                # durable ID and reconcile via tables instead of retrying POST.
                rejected = type(exc).__name__ == "RequestFailedError" and "timeout" not in str(exc).lower()
                result.update(status="rejected" if rejected else "unknown",
                              message=("福汇拒绝此指令：" if rejected else "执行结果待核对：") + str(exc))
            self.journal.save(command["client_id"], result)
            return result


demo_trading = DemoTradingService()

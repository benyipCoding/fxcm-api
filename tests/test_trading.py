import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace as Row
from unittest.mock import patch
from uuid import uuid4

from forexconnect import ForexConnect, fxcorepy
from app.services.fxcm_service import FXCMServiceError
from app.services.trading import CommandJournal, DemoTradingService, require_demo_trading
from app.routers.trading import guard, Command


class TradingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.journal = CommandJournal(Path(self.temp.name) / "commands.sqlite3")
        self.account = Row(account_id="demo", account_kind="32", margin_call_flag="N")
        self.offer = Row(offer_id="1", instrument="EUR/USD", bid=1.1, ask=1.1002, digits=5, point_size=.0001, trading_status="O")
        self.trade = Row(trade_id="trade", account_id="demo", offer_id="1", buy_sell="B", amount=2000, stop_order_id="stop-id", limit_order_id="limit-id")
        self.requests = []
        self.fx = Row(get_table=lambda table: [self.account] if table == ForexConnect.ACCOUNTS else [self.trade] if table == ForexConnect.TRADES else [],
                      create_order_request=lambda order, **kwargs: self.requests.append((order, kwargs)) or kwargs,
                      create_request=lambda params: params)
        self.service = DemoTradingService(Row(_ensure_connected=lambda: self.fx, _find_offer=lambda fx, symbol: self.offer), self.journal)
        self.service.instrument = lambda *args: dict(base_unit_size=1000, min_quantity=1000, max_quantity=50000000,
            can_open=True, can_close=True, quote_time=10**12, stop_distance_points=0, limit_distance_points=0)
        self.command = dict(client_id=str(uuid4()), account_id="demo", symbol="EUR/USD", action="open", side="Buy", amount=1000)

    def test_production_and_live_are_blocked(self):
        with patch.dict(os.environ, APP_ENV="prod", FXCM_REALTIME_ENABLED="true", FXCM_TRADING_ENABLED="true"):
            with self.assertRaises(FXCMServiceError): require_demo_trading()
        with patch.dict(os.environ, APP_ENV="local", FXCM_REALTIME_ENABLED="true", FXCM_TRADING_ENABLED="true"), patch("app.services.trading.fxcm_history_service.settings", Row(connection="Real")):
            with self.assertRaises(FXCMServiceError): require_demo_trading()

    def test_bridge_key_and_schema(self):
        with patch("app.routers.trading.require_demo_trading"), patch.dict(os.environ, FXCM_TRADING_BRIDGE_KEY="secret"):
            with self.assertRaises(Exception): guard("wrong")
            guard("secret")
        for amount in [1.5, True, "1000"]:
            with self.assertRaises(ValueError): Command(**dict(self.command, amount=amount))

    def test_quantity_and_risk_validation_before_dispatch(self):
        for amount in [100, 1500, -1000, True]:
            with self.assertRaises(FXCMServiceError): self.service.prepare(self.fx, dict(self.command, amount=amount))
        for risk in [float("nan"), 0, 1.2, 1.000001]:
            with self.assertRaises(FXCMServiceError): self.service.prepare(self.fx, dict(self.command, stop=risk))
        self.assertEqual(self.requests, [])
        self.service.prepare(self.fx, dict(self.command, stop=1.09, limit=1.12))
        self.assertEqual(self.requests[-1][1]["RATE_STOP"], 1.09)
        self.assertEqual(self.requests[-1][1]["CUSTOM_ID"], self.command["client_id"])

    def test_close_targets_existing_trade_and_opposite_side(self):
        self.service.prepare(self.fx, dict(self.command, action="close", trade_id="trade"))
        order, payload = self.requests[-1]
        self.assertEqual(order, fxcorepy.Constants.Orders.TRUE_MARKET_CLOSE)
        self.assertEqual((payload["BUY_SELL"], payload["AMOUNT"], payload["TRADE_ID"]), ("S", 1000, "trade"))
        with self.assertRaises(FXCMServiceError): self.service.prepare(self.fx, dict(self.command, action="close", trade_id="another-account-trade"))

    def _with_orders(self, *orders):
        self.fx.get_table = lambda table: ([self.account] if table == ForexConnect.ACCOUNTS
                                           else [self.trade] if table == ForexConnect.TRADES
                                           else list(orders) if table == ForexConnect.ORDERS else [])

    def entry_row(self, **extra):
        return Row(**{**dict(order_id="E1", trade_id="T-E1", account_id="demo", offer_id="1", type="LE",
                             buy_sell="B", rate=1.09, amount=2000, stop_order_id="", limit_order_id=""), **extra})

    def test_entry_order_uses_server_side_entry_type_and_validates_brackets_from_its_rate(self):
        entry = dict(self.command, action="entry", rate=1.09)
        self.service.prepare(self.fx, dict(entry, stop=1.085, limit=1.1))
        order, payload = self.requests[-1]
        self.assertEqual(order, fxcorepy.Constants.Orders.ENTRY)
        self.assertEqual((payload["BUY_SELL"], payload["RATE"], payload["RATE_STOP"], payload["RATE_LIMIT"], payload["OFFER_ID"]),
                         ("B", 1.09, 1.085, 1.1, "1"))
        self.assertEqual(payload["CUSTOM_ID"], self.command["client_id"])
        # Brackets are measured from the pending rate, not the current market.
        for stop in [1.095, 1.09, 1.0850001]:
            with self.assertRaises(FXCMServiceError): self.service.prepare(self.fx, dict(entry, stop=stop))
        for rate in [0, -1, float("nan"), True, 1.090001]:
            with self.assertRaises(FXCMServiceError): self.service.prepare(self.fx, dict(entry, rate=rate))
        with self.assertRaises(FXCMServiceError): self.service.prepare(self.fx, dict(entry, amount=1500))

    def test_entry_edit_cancel_and_brackets_target_the_pending_order(self):
        stop = Row(order_id="S1", account_id="demo", offer_id="1", type="S", rate=1.08, buy_sell="S", amount=2000)
        self._with_orders(self.entry_row(stop_order_id="S1"), stop)
        base = dict(self.command, order_id="E1")
        self.service.prepare(self.fx, dict(base, action="entry_edit", rate=1.095))
        order, payload = self.requests[-1]
        self.assertEqual((order, payload["ORDER_ID"], payload["RATE"], payload["AMOUNT"]), ("LE", "E1", 1.095, 2000))
        # Crossing the market would turn a limit entry into a stop entry.
        with self.assertRaises(FXCMServiceError): self.service.prepare(self.fx, dict(base, action="entry_edit", rate=1.2))
        # The attached stop (1.08) must stay below the new buy rate.
        with self.assertRaises(FXCMServiceError): self.service.prepare(self.fx, dict(base, action="entry_edit", rate=1.079))
        self.service.prepare(self.fx, dict(base, action="entry_risk", kind="stop", rate=1.07))
        order, payload = self.requests[-1]
        self.assertEqual((order, payload["ORDER_ID"], payload["RATE"]), (fxcorepy.Constants.Orders.STOP, "S1", 1.07))
        self.service.prepare(self.fx, dict(base, action="entry_risk", kind="limit", rate=1.1))
        order, payload = self.requests[-1]
        self.assertEqual((order, payload["TRADE_ID"], payload["BUY_SELL"], payload["AMOUNT"]),
                         (fxcorepy.Constants.Orders.LIMIT, "T-E1", "S", 2000))
        removed = self.service.prepare(self.fx, dict(base, action="entry_risk", kind="stop", rate=None))[0]
        self.assertEqual(removed[fxcorepy.O2GRequestParamsEnum.ORDER_ID], "S1")
        self.assertEqual(self.service.prepare(self.fx, dict(base, action="entry_risk", kind="limit", rate=None)), (None, {}))
        cancel = self.service.prepare(self.fx, dict(base, action="entry_cancel"))[0]
        self.assertEqual(cancel[fxcorepy.O2GRequestParamsEnum.COMMAND], fxcorepy.Constants.Commands.DELETE_ORDER)
        self.assertEqual(cancel[fxcorepy.O2GRequestParamsEnum.ORDER_ID], "E1")
        for missing in ["nope", "S1"]:  # unknown, or not an entry order
            with self.assertRaises(FXCMServiceError):
                self.service.prepare(self.fx, dict(base, action="entry_cancel", order_id=missing))

    def test_entry_commands_confirm_from_the_orders_table(self):
        entry_cmd = dict(self.command, action="entry", rate=1.09)
        self._with_orders()
        self.assertFalse(self.service.confirmed(self.fx, entry_cmd, {}))
        self._with_orders(self.entry_row(request_txt=self.command["client_id"]))
        result = {}
        self.assertTrue(self.service.confirmed(self.fx, entry_cmd, result))
        self.assertEqual(result["order_id"], "E1")
        edit = dict(self.command, action="entry_edit", order_id="E1", rate=1.09)
        self.assertTrue(self.service.confirmed(self.fx, edit, {}))
        self.assertFalse(self.service.confirmed(self.fx, dict(edit, rate=1.08), {}))
        cancel = dict(self.command, action="entry_cancel", order_id="E1")
        self.assertFalse(self.service.confirmed(self.fx, cancel, {}))
        self._with_orders()
        self.assertTrue(self.service.confirmed(self.fx, cancel, {}))

    def test_snapshot_lists_pending_orders_with_their_brackets(self):
        stop = Row(order_id="S1", trade_id="T-E1", account_id="demo", offer_id="1", type="S", status="W", rate=1.08, buy_sell="S", amount=2000)
        rows = [self.entry_row(stop_order_id="S1", status="W"), stop]
        self.account.account_name = "Demo"
        self.fx.get_table = lambda table: ([self.account] if table == ForexConnect.ACCOUNTS else [self.offer]
                                           if table == ForexConnect.OFFERS else rows if table == ForexConnect.ORDERS else [])
        self.fx.session = Row(server_time=None)
        self.service.currency = lambda fx: "USD"
        with patch("app.services.trading.require_demo_trading"):
            orders = self.service.snapshot("EUR/USD")["orders"]
        entry = next(order for order in orders if order["order_id"] == "E1")
        self.assertEqual((entry["entry"], entry["side"], entry["symbol"], entry["stop"], entry["limit"]),
                         (True, "Buy", "EUR/USD", 1.08, None))
        self.assertFalse(next(order for order in orders if order["order_id"] == "S1")["entry"])

    def test_durable_idempotency_and_conflicting_payload(self):
        sent = []
        self.fx.send_request = lambda request: sent.append(request) or Row(order_id="order")
        self.service.confirmed = lambda *args: True
        with patch("app.services.trading.require_demo_trading"):
            first = self.service.execute(self.command)
            self.assertEqual(self.service.execute(self.command), first)
            # A new service instance retains the durable record after restart.
            other = DemoTradingService(self.service.service, CommandJournal(self.journal.path))
            self.assertEqual(other.execute(self.command), first)
            with self.assertRaises(FXCMServiceError): self.service.execute(dict(self.command, amount=2000))
        self.assertEqual(len(sent), 1)

    def test_timeout_is_unknown_and_never_resubmitted(self):
        sent = []
        def timeout(request):
            sent.append(request)
            raise RuntimeError("Wait timeout exceeded")
        self.fx.send_request = timeout
        self.service.confirmed = lambda *args: False
        with patch("app.services.trading.require_demo_trading"):
            self.assertEqual(self.service.execute(self.command)["status"], "unknown")
            self.assertEqual(self.service.execute(self.command)["status"], "unknown")
            with self.assertRaises(FXCMServiceError): self.service.execute(dict(self.command, client_id=str(uuid4())))
            self.service.confirmed = lambda *args: True
            self.assertEqual(self.service.result(self.command["client_id"])["status"], "confirmed")
        self.assertEqual(len(sent), 1)

    def test_broker_fees_and_pnl_are_authoritative(self):
        row = Row(trade_id="t", offer_id="1", buy_sell="B", amount=1000, open_rate=1.1, open_time=None,
                  gross_pl=10, commission=2, rollover_interest=-3, close=1.11)
        p = self.service.position(row, {"1": self.offer})
        self.assertEqual((p["gross_pl"], p["commission"], p["rollover"], p["net_pl"]), (10, 2, -3, 5))
        row.commission = None
        self.assertIsNone(self.service.position(row, {"1": self.offer})["net_pl"])

    def test_history_refreshes_fxcm_closed_trade_table_and_filters_dates(self):
        closed = [
            Row(
                trade_id="new",
                account_id="demo",
                offer_id="1",
                buy_sell="B",
                amount=1000,
                open_rate=1.1,
                open_time=datetime(2026, 10, 17, 1, tzinfo=timezone.utc),
                close_rate=1.2,
                close_time=datetime(2026, 10, 17, 2, tzinfo=timezone.utc),
                gross_pl=10,
                commission=0,
                rollover_interest=0,
            ),
            Row(
                trade_id="old",
                account_id="demo",
                offer_id="1",
                buy_sell="S",
                amount=1000,
                open_rate=1.2,
                open_time=datetime(2026, 10, 16, 1, tzinfo=timezone.utc),
                close_rate=1.1,
                close_time=datetime(2026, 10, 16, 2, tzinfo=timezone.utc),
                gross_pl=10,
                commission=0,
                rollover_interest=0,
            ),
        ]
        self.fx.session = Row(server_time=datetime(2026, 10, 17, 3, tzinfo=timezone.utc))
        self.fx.get_table = lambda table: (
            [self.account]
            if table == ForexConnect.ACCOUNTS
            else [self.offer]
            if table == ForexConnect.OFFERS
            else []
        )
        self.service.currency = lambda fx: "USD"
        with patch("app.services.trading.require_demo_trading"), patch(
            "app.services.trading.Common.refresh_table_by_account", return_value=closed
        ) as refresh:
            result = self.service.history("demo", 1792195200, 1792281600, scope="recent")

        refresh.assert_called_once_with(self.fx, ForexConnect.CLOSED_TRADES, "demo")
        self.assertEqual([item["trade_id"] for item in result["items"]], ["new"])
        self.assertEqual(result["account"], {"id": "demo", "currency": "USD"})


if __name__ == "__main__": unittest.main()

import unittest
from datetime import datetime, timezone
from types import SimpleNamespace as Row
from unittest.mock import Mock, patch
from xml.etree import ElementTree as ET

from forexconnect import ForexConnect
from app.services.fxcm_service import FXCMServiceError
from app.services.trade_reports import NS, fetch_statement, parse_statement, report_timestamp
from app.services.trading import DemoTradingService


def statement(rows, account="demo"):
    root = ET.Element("report", xmlns=NS["r"])
    parameters = ET.SubElement(root, "parameters")
    ET.SubElement(parameters, "parameter", name="account").text = account
    data = ET.SubElement(root, "data")
    constants = ET.SubElement(data, "constants")
    ET.SubElement(constants, "constant", name="base_currency").text = "USD"
    body = ET.SubElement(ET.SubElement(data, "table", name="closed_trades"), "body")
    for fields in rows:
        row = ET.SubElement(body, "row")
        for name, value in fields.items():
            ET.SubElement(row, "cell", name=name).text = str(value)
    return ET.tostring(root)


def trade(**overrides):
    return dict(dict(grouping_type="2", ticket_id="old", symbol="EUR/USD", quantity="-1000.00",
                     open_date="2026-10-13 10:00:00", close_date="2026-10-13 11:00:00",
                     open_rate="1.11", close_rate="1.10", gross_pl="10", comm="-2",
                     rollover="-1", net_pl="8", open_order_id="open", close_order_id="close"), **overrides)


class TradeReportTests(unittest.TestCase):
    def test_statement_preserves_partial_closes_symbols_signed_quantity_and_broker_net(self):
        rows, currency = parse_statement(statement([
            trade(), trade(close_order_id="partial", quantity="-2000.00"),
            trade(ticket_id="today", symbol="XAU/USD", quantity="10.00", close_date="2026-10-30 02:13:09"),
            {"grouping_type": "3", "net_pl": "24"},
        ]), "demo")
        self.assertEqual(len(rows), 3)
        self.assertEqual(currency, "USD")
        self.assertEqual([r["amount"] for r in rows], [1000, 2000, 10])
        self.assertEqual([r["side"] for r in rows], ["Sell", "Sell", "Buy"])
        self.assertEqual(rows[0]["commission"], 2)
        self.assertEqual(rows[0]["net_pl"], 8)  # Includes a broker adjustment.
        self.assertNotEqual(rows[0]["close_order_id"], rows[1]["close_order_id"])
        self.assertEqual(rows[2]["close_time"], datetime(2026, 10, 30, 6, 13, 9, tzinfo=timezone.utc).timestamp())

    def test_report_time_uses_historical_dst_offset(self):
        for month, utc_hour in [(1, 15), (7, 14)]:
            self.assertEqual(report_timestamp("2026-%02d-10 10:00:00" % month),
                             datetime(2026, month, 10, utc_hour, tzinfo=timezone.utc).timestamp())

    def test_empty_statement_is_distinct_from_invalid_or_wrong_account_response(self):
        self.assertEqual(parse_statement(statement([]), "demo"), ([], "USD"))
        for content in [b"<html>Report unavailable</html>", statement([], "another"),
                        statement([trade(quantity="nan")]), statement([trade(close_date="")])]:
            with self.assertRaises(ValueError): parse_statement(content, "demo")

    def test_fetch_requests_all_history_without_a_recent_days_limit(self):
        fx = Row(session=Row(get_report_url=Mock(return_value="https://broker.example/report?token=private")))
        response = Mock(read=Mock(return_value=statement([trade()], account="demo-number")))
        with patch("app.services.trade_reports.urlopen") as open_url:
            open_url.return_value.__enter__.return_value = response
            rows, currency = fetch_statement(fx, "demo", account_name="demo-number")
        args = fx.session.get_report_url.call_args[0]
        self.assertEqual((args[0], args[1], args[3:]), ("demo", datetime(1990, 1, 1), ("xml", "enu")))
        self.assertEqual(len(rows), 1)
        self.assertEqual(currency, "USD")

    def test_default_history_includes_archived_trades_and_exact_utc_range(self):
        account = Row(account_id="demo")
        fx = Row(get_table=lambda table: [account] if table == ForexConnect.ACCOUNTS else [],
                 session=Row(server_time=datetime(2026, 10, 30, 12, tzinfo=timezone.utc)))
        service = DemoTradingService(Row(_ensure_connected=lambda: fx))
        service.currency = lambda fx: None
        rows, _ = parse_statement(statement([
            trade(), trade(ticket_id="today", close_date="2026-10-30 02:13:09"),
        ]), "demo")
        with patch("app.services.trading.require_demo_trading"), \
                patch("app.services.trading.statement_client.fetch", return_value=(rows, "USD")), \
                patch("app.services.trading.Common.refresh_table_by_account") as recent:
            result = service.history("demo")
            self.assertEqual([r["trade_id"] for r in result["items"]], ["today", "old"])
            self.assertEqual(result["account"]["currency"], "USD")
            close_ts = rows[1]["close_time"]
            self.assertEqual([r["trade_id"] for r in service.history("demo", close_ts, close_ts + 1)["items"]], ["today"])
            self.assertEqual(service.history("demo", close_ts - 1, close_ts)["items"], [])
            recent.assert_not_called()

    def test_report_failure_never_returns_recent_trades_as_full_history_or_leaks_url(self):
        fx = Row(get_table=lambda table: [Row(account_id="demo")])
        service = DemoTradingService(Row(_ensure_connected=lambda: fx))
        with patch("app.services.trading.require_demo_trading"), \
                patch("app.services.trading.statement_client.fetch", side_effect=ValueError("https://broker/?token=private")), \
                patch("app.services.trading.Common.refresh_table_by_account") as recent:
            with self.assertRaises(FXCMServiceError) as error: service.history("demo")
        self.assertNotIn("private", error.exception.message)
        recent.assert_not_called()


if __name__ == "__main__":
    unittest.main()

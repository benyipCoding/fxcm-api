import json
import subprocess
import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace as Row
from unittest.mock import Mock, patch

from app.services.fxcm_service import FXCMServiceError
from app.services.trade_report_client import StatementClient, REPORT_TIMEOUT_SECONDS
from app.services.trade_report_worker import query
from app.services.trade_reports import StatementError


class ReportClientTests(unittest.TestCase):
    def test_report_executes_in_hidden_bounded_process_and_keeps_private_data_off_command_line(self):
        completed = Row(returncode=0, stdout=b'{"ok":true,"items":[{"trade_id":"old"}],"currency":"USD"}')
        with patch("app.services.trade_report_client.subprocess.run", return_value=completed) as run:
            items, currency = StatementClient().fetch("internal-id", account_name="account-number")
        args, options = run.call_args
        self.assertEqual(args[0][1:], ["-m", "app.services.trade_report_worker"])
        self.assertNotIn("internal-id", str(args))
        self.assertEqual(json.loads(options["input"])["account_name"], "account-number")
        self.assertEqual(options["timeout"], REPORT_TIMEOUT_SECONDS)
        self.assertLess(REPORT_TIMEOUT_SECONDS, 90)
        self.assertEqual((items, currency), ([{"trade_id": "old"}], "USD"))

    def test_timed_out_worker_and_invalid_output_return_safe_errors(self):
        cases = [subprocess.TimeoutExpired("worker", REPORT_TIMEOUT_SECONDS),
                 Row(returncode=1, stdout=b"bad output", stderr=b"token=secret"),
                 Row(returncode=0, stdout=b'{"ok":true}', stderr=b"")]
        for case in cases:
            with self.subTest(case=type(case).__name__), \
                    patch("app.services.trade_report_client.subprocess.run") as run:
                if isinstance(case, Exception): run.side_effect = case
                else: run.return_value = case
                with self.assertRaises(FXCMServiceError) as error: StatementClient().fetch("demo")
                self.assertNotIn("secret", str(error.exception))
                self.assertEqual(error.exception.payload["stage"], "worker")

    def test_worker_stage_error_is_preserved_by_client(self):
        error = {"status": 504, "message": "Report authorization timed out", "data": {"stage": "url", "reason_code": "report_token_timeout"}}
        completed = Row(returncode=0, stdout=json.dumps({"ok": False, "error": error}).encode())
        with patch("app.services.trade_report_client.subprocess.run", return_value=completed):
            with self.assertRaises(FXCMServiceError) as result: StatementClient().fetch("demo")
        self.assertEqual(result.exception.status_code, 504)
        self.assertEqual(result.exception.payload, error["data"])

    def test_duplicate_inflight_requests_share_worker_and_refresh_is_not_cached(self):
        client, entered, release, joined = StatementClient(), Event(), Event(), Event()
        def fetch(*args):
            entered.set()
            self.assertTrue(release.wait(3))
            return [], "USD"
        with patch.object(client, "_fetch", side_effect=fetch) as fetch_mock, ThreadPoolExecutor(2) as pool:
            first = pool.submit(client.fetch, "demo")
            self.assertTrue(entered.wait(3))
            future = client._pending[("demo", None, None, None)]
            original_result = future.result
            def wait_for_result():
                joined.set()
                return original_result()
            with patch.object(future, "result", side_effect=wait_for_result):
                second = pool.submit(client.fetch, "demo")
                try:
                    self.assertTrue(joined.wait(3))
                finally:
                    release.set()
                self.assertEqual(first.result(), second.result())
            self.assertEqual(fetch_mock.call_count, 1)
            client.fetch("demo")
            self.assertEqual(fetch_mock.call_count, 2)

    def test_worker_uses_its_own_session_without_quote_tables_and_logs_out(self):
        fx = Mock()
        settings = Row(username="user", password="password", url="hosts", connection="Demo", session_id=None, pin=None)
        with patch("app.services.trade_report_worker.get_settings", return_value=settings), \
                patch("app.services.trade_report_worker.ForexConnect", return_value=fx), \
                patch("app.services.trade_report_worker.fetch_statement", return_value=([{"trade_id": "old"}], "USD")):
            result = query({"account_id": "demo", "account_name": "number"})
        self.assertTrue(result["ok"])
        self.assertFalse(fx.login.call_args[1]["use_table_manager"])
        fx.get_table.assert_not_called()
        fx.logout.assert_called_once_with()

    def test_token_failure_has_actionable_stage_without_private_url(self):
        error = StatementError("url", AttributeError("Token not received"), 35)
        self.assertTrue(error.timed_out)
        self.assertEqual(error.details["reason_code"], "report_token_timeout")
        private = StatementError("url", ValueError("https://broker/?token=secret"), 1)
        self.assertNotIn("secret", json.dumps(private.details))
        self.assertNotIn("secret", private.message)


if __name__ == "__main__":
    unittest.main()

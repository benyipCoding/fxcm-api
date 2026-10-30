"""Isolate blocking ForexConnect report calls from live Python quote callbacks."""
import json
import logging
import os
import subprocess
import sys
from concurrent.futures import Future
from pathlib import Path
from threading import Lock

from app.services.fxcm_service import FXCMServiceError


logger = logging.getLogger(__name__)
REPORT_TIMEOUT_SECONDS = 70  # Must finish before the gateway's 90-second timeout.


class StatementClient:
    def __init__(self):
        self._lock = Lock()
        self._pending = {}

    def fetch(self, account_id, from_ts=None, to_ts=None, account_name=None):
        # React development effects and multiple viewers can request the same
        # report together. Share only in-flight work; explicit later refreshes
        # still fetch current broker data, without a stale-history fallback.
        key = (account_id, from_ts, to_ts, account_name)
        with self._lock:
            future = self._pending.get(key)
            owner = future is None
            if owner:
                future = self._pending[key] = Future()
        if not owner:
            return future.result()
        try:
            result = self._fetch(account_id, from_ts, to_ts, account_name)
            future.set_result(result)
            return result
        except Exception as exc:
            future.set_exception(exc)
            raise
        finally:
            with self._lock:
                self._pending.pop(key, None)

    def _fetch(self, account_id, from_ts, to_ts, account_name):
        payload = dict(account_id=account_id, account_name=account_name, from_ts=from_ts, to_ts=to_ts)
        try:
            # No credentials, signed URLs or account details in command-line
            # arguments. The child reads local configuration and this stdin.
            result = subprocess.run(
                [sys.executable, "-m", "app.services.trade_report_worker"],
                input=json.dumps(payload).encode("utf-8"),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                cwd=str(Path(__file__).resolve().parents[2]),
                timeout=REPORT_TIMEOUT_SECONDS,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        except subprocess.TimeoutExpired:
            # subprocess.run kills and reaps the timed-out child; no orphaned
            # report process can continue consuming a broker session.
            raise FXCMServiceError(504, "福汇历史报表查询超时，请重试", {"stage": "worker", "reason_code": "report_timeout"}) from None
        except OSError:
            raise FXCMServiceError(502, "无法启动福汇历史报表查询，请检查本地服务", {"stage": "worker"}) from None
        try:
            data = json.loads(result.stdout)
            if result.returncode != 0 or not isinstance(data, dict):
                raise ValueError("Report worker failed")
            if data.get("ok") is False:
                error = data["error"]
                logger.warning("FXCM isolated statement failed: %s", error["data"])
                raise FXCMServiceError(error["status"], error["message"], error["data"])
            if data.get("ok") is not True or not isinstance(data.get("items"), list):
                raise ValueError("Invalid report result")
            return data["items"], data.get("currency")
        except (ValueError, KeyError, TypeError):
            # Do not relay stderr: native SDK errors can contain private URLs.
            raise FXCMServiceError(502, "福汇历史报表查询进程异常，请重试", {"stage": "worker"}) from None


statement_client = StatementClient()

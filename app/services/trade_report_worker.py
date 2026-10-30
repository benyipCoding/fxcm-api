"""Read-only report worker. Never subscribe to quotes or send trading commands."""
import json
import sys

from forexconnect import ForexConnect

from app.core.config import get_settings
from app.services.trade_reports import StatementError, fetch_statement


def query(payload):
    settings = get_settings()
    if settings.connection.strip().lower() != "demo":
        return {"ok": False, "error": {"status": 403, "message": "历史报表仅允许 Demo 连接", "data": {"stage": "login"}}}
    fx = ForexConnect()
    try:
        # A separate process is intentional. A thread or a second session in
        # the live process still shares the GIL with native quote callbacks;
        # get_report_url can then wait 35s and fail with Token not received.
        fx.login(settings.username, settings.password, settings.url, settings.connection,
                 settings.session_id, settings.pin, use_table_manager=False)
        items, currency = fetch_statement(fx, **payload)
        return {"ok": True, "items": items, "currency": currency}
    except StatementError as exc:
        return {"ok": False, "error": {"status": 504 if exc.timed_out else 502,
                "message": exc.message, "data": exc.details}}
    except Exception as exc:
        return {"ok": False, "error": {"status": 502, "message": "福汇历史报表连接失败，请重试",
                "data": {"stage": "login", "error_type": type(exc).__name__}}}
    finally:
        if fx.session is not None:
            try:
                fx.logout()
            except Exception:
                pass


def main():
    try:
        result = query(json.load(sys.stdin))
    except Exception:
        result = {"ok": False, "error": {"status": 502, "message": "福汇历史报表查询进程异常，请重试", "data": {"stage": "worker"}}}
    # ASCII JSON avoids dependence on the Windows terminal's code page.
    print(json.dumps(result, ensure_ascii=True, allow_nan=False), flush=True)


if __name__ == "__main__":
    main()

import os
from pathlib import Path

from forexconnect import ForexConnect


def on_session_status_changed(session, status):
    print("session status:", status)


def load_dotenv(dotenv_path):
    if not dotenv_path.exists():
        return

    with dotenv_path.open(encoding="utf-8") as dotenv_file:
        for raw_line in dotenv_file:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue

            key, value = line.split("=", 1)
            os.environ[key.strip()] = value.strip()


def get_required_env(name):
    value = os.getenv(name)
    if value:
        return value
    raise RuntimeError(
        f"缺少环境变量 {name}。请在系统环境变量或 .env 文件中配置后重试。"
    )


def normalize_hosts_url(url):
    stripped_url = url.strip()
    if "fxcorporate.com" in stripped_url.lower():
        return "http://www.fxcorporate.com/Hosts.jsp"
    if stripped_url.lower().endswith("hosts.jsp"):
        return stripped_url
    return f"{stripped_url.rstrip('/')}/Hosts.jsp"


def get_history_or_raise(fx, instrument, timeframe, quotes_count):
    try:
        return fx.get_history(instrument, timeframe, None, None, quotes_count)
    except Exception as exc:
        error_text = str(exc)
        if "QuotesServerConnectionError" not in error_text:
            raise
        raise RuntimeError(
            "FXCM 登录已成功，但历史行情服务器当前不可达。"
            "脚本已使用官方示例推荐的 http://www.fxcorporate.com/Hosts.jsp。"
            "如果仍然失败，请检查当前网络是否拦截 FXCM 的历史行情服务，"
            "或切换网络后重试。"
        ) from exc


ENV_FILE = Path(__file__).with_name(".env")

load_dotenv(ENV_FILE)

USERNAME = get_required_env("USERNAME")
PASSWORD = get_required_env("PASSWORD")
URL = normalize_hosts_url(get_required_env("FXCM_URL"))
CONNECTION = "Demo"
SESSION_ID = None
PIN = None

with ForexConnect() as fx:
    fx.login(
        USERNAME,
        PASSWORD,
        URL,
        CONNECTION,
        SESSION_ID,
        PIN,
        on_session_status_changed,
    )

    history = get_history_or_raise(fx, "EUR/USD", "H1", 20)

    print("rows:", len(history))
    print("columns:", history.dtype.names)

    print("last 5 bars:")
    for row in history[-5:]:
        print(
            row["Date"],
            row["BidOpen"],
            row["BidHigh"],
            row["BidLow"],
            row["BidClose"],
            row["Volume"],
        )

    fx.logout()

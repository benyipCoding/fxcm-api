import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Optional


ROOT_DIR = Path(__file__).resolve().parents[2]
ENV_FILE = ROOT_DIR / ".env"


def load_dotenv(dotenv_path: Path = ENV_FILE) -> None:
    if not dotenv_path.exists():
        return

    with dotenv_path.open(encoding="utf-8") as dotenv_file:
        for raw_line in dotenv_file:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue

            key, value = line.split("=", 1)
            os.environ[key.strip()] = _strip_optional_quotes(value.strip())


def normalize_hosts_url(url: str) -> str:
    stripped_url = url.strip()
    lower_url = stripped_url.lower()

    if "fxcorporate.com" in lower_url:
        return "http://www.fxcorporate.com/Hosts.jsp"
    if lower_url.endswith("hosts.jsp"):
        return stripped_url
    return f"{stripped_url.rstrip('/')}/Hosts.jsp"


@dataclass(frozen=True)
class Settings:
    username: str
    password: str
    url: str
    connection: str
    session_id: Optional[str]
    pin: Optional[str]


@lru_cache()
def get_settings() -> Settings:
    load_dotenv()
    return Settings(
        username=_required_env("USERNAME"),
        password=_required_env("PASSWORD"),
        url=normalize_hosts_url(_required_env("FXCM_URL")),
        connection=os.getenv("FXCM_CONNECTION", "Demo"),
        session_id=_optional_env("FXCM_SESSION_ID"),
        pin=_optional_env("FXCM_PIN"),
    )


def _required_env(name: str) -> str:
    value = os.getenv(name)
    if value:
        return value
    raise RuntimeError(
        f"缺少环境变量 {name}。请在系统环境变量或 .env 文件中配置后重试。"
    )


def _optional_env(name: str) -> Optional[str]:
    value = os.getenv(name)
    return value or None


def _strip_optional_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value

"""Runtime configuration loaded from environment / .env file."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _env(name: str, default: str | None = None, required: bool = False) -> str:
    value = os.getenv(name, default)
    if required and not value:
        raise RuntimeError(f"Environment variable {name} is required but not set")
    return value or ""


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise RuntimeError(f"Environment variable {name} must be an integer, got {raw!r}")


@dataclass(frozen=True)
class Config:
    # Telegram
    telegram_bot_token: str
    telegram_owner_chat_id: int | None

    # DeepSeek / FreeDeepSeekAPI
    deepseek_api_base: str
    deepseek_api_key: str
    deepseek_model: str

    # Kwork polling
    kwork_poll_interval: int
    kwork_max_ai_per_cycle: int
    kwork_seed_seen: int

    # Storage
    sqlite_path: Path

    # Logging
    log_level: str

    # Cookie refresh: either a shell script path (local dev / same-host deploy)
    # or an HTTP URL that the proxy exposes (Docker / separate host).
    cookie_refresh_script: Path | None
    cookie_refresh_url: str
    cookie_refresh_secret: str


def load_config() -> Config:
    owner_raw = os.getenv("TELEGRAM_OWNER_CHAT_ID", "").strip()
    owner_id: int | None
    if owner_raw:
        try:
            owner_id = int(owner_raw)
        except ValueError:
            raise RuntimeError(
                f"TELEGRAM_OWNER_CHAT_ID must be an integer, got {owner_raw!r}"
            )
    else:
        owner_id = None

    sqlite_path = Path(_env("SQLITE_PATH", "./data/kwork_bot.sqlite3"))
    sqlite_path.parent.mkdir(parents=True, exist_ok=True)

    cookie_refresh_script_raw = _env("COOKIE_REFRESH_SCRIPT", "")
    cookie_refresh_script: Path | None = None
    if cookie_refresh_script_raw:
        cookie_refresh_script = Path(cookie_refresh_script_raw).expanduser().resolve()

    return Config(
        telegram_bot_token=_env("TELEGRAM_BOT_TOKEN", required=True),
        telegram_owner_chat_id=owner_id,
        deepseek_api_base=_env("DEEPSEEK_API_BASE", "http://127.0.0.1:8080/v1"),
        # OpenAI SDK requires *some* api_key; "unused" is the convention for local proxies.
        deepseek_api_key=_env("DEEPSEEK_API_KEY", "") or "unused",
        deepseek_model=_env("DEEPSEEK_MODEL", "deepseek-chat"),
        kwork_poll_interval=_env_int("KWORK_POLL_INTERVAL", 45),
        kwork_max_ai_per_cycle=_env_int("KWORK_MAX_AI_PER_CYCLE", 8),
        kwork_seed_seen=_env_int("KWORK_SEED_SEEN", 12),
        sqlite_path=sqlite_path,
        log_level=_env("LOG_LEVEL", "INFO").upper(),
        cookie_refresh_script=cookie_refresh_script,
        cookie_refresh_url=_env("COOKIE_REFRESH_URL", ""),
        cookie_refresh_secret=_env("COOKIE_REFRESH_SECRET", ""),
    )

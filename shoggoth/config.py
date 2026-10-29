"""Typed settings loaded from the environment.

Fails fast on anything missing, and never logs a secret.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# The bare domain redirects and drops the Authorization header, so the www
# prefix is part of the contract, not a stylistic choice.
DEFAULT_BASE_URL = "https://www.moltbook.com/api/v1"
DEFAULT_STATE_PATH = Path("state/state.json")
DEFAULT_PERSONA_DIR = Path("persona")


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _required(name: str) -> str:
    value = _env(name)
    if not value:
        raise ConfigError(f"missing required environment variable: {name}")
    return value


def _flag(name: str, default: bool = False) -> bool:
    value = _env(name).lower()
    if not value:
        return default
    return value in {"1", "true", "yes", "on"}


def utc_now_iso() -> str:
    """UTC timestamp in a fixed, lexicographically sortable format."""
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class Settings:
    moltbook_api_key: str
    telegram_bot_token: str
    telegram_chat_id: str
    moltbook_base_url: str = DEFAULT_BASE_URL
    state_path: Path = DEFAULT_STATE_PATH
    persona_dir: Path = DEFAULT_PERSONA_DIR
    dry_run: bool = False
    request_timeout: float = 20.0

    @property
    def topics_path(self) -> Path:
        return self.persona_dir / "topics.toml"

    @property
    def instructions_path(self) -> Path:
        return self.persona_dir / "instructions.md"

    @classmethod
    def from_env(cls, *, require_moltbook: bool = True, require_telegram: bool = True) -> "Settings":
        return cls(
            moltbook_api_key=_required("MOLTBOOK_API_KEY") if require_moltbook else _env("MOLTBOOK_API_KEY"),
            telegram_bot_token=_required("TELEGRAM_BOT_TOKEN") if require_telegram else _env("TELEGRAM_BOT_TOKEN"),
            telegram_chat_id=_required("TELEGRAM_CHAT_ID") if require_telegram else _env("TELEGRAM_CHAT_ID"),
            moltbook_base_url=_env("MOLTBOOK_BASE_URL", DEFAULT_BASE_URL),
            state_path=Path(_env("STATE_PATH", str(DEFAULT_STATE_PATH))),
            persona_dir=Path(_env("PERSONA_DIR", str(DEFAULT_PERSONA_DIR))),
            dry_run=_flag("DRY_RUN"),
            request_timeout=float(_env("REQUEST_TIMEOUT", "20")),
        )

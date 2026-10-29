"""Telegram Bot API delivery.

Two rules, both of which matter more here than in most bots:

* ``parse_mode="HTML`` with everything escaped. Post titles are attacker-controlled
  text from the open internet; legacy Markdown chokes on stray ``_`` and ``*`` and
  MarkdownV2 requires escaping eighteen reserved characters. HTML needs three.
* Chunking and rate limiting. The documented limit is 4096 characters per message
  and roughly one message a second per chat.
"""

from __future__ import annotations

import html
import logging
import time
from typing import Any

import requests

log = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org"

# Documented limit is 4096; we stay comfortably under it and split on safe
# boundaries so formatting never straddles a chunk.
MAX_CHARS = 3500
SAFE_SPLIT = ("\n\n", "\n", " ")

# ~1 message/second per chat, 20/minute in a group.
SECONDS_BETWEEN_MESSAGES = 1.2


def escape_html(text: Any) -> str:
    """Escape untrusted text for Telegram's HTML parse mode.

    Escapes ``&``, ``<``, ``>``, ``"`` and ``'``. Apply this to every field that
    came from the network, without exception.
    """
    return html.escape(str(text), quote=True)


def safe_href(url: str) -> str:
    """Escape a URL for use inside an ``<a href>`` attribute."""
    return escape_html(url)


def chunk_message(text: str, limit: int = MAX_CHARS) -> list[str]:
    """Split text into chunks no longer than ``limit``, preferring safe boundaries."""
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break
        window = remaining[:limit]
        cut = 0
        for separator in SAFE_SPLIT:
            found = window.rfind(separator)
            # Refuse a boundary in the first half: it would produce silly slivers.
            if found > limit // 2:
                cut = found + len(separator)
                break
        if not cut:
            cut = limit  # no safe boundary available; split hard
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip("\n")
    return [chunk for chunk in chunks if chunk]


class Telegram:
    """Minimal sender. Returns False rather than raising, so callers can degrade."""

    def __init__(
        self,
        token: str,
        chat_id: str,
        dry_run: bool = False,
        session: requests.Session | None = None,
    ) -> None:
        self.token = token
        self.chat_id = chat_id
        self.dry_run = dry_run
        self.session = session or requests.Session()

    @property
    def _base(self) -> str:
        return f"{TELEGRAM_API}/bot{self.token}"

    def send(
        self,
        text: str,
        *,
        parse_mode: str = "HTML",
        disable_web_page_preview: bool = True,
    ) -> bool:
        """Send one message, chunking and pacing as needed.

        Callers are responsible for escaping any untrusted content before it
        reaches this method — see :func:`escape_html`.
        """
        chunks = chunk_message(text)
        if len(chunks) > 1:
            log.info("message split into %d chunks", len(chunks))

        success = True
        for index, chunk in enumerate(chunks):
            if index and not self.dry_run:
                time.sleep(SECONDS_BETWEEN_MESSAGES)
            if not self._send_chunk(chunk, parse_mode, disable_web_page_preview):
                success = False
        return success

    def _send_chunk(self, text: str, parse_mode: str, disable_preview: bool) -> bool:
        payload: dict[str, Any] = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": disable_preview,
        }

        if self.dry_run:
            print("--- dry run: Telegram message ---")
            print(text)
            print("--- end ---")
            return True

        try:
            response = self.session.post(
                f"{self._base}/sendMessage", json=payload, timeout=20
            )
        except requests.RequestException as exc:
            log.error("Telegram request failed: %s", exc)
            return False

        try:
            body = response.json()
        except ValueError:
            log.error("Telegram returned non-JSON (HTTP %s)", response.status_code)
            return False

        if not body.get("ok"):
            log.error(
                "Telegram rejected the message (HTTP %s): %s",
                response.status_code,
                body.get("description"),
            )
            return False
        return True

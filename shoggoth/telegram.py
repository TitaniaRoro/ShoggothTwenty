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
import json
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

    def send_document(self, filename: str, content: str, *, caption: str = "") -> bool:
        """Upload a file. Returns False rather than raising, like :meth:`send`.

        Used for reports too long or too structured to read as a message. The
        body is sent verbatim as ``text/markdown``; the caption is plain text
        with no parse mode, so neither needs escaping.
        """
        if self.dry_run:
            print(f"--- dry run: document {filename} ---")
            print(content[:2000])
            print("--- end ---")
            return True

        files = {"document": (filename, content.encode("utf-8"), "text/markdown")}
        data: dict[str, Any] = {"chat_id": self.chat_id}
        if caption:
            # Document captions cap at 1024 characters.
            data["caption"] = caption[:1024]

        try:
            response = self.session.post(
                f"{self._base}/sendDocument", data=data, files=files, timeout=30
            )
        except requests.RequestException as exc:
            log.error("Telegram document upload failed: %s", exc)
            return False

        try:
            body = response.json()
        except ValueError:
            log.error("Telegram returned non-JSON (HTTP %s)", response.status_code)
            return False

        if not body.get("ok"):
            log.error(
                "Telegram rejected the document (HTTP %s): %s",
                response.status_code,
                body.get("description"),
            )
            return False

        log.info("sent document %s (%d bytes)", filename, len(content))
        return True

    def send_approval(self, text: str, proposal_ids: str | list[str]) -> bool:
        """Send drafts for human sign-off with Approve / Reject buttons.

        One numbered row per draft, so a whole batch is decidable from a single
        card. At six drafts a day, one message per draft meant six separate
        cards to scroll back through; the cost of approving was the real limit
        on how much the agent could publish, not the drafting itself.

        The heartbeat polls for the presses on a later run, so approvals need
        no webhook, no public endpoint and no long-lived process.
        """
        if isinstance(proposal_ids, str):
            proposal_ids = [proposal_ids]

        keyboard = [
            [
                {"text": f"\u2705 {number}", "callback_data": f"a:{pid}"},
                {"text": f"\u274c {number}", "callback_data": f"r:{pid}"},
            ]
            for number, pid in enumerate(proposal_ids, start=1)
        ]

        payload: dict[str, Any] = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
            "reply_markup": {"inline_keyboard": keyboard},
        }

        if self.dry_run:
            print("--- dry run: approval request ---")
            print(text)
            print("buttons:", "  ".join(
                f"[{button['text']}]" for row in keyboard for button in row
            ))
            print("--- end ---")
            return True

        try:
            response = self.session.post(
                f"{self._base}/sendMessage", json=payload, timeout=20
            )
            body = response.json()
        except (requests.RequestException, ValueError) as exc:
            log.error("approval request failed: %s", exc)
            return False

        if not body.get("ok"):
            log.error("approval request rejected: %s", body.get("description"))
            return False
        return True

    def answer_callback(self, query_id: str | None, text: str = "") -> bool:
        """Acknowledge a button press.

        Purely a client-side courtesy, and a necessary one: until this is
        called the button stays in its loading state and the chat shows no
        confirmation, so a press that worked looks identical to one that
        didn't.
        """
        if not query_id or self.dry_run:
            return True

        try:
            response = self.session.post(
                f"{self._base}/answerCallbackQuery",
                json={"callback_query_id": query_id, "text": text},
                timeout=10,
            )
            body = response.json()
        except requests.RequestException as exc:
            log.error("answerCallbackQuery failed: %s", exc)
            return False

        if not body.get("ok"):
            log.error("answerCallbackQuery rejected: %s", body.get("description"))
            return False
        return True

    def poll_callbacks(self, offset: int) -> tuple[list[dict] | None, int]:
        """Fetch pending button presses.

        Returns (callbacks, next_offset), or (None, offset) when the poll
        itself failed. None is not the same as []: an empty list means the
        human has pressed nothing, None means we could not ask. Callers must
        not treat them alike, or a dead poll looks like a quiet human.

        The offset must be persisted by the caller so an approval is never
        processed twice — or never at all.
        """
        if self.dry_run:
            return [], offset

        try:
            response = self.session.get(
                f"{self._base}/getUpdates",
                params={
                    "offset": offset,
                    "timeout": 0,
                    # Telegram wants a JSON-serialized array here. Passing a
                    # Python list makes requests send
                    # allowed_updates=callback_query, which the API rejects —
                    # getUpdates then never succeeds, no press is ever read,
                    # and the buttons look dead while logging nothing useful.
                    "allowed_updates": json.dumps(["callback_query"]),
                },
                timeout=25,
            )
            body = response.json()
        except (requests.RequestException, ValueError) as exc:
            log.error("getUpdates failed: %s", exc)
            return None, offset

        if not body.get("ok"):
            # Returned rather than raised so one bad poll cannot kill a run —
            # but the caller counts these, because a poll that always fails
            # means no approval ever lands and nothing ever publishes.
            log.error("getUpdates rejected: %s", body.get("description"))
            return None, offset

        callbacks: list[dict] = []
        next_offset = offset
        for update in body.get("result", []):
            next_offset = max(next_offset, int(update.get("update_id", 0)) + 1)
            query = update.get("callback_query")
            if not query:
                continue
            data = (query.get("data") or "").strip()
            if ":" not in data:
                continue
            action, _, proposal_id = data.partition(":")
            if action in ("a", "r") and proposal_id:
                callbacks.append({
                    "action": "approve" if action == "a" else "reject",
                    "id": proposal_id,
                    "query_id": query.get("id"),
                })

        return callbacks, next_offset

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

"""Render Telegram digests.

Every field here is untrusted — titles and author names come from other agents on
the open internet — so every one of them goes through ``escape_html`` before it
reaches the message. We build the markup; they supply only escaped text.
"""

from __future__ import annotations

from typing import Any

from .telegram import escape_html, safe_href

HEADING = "\U0001f99e <b>ShoggothTwenty digest</b>"


def _render_item(item: dict[str, Any]) -> str:
    submolt = escape_html(item.get("submolt", "unknown"))
    title = escape_html(item.get("title", "(untitled)")) or "(untitled)"
    score_value = item.get("score", 0.0)
    matched = ", ".join(escape_html(m) for m in item.get("matched", []))
    author = escape_html(item.get("author", "unknown"))

    line = f"• <b>m/{submolt}</b> — {title}\n  <i>{author}</i>"
    if matched:
        line += f"\n  {matched} (score {score_value:.1f})"
    else:
        line += f"\n  score {score_value:.1f}"

    # Only ever link to a URL we constructed from a validated post ID.
    url = item.get("url")
    if url:
        line += f"\n  <a href=\"{safe_href(url)}\">open on Moltbook</a>"
    return line


def render_digest(items: list[dict[str, Any]], *, suppressed: int = 0) -> str:
    """Render a batched digest, escaping everything untrusted."""
    count = len(items)
    noun = "post" if count == 1 else "posts"
    lines = [f"{HEADING} — {count} new {noun}", ""]

    for item in items:
        lines.append(_render_item(item))
        lines.append("")

    if suppressed:
        lines.append(f"(+{suppressed} more withheld — cap reached)")

    return "\n".join(lines).rstrip()


def render_alert(title: str, detail: str) -> str:
    """Render an operational alert: failures, suspension risk, escalation."""
    return f"⚠️ <b>{escape_html(title)}</b>\n\n{escape_html(detail)}"

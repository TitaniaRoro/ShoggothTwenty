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


def render_published(items: list[dict], level: str) -> str:
    """Report what the agent published without asking anyone first.

    An unattended publish has no approval card to point back at, so without
    this the only record is a line in a workflow log nobody reads. The owner
    is responsible for everything the agent posts, and cannot review what
    they are never shown.
    """
    lines = [
        f"\U0001f916 <b>ShoggothTwenty — published unattended (level {escape_html(level)})</b>",
        "",
    ]
    for number, item in enumerate(items, start=1):
        url = item.get("url")
        heading = f"{number}. <b>{escape_html(item.get('header', '(unknown)'))}</b>"
        if url:
            heading = f'{number}. <b><a href="{escape_html(url)}">{escape_html(item.get("header", "(unknown)"))}</a></b>'
        lines.append(heading)
        if item.get("title"):
            lines.append(f"   <i>{escape_html(item['title'])}</i>")
        lines.append(f"   {escape_html(item.get('kind', 'comment'))} — {escape_html(item.get('meta', ''))}".rstrip(" — "))
        lines.append("")
        body = escape_html(item.get("text", ""))
        lines.append(body[:700] + ("…" if len(body) > 700 else ""))
        lines.append("")
    lines.append(
        "<i>Drop to L0 in Settings-level control if you would rather approve these first.</i>"
    )
    return "\n".join(lines).rstrip()


def render_alert(title: str, detail: str) -> str:
    """Render an operational alert: failures, suspension risk, escalation."""
    return f"⚠️ <b>{escape_html(title)}</b>\n\n{escape_html(detail)}"


# NOTE: render_proposal and render_post_proposal used to live here, one card
# per draft. They were removed when approvals became batched — leaving them
# would keep a dead renderer that still looked live, and the escaping
# guarantee has to be tested against the function that actually ships.

BATCH_HEADING = "✍️ <b>ShoggothTwenty — drafts for review</b>"

# Telegram caps a message at 4096 characters. Stay well under it so a card
# never has to be split mid-draft, which would strand its buttons.
BATCH_ITEM_BUDGET = 3000


def render_batch(items: list[dict], *, start: int = 1) -> str:
    """Render several drafts in one card, numbered to match their buttons.

    Every field is untrusted — titles and bodies came from a model that read
    other agents' posts — so all of it is escaped. We build the markup; they
    supply only escaped text.
    """
    lines = [BATCH_HEADING, ""]

    for offset, item in enumerate(items):
        number = start + offset
        kind = "post" if item.get("kind") == "post" else "comment"
        header = escape_html(item.get("header", "")) or "(untitled)"
        lines.append(f"{number}. <b>{header}</b> <i>({kind})</i>")

        meta = item.get("meta")
        if meta:
            lines.append(f"   {escape_html(meta)}")

        title = item.get("title")
        if title:
            lines.append(f"   <b>{escape_html(title)}</b>")

        url = item.get("url")
        if url:
            lines.append(f"   <a href=\"{safe_href(url)}\">open on Moltbook</a>")

        lines.append("")
        lines.append(escape_html(item.get("text", "")))
        lines.append("")

    lines.append("Tap ✅ to publish, ❌ to discard. Numbering matches the buttons.")
    return "\n".join(lines).rstrip()


def chunk_items(items: list[dict], limit: int = BATCH_ITEM_BUDGET) -> list[list[dict]]:
    """Split drafts into cards that fit Telegram's limit, keeping each whole.

    Items are never truncated: a draft you can only partly read is a draft you
    should not be approving. If it does not fit, it goes on the next card.
    """
    groups: list[list[dict]] = []
    current: list[dict] = []
    size = 0

    for item in items:
        cost = len(item.get("text", "")) + len(item.get("header", "")) + 120
        # A single oversized draft still gets its own card rather than being
        # cut: the alternative is approving text nobody can read.
        if current and size + cost > limit:
            groups.append(current)
            current, size = [], 0
        current.append(item)
        size += cost

    if current:
        groups.append(current)
    return groups

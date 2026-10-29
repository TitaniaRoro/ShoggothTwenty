"""Telegram delivery: escaping, chunking, and treating rejections as failures."""

from __future__ import annotations

import pytest

from shoggoth.telegram import Telegram, chunk_message, escape_html, safe_href


# --- escaping -------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("plain", "plain"),
        ("a & b", "a &amp; b"),
        ("<script>", "&lt;script&gt;"),
        ("5 < 6 and 7 > 6", "5 &lt; 6 and 7 &gt; 6"),
        ('quote " and \' apostrophe', "quote &quot; and &#x27; apostrophe"),
    ],
)
def test_escape_html(raw, expected):
    assert escape_html(raw) == expected


def test_hostile_title_becomes_literal_text():
    """This is the whole point: post titles are attacker-controlled."""
    hostile = "FREE <b>crypto</b> & click <a href='http://evil'>here</a>"

    escaped = escape_html(hostile)

    assert "<b>" not in escaped
    assert "<a " not in escaped
    assert "&lt;b&gt;" in escaped


def test_escape_handles_non_strings():
    assert escape_html(42) == "42"
    assert escape_html(None) == "None"


def test_safe_href_escapes_quotes():
    assert "&#x27;" in safe_href("http://x/?a='b'")


# --- chunking -------------------------------------------------------------


def test_short_message_is_one_chunk():
    assert chunk_message("hello") == ["hello"]


def test_long_message_is_split_and_under_the_limit():
    text = "\n\n".join(f"paragraph {i}" for i in range(400))

    chunks = chunk_message(text)

    assert len(chunks) > 1
    assert all(len(chunk) <= 3500 for chunk in chunks)
    # Nothing is lost: every paragraph survives the split.
    assert all(f"paragraph {i}" in "".join(chunks) for i in (0, 399))


def test_chunks_prefer_paragraph_boundaries():
    text = ("first " * 500) + "\n\n" + ("second " * 500)

    chunks = chunk_message(text)

    assert len(chunks) == 2
    assert chunks[0].startswith("first")
    assert chunks[1].startswith("second")


def test_unbreakable_text_is_hard_split_not_dropped():
    text = "x" * 9000

    chunks = chunk_message(text)

    assert len(chunks) == 3
    assert sum(len(chunk) for chunk in chunks) >= 8900


# --- sending --------------------------------------------------------------


class RecordingSession:
    def __init__(self, ok=True, description=None, status=200):
        self.ok = ok
        self.description = description
        self.status = status
        self.payloads: list[dict] = []

    def post(self, url, json=None, timeout=None):  # noqa: A002 - requests' own signature
        self.payloads.append(json)

        class Response:
            status_code = self.status

            def json(inner):  # noqa: N805
                return {"ok": self.ok, "description": self.description}

        return Response()


def test_send_uses_html_mode_and_disables_previews():
    session = RecordingSession()
    telegram = Telegram("token", "123", session=session)

    assert telegram.send("hello") is True

    payload = session.payloads[0]
    assert payload["parse_mode"] == "HTML"
    assert payload["disable_web_page_preview"] is True
    assert payload["chat_id"] == "123"


def test_rejection_is_reported_not_swallowed():
    """The original script ignored the response entirely."""
    session = RecordingSession(ok=False, description="can't parse entities", status=400)
    telegram = Telegram("token", "123", session=session)

    assert telegram.send("hello") is False


def test_dry_run_prints_without_sending(monkeypatch):
    session = RecordingSession()
    telegram = Telegram("token", "123", dry_run=True, session=session)

    assert telegram.send("hello") is True
    assert session.payloads == []


def test_multi_chunk_send_reports_partial_failure():
    class FlakySession(RecordingSession):
        def post(self, url, json=None, timeout=None):  # noqa: A002
            self.payloads.append(json)
            should_fail = len(self.payloads) == 2

            class Response:
                status_code = 200

                def json(inner):  # noqa: N805
                    return {"ok": not should_fail, "description": "boom"}

            return Response()

    session = FlakySession()
    telegram = Telegram("token", "123", session=session)

    assert telegram.send("\n\n".join("word " * 400 for _ in range(4))) is False
    assert len(session.payloads) > 1

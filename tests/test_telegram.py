"""Telegram delivery: escaping, chunking, and treating rejections as failures."""

from __future__ import annotations

import pytest
import requests

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


# --- buttons -------------------------------------------------------------

# The Approve / Reject flow failed silently in production: the cards arrived,
# the taps were registered by Telegram, and nothing ever happened. Root cause
# was the wire format of one parameter. These pin it.


class RecordingGetSession:
    """A session that records what poll_callbacks actually asked for."""

    def __init__(self, result=None):
        self.result = result if result is not None else []
        self.params: dict = {}
        self.payloads: list = []

    def get(self, url, params=None, timeout=None):
        self.params = params or {}
        result = self.result

        class Response:
            status_code = 200

            def json(inner):  # noqa: N805
                return {"ok": True, "result": result}

        return Response()

    def post(self, url, json=None, timeout=None):  # noqa: A002 - requests' own name
        self.payloads.append(json)
        payload = json

        class Response:
            status_code = 200

            def json(inner):  # noqa: N805
                return {"ok": True, "result": payload}

        return Response()


def test_getUpdates_sends_allowed_updates_as_a_json_array():
    """The bug that made the buttons dead.

    Telegram documents allowed_updates as a JSON-serialized list. Passing a
    Python list instead makes requests emit `allowed_updates=callback_query`,
    which the API rejects — getUpdates then fails on every run, no press is
    ever read, and the buttons look broken while logging only a line nobody
    reads.
    """
    session = RecordingGetSession()
    telegram = Telegram("token", "42", session=session)

    telegram.poll_callbacks(0)

    assert session.params["allowed_updates"] == '["callback_query"]', (
        "allowed_updates must be a JSON-serialized array, not a bare string"
    )


def test_a_button_press_is_read_as_an_approval():
    session = RecordingGetSession(
        [{"update_id": 7, "callback_query": {"id": "q1", "data": "a:abc123"}}]
    )
    telegram = Telegram("token", "42", session=session)

    callbacks, offset = telegram.poll_callbacks(0)

    assert callbacks == [{"action": "approve", "id": "abc123", "query_id": "q1"}]
    assert offset == 8  # next_offset is last update_id + 1, or it replays forever


def test_a_rejection_is_read_as_a_rejection():
    session = RecordingGetSession(
        [{"update_id": 9, "callback_query": {"id": "q2", "data": "r:abc123"}}]
    )
    telegram = Telegram("token", "42", session=session)

    callbacks, _ = telegram.poll_callbacks(0)
    assert callbacks[0]["action"] == "reject"


def test_unrelated_updates_advance_the_offset_and_are_ignored():
    """A plain message must not be read as a press — but it must still move
    the offset, or getUpdates returns it forever."""
    session = RecordingGetSession(
        [{"update_id": 11, "message": {"text": "hello"}},
         {"update_id": 12, "callback_query": {"id": "q3", "data": "a:xyz"}}]
    )
    telegram = Telegram("token", "42", session=session)

    callbacks, offset = telegram.poll_callbacks(0)

    assert [c["id"] for c in callbacks] == ["xyz"]
    assert offset == 13


def test_the_press_is_acknowledged_so_the_button_stops_spinning():
    """Without answerCallbackQuery the client keeps the button loading and
    shows no confirmation, so a press that worked looks like one that didn't."""
    session = RecordingGetSession()
    telegram = Telegram("token", "42", session=session)

    assert telegram.answer_callback("q1", "Approved") is True
    assert session.payloads[-1]["callback_query_id"] == "q1"

    # A press with no query id must not raise: it is absent, not an error.
    assert telegram.answer_callback(None, "Approved") is True


def test_a_failed_poll_is_reported_as_none_not_as_silence():
    """A dead poll and a quiet human look identical if both return [].

    They are very different: at L0 the buttons are the only route to
    publishing, so a poll that always fails freezes the agent while every
    other step still reports success.
    """

    class RejectingSession:
        status = 400

        def get(self, url, params=None, timeout=None):
            class Response:
                status_code = 400

                def json(inner):  # noqa: N805
                    return {
                        "ok": False,
                        "description": "Bad Request: can't parse allowed_updates",
                    }

            return Response()

    telegram = Telegram("token", "42", session=RejectingSession())
    callbacks, offset = telegram.poll_callbacks(5)

    assert callbacks is None, "a failed poll must not look like 'no presses'"
    assert offset == 5, "a failed poll must not advance the offset"


def test_a_failed_poll_does_not_raise():
    """One bad poll cannot take down a heartbeat."""

    class BrokenSession:
        def get(self, url, params=None, timeout=None):
            raise requests.RequestException("network down")

    telegram = Telegram("token", "42", session=BrokenSession())
    assert telegram.poll_callbacks(1) == (None, 1)

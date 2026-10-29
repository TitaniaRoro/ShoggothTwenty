"""The three things a naive Moltbook client gets wrong."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from shoggoth.config import Settings
from shoggoth.moltbook import MoltbookClient, retry_after_seconds

from .conftest import StubSession, make_response


def make_settings(**kwargs) -> Settings:
    return Settings(
        moltbook_api_key="moltbook_test_key",
        telegram_bot_token="token",
        telegram_chat_id="1",
        **kwargs,
    )


def client_with(*responses, settings: Settings | None = None):
    session = StubSession(*responses)
    return MoltbookClient(settings or make_settings(), session=session), session


# --- envelope -------------------------------------------------------------


def test_posts_live_under_data_not_at_top_level():
    """The original script read .get("posts") at the top level and always got []."""
    payload = {"success": True, "data": {"posts": [{"id": "abc"}], "has_more": False}}
    client, _ = client_with(make_response(200, payload))

    result = client.get_posts(submolt="agents")

    assert result.ok
    assert result.data["posts"][0]["id"] == "abc"


def test_error_envelope_is_a_failure_not_an_empty_feed():
    client, _ = client_with(make_response(200, {"success": False, "error": "nope", "hint": "try again"}))

    result = client.get_posts()

    assert not result.ok
    assert result.error == "nope"
    assert result.hint == "try again"


def test_401_is_never_mistaken_for_no_new_posts():
    client, _ = client_with(make_response(401, {"success": False, "error": "invalid key"}))

    result = client.get_posts()

    assert not result.ok
    assert result.status == 401
    assert "unauthorized" in result.error
    assert "www.moltbook.com" in result.hint


def test_non_json_response_is_a_failure():
    client, _ = client_with(make_response(502, body_text="<html>bad gateway</html>"))

    result = client.get_posts()

    assert not result.ok
    assert "non-JSON" in result.error


def test_success_false_with_no_data_is_not_crashing():
    client, _ = client_with(make_response(200, {"success": True, "data": None}))

    result = client.get_posts()

    assert result.ok
    assert result.data == {}


# --- 429 ------------------------------------------------------------------


def test_429_uses_a_different_body_shape():
    """429s do not use the {success, error} envelope."""
    body = {"statusCode": 429, "message": "Rate limit exceeded", "retry_after_seconds": 45}
    response = make_response(429, body, headers={"Retry-After": "45"})

    assert retry_after_seconds(response) == 45.0


def test_short_429_sleeps_and_retries_once(monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr("shoggoth.moltbook.time.sleep", slept.append)

    payload = {"success": True, "data": {"posts": [], "has_more": False}}
    client, session = client_with(
        make_response(429, {"statusCode": 429, "retry_after_seconds": 12}, headers={"Retry-After": "12"}),
        make_response(200, payload),
    )

    result = client.get_posts()

    assert result.ok
    assert slept == [12.0]
    assert len(session.calls) == 2


def test_long_429_is_deferred_not_blocked(monkeypatch):
    """A 30 minute post cooldown must not burn the compute budget sleeping."""
    slept: list[float] = []
    monkeypatch.setattr("shoggoth.moltbook.time.sleep", slept.append)

    client, session = client_with(
        make_response(429, {"retry_after_minutes": 30}, headers={"Retry-After": "1800"})
    )

    result = client.get_posts()

    assert not result.ok
    assert result.deferred
    assert result.deferred_seconds == 1800.0
    assert slept == []
    assert len(session.calls) == 1


def test_retry_after_preference_order():
    # Header wins over body.
    response = make_response(
        429, {"retry_after_seconds": 99}, headers={"Retry-After": "5"}
    )
    assert retry_after_seconds(response) == 5.0

    # Body seconds beat body minutes.
    response = make_response(
        429, {"retry_after_seconds": 7, "retry_after_minutes": 3}
    )
    assert retry_after_seconds(response) == 7.0

    # Minutes are converted to seconds.
    response = make_response(429, {"retry_after_minutes": 2})
    assert retry_after_seconds(response) == 120.0

    # Nothing usable: a conservative default, never an immediate retry.
    assert retry_after_seconds(make_response(429, {})) == 60.0


# --- request hygiene ------------------------------------------------------


def test_every_request_has_a_timeout():
    client, session = client_with(make_response(200, {"success": True, "data": {}}))

    client.get_posts()

    assert session.last_kwargs["timeout"] > 0


def test_redirects_are_never_followed():
    """The bare domain redirects and drops the Authorization header."""
    client, session = client_with(make_response(200, {"success": True, "data": {}}))

    client.get_posts()

    assert session.last_kwargs["allow_redirects"] is False


def test_www_host_is_the_default():
    client, session = client_with(make_response(200, {"success": True, "data": {}}))

    client.get_posts()

    assert session.calls[-1][1].startswith("https://www.moltbook.com/api/v1")


def test_network_error_is_a_failure_not_a_crash():
    class Boom(StubSession):
        def request(self, method, url, **kwargs):
            import requests

            raise requests.ConnectionError("dns")

    client = MoltbookClient(make_settings(), session=Boom())

    result = client.get_posts()

    assert not result.ok
    assert "request failed" in result.error


# --- pagination -----------------------------------------------------------


def test_pagination_stops_on_has_more_false():
    page_one = {"success": True, "data": {"posts": [{"id": "a1b2c3d4-0000-0000-0000-000000000000"}], "has_more": False}}
    client, session = client_with(make_response(200, page_one))

    posts = list(client.iter_posts(submolt="agents"))

    assert len(posts) == 1
    assert len(session.calls) == 1


def test_pagination_follows_the_cursor():
    first = {
        "success": True,
        "data": {"posts": [{"id": "a1b2c3d4-0000-0000-0000-000000000000"}], "has_more": True, "next_cursor": "CURSOR2"},
    }
    second = {
        "success": True,
        "data": {"posts": [{"id": "a1b2c3d4-0000-0000-0000-000000000001"}], "has_more": False},
    }
    client, session = client_with(make_response(200, first), make_response(200, second))

    posts = list(client.iter_posts(submolt="agents"))

    assert len(posts) == 2
    assert session.calls[-1][2]["params"]["cursor"] == "CURSOR2"


def test_pagination_stops_on_error_instead_of_returning_nothing():
    client, _ = client_with(make_response(500, {"success": False, "error": "boom"}))

    assert list(client.iter_posts(submolt="agents")) == []


def test_cursor_is_never_fabricated():
    """We only ever send back a cursor the API gave us."""
    payload = {"success": True, "data": {"posts": [], "has_more": False, "next_cursor": None}}
    client, session = client_with(make_response(200, payload))

    list(client.iter_posts())

    assert session.calls[-1][2]["params"].get("cursor") is None

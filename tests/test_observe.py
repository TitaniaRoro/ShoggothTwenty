"""Normalisation of untrusted feed data."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from shoggoth.observe import collect, observation_from_post
from shoggoth.triage import TriageConfig

FIXTURES = Path(__file__).parent / "fixtures"
REPO_ROOT = Path(__file__).resolve().parent.parent


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_submolt_as_a_string():
    observation = observation_from_post({"id": "a1b2c3d4-1111-2222-3333-444455556666", "submolt": "agents"})

    assert observation is not None
    assert observation.submolt == "agents"


def test_submolt_as_an_object():
    """The read shape isn't documented; accept both rather than guessing wrong."""
    observation = observation_from_post(
        {"id": "a1b2c3d4-1111-2222-3333-444455556666", "submolt": {"name": "infrastructure", "display_name": "Infra"}}
    )

    assert observation is not None
    assert observation.submolt == "infrastructure"


def test_submolt_name_fallback():
    observation = observation_from_post(
        {"id": "a1b2c3d4-1111-2222-3333-444455556666", "submolt_name": "builds"}
    )

    assert observation is not None
    assert observation.submolt == "builds"


def test_unknown_submolt_degrades_gracefully():
    observation = observation_from_post({"id": "a1b2c3d4-1111-2222-3333-444455556666"})

    assert observation is not None
    assert observation.submolt == "unknown"


@pytest.mark.parametrize(
    "post_id",
    ["not a valid id!!", "", None, 12345, "../etc/passwd", "x" * 200],
)
def test_unusable_ids_are_dropped_not_interpolated(post_id):
    """Anything we'd put in a URL has to be validated first."""
    assert observation_from_post({"id": post_id}) is None


def test_valid_uuid_style_id_is_accepted():
    observation = observation_from_post({"id": "7f3c9a2e-1b4d-4e8a-9c0f-2d5e6a7b8c9d"})

    assert observation is not None
    assert observation.url == "https://www.moltbook.com/posts/7f3c9a2e-1b4d-4e8a-9c0f-2d5e6a7b8c9d"


def test_missing_title_and_content_do_not_crash():
    observation = observation_from_post({"id": "a1b2c3d4-1111-2222-3333-444455556666"})

    assert observation is not None
    assert observation.title == ""
    assert observation.content == ""


def test_author_as_string_or_object():
    assert observation_from_post(
        {"id": "a1b2c3d4-1111-2222-3333-444455556666", "author": "Molt"}
    ).author == "Molt"
    assert observation_from_post(
        {"id": "a1b2c3d4-1111-2222-3333-444455556666", "author": {"name": "Molt"}}
    ).author == "Molt"


def test_non_dict_posts_are_rejected():
    assert observation_from_post(["not", "a", "dict"]) is None


def test_collect_raises_when_home_is_unreachable():
    """A heartbeat that can't read must not be reported as 'nothing new'."""
    from shoggoth.config import Settings
    from shoggoth.moltbook import MoltbookClient

    from .conftest import StubSession, make_response

    settings = Settings(
        moltbook_api_key="k", telegram_bot_token="t", telegram_chat_id="1"
    )
    session = StubSession(make_response(200, {"success": False, "error": "down"}))
    client = MoltbookClient(settings, session=session)
    config = TriageConfig.load(REPO_ROOT / "persona" / "topics.toml")

    with pytest.raises(RuntimeError, match="GET /home failed"):
        collect(client, settings, config.submolts)

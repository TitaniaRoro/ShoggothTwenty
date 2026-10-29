"""Budgets, cooldowns, autonomy and the quality gate.

This is the layer that stands between untrusted post content and the agent
taking a real action, so it's checked before every request rather than after a
429.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from shoggoth.policy import (
    ESTABLISHED,
    MAX_COMMENT_CHARS,
    MIN_COMMENT_CHARS,
    MIN_POST_CHARS,
    NEW_AGENT,
    Policy,
    check_quality,
    novel_enough,
    sanitise_draft,
)
from shoggoth.state import State

# The daily counters roll at midnight UTC, so any fixture that wants a non-zero
# count has to be dated today or the rollover will wipe it.
TODAY = datetime.now(timezone.utc).strftime("%Y-%m-%d")


def policy_for(level: str = "L1", limits=None, budgets: dict | None = None) -> tuple[Policy, State]:
    state = State(autonomy_level=level)
    if budgets:
        state.budgets = budgets
    return Policy(state, limits), state


# --- platform limits -----------------------------------------------------


def test_established_limits_match_the_documented_rates():
    assert ESTABLISHED.post_cooldown_s == 30 * 60
    assert ESTABLISHED.comment_cooldown_s == 20
    assert ESTABLISHED.comments_per_day == 50


def test_new_agents_get_the_harsher_first_day_limits():
    """1 post per 2 hours and 20 comments a day for the first 24 hours."""
    assert NEW_AGENT.post_cooldown_s == 2 * 60 * 60
    assert NEW_AGENT.comments_per_day == 20


# --- daily counters ------------------------------------------------------


def test_counters_roll_over_at_midnight_utc():
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    policy, state = policy_for(budgets={"day": yesterday, "posts_today": 12, "comments_today": 50})

    policy.allows("comment")  # triggers the roll

    assert state.budgets["comments_today"] == 0
    assert state.budgets["day"] != yesterday


def test_daily_comment_cap_blocks():
    policy, _ = policy_for(budgets={"day": TODAY, "comments_today": 50})

    allowed, reason = policy.allows("comment")

    assert not allowed
    assert "cap reached" in reason


def test_daily_post_cap_blocks():
    policy, _ = policy_for(budgets={"day": TODAY, "posts_today": 48})

    allowed, reason = policy.allows("post")

    assert not allowed
    assert "cap reached" in reason


def test_upvotes_are_capped_so_they_cannot_crowd_out_writes():
    """Votes draw on the same 30-writes-per-minute budget as comments."""
    policy, _ = policy_for(budgets={"day": TODAY, "upvotes_today": 30})

    assert not policy.allows("upvote")[0]


# --- cooldowns -----------------------------------------------------------


def test_post_cooldown_defers_rather_than_failing():
    import time

    policy, state = policy_for(budgets={"day": TODAY})
    state.cooldowns["next_post_allowed"] = time.time() + 900

    allowed, reason = policy.allows("post")

    assert not allowed
    assert "cooldown" in reason
    assert "900s" in reason or "899s" in reason


def test_recording_an_action_starts_its_cooldown():
    import time

    policy, state = policy_for(budgets={"day": TODAY})

    policy.record("post")

    assert state.cooldowns["next_post_allowed"] > time.time()
    assert state.budgets["posts_today"] == 1


def test_unknown_action_kind_is_refused():
    policy, _ = policy_for()

    assert not policy.allows("delete_everything")[0]


# --- autonomy ------------------------------------------------------------


@pytest.mark.parametrize(
    "level,kind,expected",
    [
        ("L0", "comment", True),
        ("L0", "upvote", True),
        ("L0", "post", True),
        ("L1", "comment", False),
        ("L1", "upvote", False),
        ("L1", "post", True),
        ("L2", "post", False),
        ("L2", "comment", False),
        ("L3", "post", False),
    ],
)
def test_needs_approval_by_level(level, kind, expected):
    policy, _ = policy_for(level)

    assert policy.needs_approval(kind) is expected


def test_kill_switch_forces_approval_even_at_l3():
    policy, state = policy_for("L3")
    state.kill_switch = True

    assert policy.needs_approval("comment") is True


def test_unknown_level_defaults_to_requiring_approval():
    policy, state = policy_for("L9")

    assert policy.needs_approval("upvote") is True


# --- quality gate --------------------------------------------------------


GOOD_COMMENT = (
    "We hit this exact failure last month. Our cron kept reporting success while "
    "the workflow had already been disabled, so we added a dead man's switch."
)


def test_good_comment_passes():
    assert check_quality(GOOD_COMMENT, "comment") == (True, "ok")


def test_short_comment_is_rejected():
    """One-word replies and emoji spam are warning-level offences."""
    ok, reason = check_quality("nice", "comment")

    assert not ok
    assert "too short" in reason


def test_url_in_draft_is_rejected():
    """The persona is told never to emit URLs; enforce it here too."""
    ok, reason = check_quality(
        "Great point, more detail at https://example.com/evil — check it out now please", "comment"
    )

    assert not ok
    assert "URL" in reason


def test_emoji_spam_is_rejected():
    ok, _ = check_quality("🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥", "comment")
    assert not ok


def test_repetitive_filler_is_rejected():
    ok, _ = check_quality("a" * 200, "comment")
    assert not ok


def test_empty_draft_is_rejected():
    assert not check_quality("", "comment")[0]
    assert not check_quality("   ", "post")[0]


def test_post_needs_more_substance_than_a_comment():
    # Long enough for a comment, too short for a post.
    ok, reason = check_quality(GOOD_COMMENT[:100], "post")

    assert not ok
    assert "too short" in reason


def test_overlong_draft_is_rejected():
    assert not check_quality("word " * 2000, "comment")[0]


def test_control_characters_are_stripped():
    cleaned = sanitise_draft("hello​﻿world")

    assert cleaned == "helloworld"


def test_draft_length_bounds_are_sane():
    assert MIN_COMMENT_CHARS >= 20
    assert MIN_POST_CHARS > MIN_COMMENT_CHARS
    assert MAX_COMMENT_CHARS > MIN_COMMENT_CHARS


# --- novelty -------------------------------------------------------------


def test_duplicate_text_is_not_novel():
    text = "the quick brown fox jumps over the lazy dog"
    assert not novel_enough(text, [text])


def test_unrelated_text_is_novel():
    assert novel_enough("kubernetes cronjob scheduling", ["lobster recipes for dinner"])


def test_summary_is_readable():
    policy, state = policy_for("L1", budgets={"day": TODAY})

    summary = policy.summary()

    assert "L1" in summary
    assert "comments 0/" in summary

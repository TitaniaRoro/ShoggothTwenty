"""The policy layer: what the agent is allowed to do, enforced in code.

This is deliberately **not** the model's job. A model that decides its own
actions is the thing that turns untrusted post content into real-world effects.
Everything here is ordinary Python: budgets, cooldowns, allowlists and a quality
gate, all checked *before* a request goes out.

Limits come from Moltbook's rate-limit documentation (skill.md v1.12.0).
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Limits:
    posts_per_day: int
    comments_per_day: int
    post_cooldown_s: int
    comment_cooldown_s: int


# Established agents (past their first 24 hours).
ESTABLISHED = Limits(
    posts_per_day=48,
    comments_per_day=50,
    post_cooldown_s=30 * 60,
    comment_cooldown_s=20,
)

# New agents: harsher, and DMs are blocked entirely for the first day.
NEW_AGENT = Limits(
    posts_per_day=12,
    comments_per_day=20,
    post_cooldown_s=2 * 60 * 60,
    comment_cooldown_s=60,
)

# What each autonomy level may do unattended. Anything else is queued for a human.
AUTO_ACTIONS: dict[str, set[str]] = {
    "L0": set(),                                   # shadow: nothing happens unattended
    "L1": {"upvote", "comment"},                   # posts still need approval
    "L2": {"upvote", "comment", "post"},
    "L3": {"upvote", "comment", "post"},           # same permissions, retroactive review
}

# Moltbook's rules call out "one-word comments, emoji spam" as warning-level
# offences, so a chatty model is a liability rather than a feature.
MIN_COMMENT_CHARS = 40
MIN_POST_CHARS = 120
MAX_POST_CHARS = 4000
MAX_COMMENT_CHARS = 1500

_URL_RE = re.compile(r"https?://|www\.", re.IGNORECASE)
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\u200b-\u200f\u202a-\u202e\ufeff]")


def sanitise_draft(text: str) -> str:
    """Strip control and invisible characters before anything else looks at it."""
    return _CONTROL_RE.sub("", text or "").strip()


def check_quality(text: str, kind: str) -> tuple[bool, str]:
    """Reject drafts that would get the agent warned, or that leaked a URL.

    Returns (ok, reason).
    """
    cleaned = sanitise_draft(text)
    if not cleaned:
        return False, "empty draft"

    if _URL_RE.search(cleaned):
        # The persona is instructed never to emit URLs. Enforce it here too:
        # this kills the entire "get the agent to link to my site" class.
        return False, "draft contains a URL"

    length = len(cleaned)
    if kind == "post":
        if length < MIN_POST_CHARS:
            return False, f"post too short ({length} < {MIN_POST_CHARS})"
        if length > MAX_POST_CHARS:
            return False, f"post too long ({length} > {MAX_POST_CHARS})"
    else:
        if length < MIN_COMMENT_CHARS:
            return False, f"comment too short ({length} < {MIN_COMMENT_CHARS})"
        if length > MAX_COMMENT_CHARS:
            return False, f"comment too long ({length} > {MAX_COMMENT_CHARS})"

    # Emoji spam and single-character repetition are exactly what the rules ban.
    stripped = re.sub(r"[\W\d_]+", "", cleaned, flags=re.UNICODE)
    if len(stripped) < (MIN_COMMENT_CHARS // 4 if kind == "comment" else MIN_POST_CHARS // 4):
        return False, "draft is mostly punctuation or emoji"

    unique = len(set(cleaned))
    if len(cleaned) > 20 and unique <= 3:
        return False, "draft is repetitive filler"

    return True, "ok"


class Policy:
    """Budgets and permissions, backed by the durable state file."""

    def __init__(self, state, limits: Limits | None = None) -> None:
        self.state = state
        self.limits = limits or ESTABLISHED
        self._roll_day()

    @classmethod
    def for_state(cls, state, is_new_agent: bool = False) -> "Policy":
        return cls(state, NEW_AGENT if is_new_agent else ESTABLISHED)

    # -- daily counters ---------------------------------------------------

    def _roll_day(self) -> None:
        """Reset the counters at midnight UTC, which is when Moltbook does."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self.state.budgets.get("day") != today:
            if self.state.budgets:
                log.info("rolling daily counters into %s", today)
            self.state.budgets = {
                "day": today,
                "posts_today": 0,
                "comments_today": 0,
                "upvotes_today": 0,
            }

    # -- permissions ------------------------------------------------------

    def needs_approval(self, kind: str) -> bool:
        level = self.state.autonomy_level or "L0"
        if self.state.kill_switch:
            return True
        permitted = AUTO_ACTIONS.get(level, set())
        return kind not in permitted

    def _decision(self, kind: str) -> tuple[bool, str]:
        self._roll_day()
        budgets = self.state.budgets
        limits = self.limits
        now = time.time()

        if kind == "post":
            used = budgets.get("posts_today", 0)
            if used >= limits.posts_per_day:
                return False, f"daily post cap reached ({used}/{limits.posts_per_day})"
            ready_at = self.state.cooldowns.get("next_post_allowed", 0)
            if now < ready_at:
                return False, f"post cooldown ({int(ready_at - now)}s left)"
            return True, "ok"

        if kind == "comment":
            used = budgets.get("comments_today", 0)
            if used >= limits.comments_per_day:
                return False, f"daily comment cap reached ({used}/{limits.comments_per_day})"
            ready_at = self.state.cooldowns.get("next_comment_allowed", 0)
            if now < ready_at:
                return False, f"comment cooldown ({int(ready_at - now)}s left)"
            return True, "ok"

        if kind == "upvote":
            # Votes draw on the 30-writes-per-minute budget; keep them modest.
            if budgets.get("upvotes_today", 0) >= 30:
                return False, "daily upvote cap reached"
            return True, "ok"

        return False, f"unknown action kind: {kind}"

    def allows(self, kind: str) -> tuple[bool, str]:
        """Can this action be attempted right now, ignoring autonomy?"""
        return self._decision(kind)

    def record(self, kind: str) -> None:
        """Record a successful action, starting its cooldown."""
        now = time.time()
        if kind == "post":
            self.state.budgets["posts_today"] = self.state.budgets.get("posts_today", 0) + 1
            self.state.cooldowns["next_post_allowed"] = now + self.limits.post_cooldown_s
        elif kind == "comment":
            self.state.budgets["comments_today"] = self.state.budgets.get("comments_today", 0) + 1
            self.state.cooldowns["next_comment_allowed"] = now + self.limits.comment_cooldown_s
        elif kind == "upvote":
            self.state.budgets["upvotes_today"] = self.state.budgets.get("upvotes_today", 0) + 1

    def summary(self) -> str:
        budgets = self.state.budgets
        return (
            f"posts {budgets.get('posts_today', 0)}/{self.limits.posts_per_day}, "
            f"comments {budgets.get('comments_today', 0)}/{self.limits.comments_per_day}, "
            f"upvotes {budgets.get('upvotes_today', 0)}/30, "
            f"level {self.state.autonomy_level}"
            + (" (kill switch ON)" if self.state.kill_switch else "")
        )


def novel_enough(text: str, existing: Iterable[str], threshold: float = 0.8) -> bool:
    """Cheap novelty check against things we've already said.

    Moltbook's own search is semantic, and its docs suggest ~0.8 similarity as a
    practical "already covered" test. This is a crude token-overlap stand-in for
    when we don't want to spend a request on /search.
    """
    def tokens(value: str) -> set[str]:
        return {word for word in re.findall(r"[a-z]{4,}", (value or "").lower())}

    candidate = tokens(text)
    if not candidate:
        return False
    for previous in existing:
        other = tokens(previous)
        if not other:
            continue
        overlap = len(candidate & other) / len(candidate | other)
        if overlap >= threshold:
            return False
    return True

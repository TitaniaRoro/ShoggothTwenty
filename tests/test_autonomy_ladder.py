"""Proof that the autonomy level actually does something.

Until now it did not. Every level behaved identically: drafts always went
into pending_approvals, and the only code that ever published anything was
`_execute_approved`, which only touches proposals a human had already
tapped Approve on. The two actor call sites in the whole codebase both
passed approved=True.

So L1, L2 and L3 were real in Policy, real in the control workflow's
dropdown, and had no effect whatsoever on the agent. Setting L1 looked
like granting permission to comment unattended and granted nothing.

This is the same shape as the other defects in this project: a rule that
is written down, enforced somewhere, and never reaches the thing it was
supposed to govern.
"""

from __future__ import annotations

import types

import pytest

import agent
from shoggoth.policy import Policy
from shoggoth.state import State

KINDS = ("comment", "post")


class FakeActor:
    def __init__(self, ok: bool = True, deferred: bool = False):
        self.ok = ok
        self.deferred = deferred
        self.calls: list[tuple] = []

    def create_comment(self, target, text, approved=False):
        self.calls.append(("comment", target, approved))
        return types.SimpleNamespace(ok=self.ok, deferred=self.deferred, error="boom")

    def create_post(self, target, title, text, approved=False):
        self.calls.append(("post", target, approved))
        return types.SimpleNamespace(ok=self.ok, deferred=self.deferred, error="boom")


class FakeTelegram:
    def __init__(self):
        self.sent: list[str] = []

    def send(self, text, **kwargs):
        self.sent.append(text)


class FakeAudit:
    """Mirrors AuditLog.record(event, **fields): the event name is positional
    and `kind` arrives as a keyword, so binding it as a parameter name
    collides."""

    def __init__(self):
        self.events: list[tuple] = []

    def record(self, event, **fields):
        self.events.append((event, fields))


def _run(level: str, kind: str, actor: FakeActor | None = None) -> tuple[State, FakeActor, list, FakeTelegram, FakeAudit]:
    state = State()
    state.autonomy_level = level
    actor = actor or FakeActor()
    telegram = FakeTelegram()
    audit = FakeAudit()
    batch: list[dict] = []

    handled = agent._publish_or_queue(
        state=state,
        policy=Policy.for_state(state),
        actor=actor,
        telegram=telegram,
        audit=audit,
        kind=kind,
        target="target-1",
        text="a body of text",
        title="A title" if kind == "post" else None,
        score=3.0,
        observation_id="target-1",
        header="m/agents — something",
        meta="score 3.0",
        url=None,
        batch=batch,
    )
    return state, actor, batch, telegram, audit, handled


def _run_kill(level: str, kind: str) -> list[dict]:
    state = State()
    state.autonomy_level = level
    state.kill_switch = True
    batch: list[dict] = []
    agent._publish_or_queue(
        state=state, policy=Policy.for_state(state), actor=FakeActor(),
        telegram=FakeTelegram(), audit=FakeAudit(), kind=kind,
        target="t", text="body", title="T" if kind == "post" else None,
        score=3.0, observation_id="t", header="h", meta="m", url=None, batch=batch,
    )
    return batch


# --- the ladder now has an effect ---------------------------------------


@pytest.mark.parametrize("kind", KINDS)
def test_at_l0_everything_is_queued_for_a_human(kind):
    state, actor, batch, _, _, handled = _run("L0", kind)
    assert handled
    assert len(batch) == 1, "L0 must queue, never publish"
    assert len(state.pending_approvals) == 1
    assert state.pending_approvals[0]["status"] == "pending"
    assert actor.calls == [], "L0 must not touch the network"


@pytest.mark.parametrize("kind", KINDS)
def test_at_l1_comments_publish_and_posts_do_not(kind):
    state, actor, batch, _, _, handled = _run("L1", kind)
    assert handled
    if kind == "comment":
        assert actor.calls == [("comment", "target-1", False)]
        assert batch == [], "a published comment has nothing to approve"
        assert state.pending_approvals == []
    else:
        assert actor.calls == [], "L1 must not publish posts unattended"
        assert len(batch) == 1
        assert len(state.pending_approvals) == 1


@pytest.mark.parametrize("kind", KINDS)
def test_at_l2_and_l3_everything_publishes(kind):
    for level in ("L2", "L3"):
        state, actor, batch, _, _, handled = _run(level, kind)
        assert handled
        assert len(actor.calls) == 1, f"{level} should publish a {kind}"
        assert actor.calls[0][2] is False, "unattended means unattended"
        assert batch == []
        assert state.pending_approvals == []


def test_the_levels_are_actually_different_from_each_other():
    """The regression this file exists for. Before the fix, all four levels
    produced identical behaviour and this test would have passed trivially
    for the wrong reason."""
    published = []
    for level in ("L0", "L1", "L2", "L3"):
        _, actor, _, _, _, _ = _run(level, "comment")
        published.append(len(actor.calls))

    assert published == [0, 1, 1, 1], published
    assert len(set(published)) > 1, "every level behaving the same IS the bug"


# --- the kill switch still overrides everything -------------------------


@pytest.mark.parametrize("level", ["L0", "L1", "L2", "L3"])
@pytest.mark.parametrize("kind", KINDS)
def test_the_kill_switch_forces_approval_at_every_level(level, kind):
    batch = _run_kill(level, kind)
    assert len(batch) == 1, f"kill switch must stop {kind} at {level}"


# --- failures are loud, not silent --------------------------------------


def test_a_deferred_publish_stops_the_run_rather_than_marking_it_done():
    """A cooldown is not a failure. Marking the observation as handled would
    burn the candidate permanently."""
    actor = FakeActor(ok=False, deferred=True)
    state, _, batch, telegram, _, handled = _run("L2", "comment", actor=actor)
    assert handled is False, "deferred means stop drafting this run"
    assert batch == []
    assert telegram.sent == [], "a cooldown is not worth alerting about"


def test_a_failed_publish_alerts_and_names_the_level():
    actor = FakeActor(ok=False, deferred=False)
    _state, _, _batch, telegram, audit, handled = _run("L2", "post", actor=actor)
    assert handled
    assert telegram.sent, "a failed unattended publish must be reported"
    assert "L2" in telegram.sent[0], "the alert has to say which level did it"
    assert any(kind == "auto_publish_failed" for kind, _ in audit.events)


def test_a_successful_publish_is_audited_as_unattended():
    _state, _, _, _, audit, _ = _run("L1", "comment")
    assert any(kind == "auto_published" for kind, _ in audit.events)


def test_a_queued_draft_is_audited_as_a_draft():
    _state, _, _, _, audit, _ = _run("L0", "comment")
    assert any(kind == "draft" for kind, _ in audit.events)

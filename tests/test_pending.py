"""Re-sending drafts that are still awaiting a decision.

The problem this solves was found in production: nine drafts sat pending while
the agent had published nothing. Cards scroll out of view and Telegram only
holds a button press for 24 hours, so a backlog quietly becomes unactionable.

The age gate is the important part, and it is not really about staleness. A
re-sent card looks exactly like a fresh one. Drafts written under an earlier,
defective persona were still in state and would have been offered for approval
as though nothing had changed — four of them opened with invented first-hand
incidents ("We hit a silent-fail on a payment-gateway fallback last quarter").
Everything drafted since the persona fix opens with a mechanism instead.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from shoggoth.telegram import Telegram


def _hours_ago(hours: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


class RecordingTelegram:
    def __init__(self, *args, **kwargs):
        self.cards: list[tuple[str, list[str]]] = []

    def send(self, text):
        return True

    def send_approval(self, text, proposal_ids):
        if isinstance(proposal_ids, str):
            proposal_ids = [proposal_ids]
        self.cards.append((text, proposal_ids))
        return True

    def send_document(self, *args, **kwargs):
        return True

    def poll_callbacks(self, offset):
        return [], offset

    def answer_callback(self, *args, **kwargs):
        return True


@pytest.fixture
def harness(monkeypatch, tmp_path):
    import agent

    telegram = RecordingTelegram()
    monkeypatch.setattr(agent, "Telegram", lambda *a, **k: telegram)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "stub")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "stub")
    monkeypatch.setenv("STATE_PATH", str(tmp_path / "state.json"))

    def seed(*ages_hours: float) -> Path:
        from shoggoth.state import State

        state = State.load(Path(tmp_path / "state.json"))
        for index, age in enumerate(ages_hours):
            state.pending_approvals.append(
                {
                    "id": f"id{index:04d}",
                    "kind": "comment",
                    "target": f"post{index}",
                    "text": f"Draft number {index}.",
                    "status": "pending",
                    "score": 7.0,
                    "created_at": _hours_ago(age),
                }
            )
        state.save(Path(tmp_path / "state.json"))
        return Path(tmp_path / "state.json")

    def run(all_: bool = False):
        code = agent.cmd_pending(argparse.Namespace(all=all_))
        return code, telegram

    return seed, run, tmp_path


def test_recent_drafts_are_re_sent(harness):
    seed, run, _ = harness
    seed(1.0, 2.0)

    code, telegram = run()
    assert code == 0
    assert len(telegram.cards) == 1
    assert telegram.cards[0][1] == ["id0000", "id0001"]


def test_stale_drafts_are_withheld_by_default(harness):
    """The ones written under the old persona. Offering them again would put
    fabricated incidents in front of a human as though they were current."""
    seed, run, _ = harness
    seed(64.0)

    code, telegram = run()
    assert code == 0
    assert not telegram.cards


def test_all_overrides_the_age_gate_explicitly(harness):
    """Available, but you have to ask for it."""
    seed, run, _ = harness
    seed(64.0)

    code, telegram = run(all_=True)
    assert code == 0
    assert len(telegram.cards) == 1


def test_fresh_and_stale_are_separated_not_mixed(harness):
    seed, run, _ = harness
    seed(64.0, 1.0)

    _code, telegram = run()
    assert len(telegram.cards) == 1
    assert telegram.cards[0][1] == ["id0001"]  # only the recent one


def test_a_re_sent_card_shows_how_old_the_draft_is(harness):
    """Age is the only signal a human has for judging whether it is current."""
    seed, run, _ = harness
    seed(5.0)

    _code, telegram = run()
    assert "drafted 5h ago" in telegram.cards[0][0]


def test_nothing_pending_sends_nothing(harness):
    seed, run, _ = harness
    seed()  # no drafts

    code, telegram = run()
    assert code == 0
    assert not telegram.cards


def test_decided_drafts_are_not_re_sent(harness):
    seed, run, tmp_path = harness
    from shoggoth.state import State

    path = Path(tmp_path / "state.json")
    seed(1.0)
    state = State.load(path)
    state.pending_approvals[0]["status"] = "approved"
    state.save(path)

    _code, telegram = run()
    assert not telegram.cards


# --- discarding ----------------------------------------------------------


def test_clear_pending_discards_everything_awaiting_a_decision(harness):
    """The way out of a backlog you do not trust. Drafts outlive the persona
    that wrote them; a fix must not leave the old output publishable."""
    import agent

    seed, _run, tmp_path = harness
    from shoggoth.state import State

    path = seed(1.0, 64.0)
    code = agent.cmd_control(
        argparse.Namespace(level=None, kill=None, clear_challenge=False, clear_pending=True)
    )

    assert code == 0
    state = State.load(path)
    assert not [p for p in state.pending_approvals if p.get("status") == "pending"]


def test_clear_pending_leaves_already_decided_drafts_alone(harness):
    import agent

    seed, _run, tmp_path = harness
    from shoggoth.state import State

    path = seed(1.0)
    state = State.load(path)
    state.pending_approvals[0]["status"] = "approved"
    state.save(path)

    agent.cmd_control(
        argparse.Namespace(level=None, kill=None, clear_challenge=False, clear_pending=True)
    )

    state = State.load(path)
    assert state.pending_approvals[0]["status"] == "approved"

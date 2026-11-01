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
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from shoggoth.telegram import Telegram


def _hours_ago(hours: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


class RecordingTelegram:
    def __init__(self, *args, **kwargs):
        self.cards: list[tuple[str, list[str]]] = []
        self.presses: list[dict] = []   # button presses waiting to be polled

    def send(self, text):
        return True

    def send_approval(self, text, proposal_ids, start=1):
        if isinstance(proposal_ids, str):
            proposal_ids = [proposal_ids]
        self.cards.append((text, proposal_ids))
        return True

    def send_document(self, *args, **kwargs):
        return True

    def poll_callbacks(self, offset):
        presses, self.presses = list(self.presses), []
        return presses, offset

    def answer_callback(self, *args, **kwargs):
        return True


@pytest.fixture
def harness(monkeypatch, tmp_path):
    import agent

    telegram = RecordingTelegram()
    monkeypatch.setattr(agent, "Telegram", lambda *a, **k: telegram)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "stub")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "stub")
    # cmd_pending now harvests presses and publishes approved drafts, so it
    # needs the Moltbook credential it did not previously require.
    monkeypatch.setenv("MOLTBOOK_API_KEY", "stub")
    monkeypatch.setenv("STATE_PATH", str(tmp_path / "state.json"))

    # Nothing in these tests should touch the network. Stub the actor so an
    # approved draft is recorded rather than sent.
    class StubActor:
        def __init__(self, *a, **k):
            self.published: list[str] = []

        def create_comment(self, target, text, approved=False):
            self.published.append(target)
            return types.SimpleNamespace(ok=True, deferred=False)

        def create_post(self, target, title, text, approved=False):
            self.published.append(target)
            return types.SimpleNamespace(ok=True, deferred=False)

    monkeypatch.setattr(agent, "Actor", StubActor)
    monkeypatch.setattr(agent, "build_model", lambda *a, **k: None)

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

    def run(all_: bool = False, presses: list[dict] | None = None):
        telegram.presses = list(presses or [])
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


# --- the buttons did nothing ---------------------------------------------
#
# cmd_pending used to only re-send cards. It never asked Telegram what the
# human had pressed — that happened solely inside the heartbeat. With the
# schedule not firing, presses piled up in Telegram unread and every button
# looked dead.


def _press(*proposal_ids, action="approve") -> list[dict]:
    return [
        {"id": pid, "action": action, "query_id": f"q{n}"}
        for n, pid in enumerate(proposal_ids)
    ]


class _PublishingActor:
    """Records what would have been published, without any network."""

    instances: list = []

    def __init__(self, *a, **k):
        self.published: list[str] = []
        _PublishingActor.instances.append(self)

    def create_comment(self, target, text, approved=False):
        self.published.append(target)
        return types.SimpleNamespace(ok=True, deferred=False)

    def create_post(self, target, title, text, approved=False):
        self.published.append(target)
        return types.SimpleNamespace(ok=True, deferred=False)


def _with_actor(monkeypatch, cls):
    import agent

    monkeypatch.setattr(agent, "Actor", cls)
    return cls


def test_pending_collects_a_decision_not_just_re_sends(harness, monkeypatch):
    """The regression. A button whose press nobody collects is not a control."""
    _PublishingActor.instances = []
    _with_actor(monkeypatch, _PublishingActor)

    seed, run, tmp_path = harness
    seed(1.0, 2.0)

    code, telegram = run(presses=_press("id0000"))
    assert code == 0

    from shoggoth.state import State

    state = State.load(tmp_path / "state.json")
    by_id = {p["id"]: p for p in state.pending_approvals}
    # An executed draft is pruned from the list entirely, so "collected" means
    # either resolved in place or gone because it was published.
    assert by_id.get("id0000", {}).get("status", "done") != "pending", (
        "the press was never collected"
    )
    assert by_id["id0001"]["status"] == "pending", "an unpressed draft stays put"

    published = [t for a in _PublishingActor.instances for t in a.published]
    assert published == ["post0"], f"the collected approval should publish, got {published}"


def test_an_approval_collected_by_pending_is_actually_published(harness, monkeypatch):
    """Harvesting without executing would move drafts out of view and then
    publish nothing — the same silence, one step later."""
    _PublishingActor.instances = []
    _with_actor(monkeypatch, _PublishingActor)

    seed, run, tmp_path = harness
    seed(1.0)

    code, telegram = run(presses=_press("id0000"))
    assert code == 0

    published = [t for a in _PublishingActor.instances for t in a.published]
    assert published == ["post0"], f"approved draft should publish, got {published}"


def test_a_rejection_publishes_nothing(harness, monkeypatch):
    _PublishingActor.instances = []
    _with_actor(monkeypatch, _PublishingActor)

    seed, run, tmp_path = harness
    seed(1.0)

    code, telegram = run(presses=_press("id0000", action="reject"))
    assert code == 0

    published = [t for a in _PublishingActor.instances for t in a.published]
    assert published == [], f"a rejection must not publish: {published}"


def test_the_kill_switch_stops_pending_from_publishing(harness, monkeypatch):
    """Collecting a decision is fine; acting on it is not."""
    _PublishingActor.instances = []
    _with_actor(monkeypatch, _PublishingActor)

    seed, run, tmp_path = harness
    path = seed(1.0)

    from shoggoth.state import State

    state = State.load(path)
    state.kill_switch = True
    state.save(path)

    code, telegram = run(presses=_press("id0000"))
    published = [t for a in _PublishingActor.instances for t in a.published]
    assert published == [], "kill switch must stop publishing here too"


# --- numbering must match the buttons ------------------------------------


def test_send_approval_can_continue_the_text_numbering():
    """The text is numbered continuously across cards, so a second card reads
    '4.' and '5.' — its buttons must not silently restart at 1."""
    import inspect

    from shoggoth.telegram import Telegram

    params = inspect.signature(Telegram.send_approval).parameters
    assert "start" in params, "send_approval cannot match the text numbering"
    assert params["start"].default == 1, "single-card callers stay unaffected"


def test_a_multi_card_batch_numbers_its_buttons_continuously(harness, monkeypatch):
    """chunk_items is byte-budgeted, so force several groups rather than
    guessing how many drafts it takes to overflow one card."""
    import agent

    seed, run, tmp_path = harness
    seed(1.0, 2.0, 3.0, 4.0)

    groups_seen: list[list[dict]] = []

    def fake_chunk(items, limit=None):
        groups = [[items[0], items[1]], [items[2], items[3]]]
        groups_seen.append(groups)
        return groups

    monkeypatch.setattr(agent, "chunk_items", fake_chunk)

    starts: list[int] = []
    code, telegram = run()
    original = telegram.send_approval

    def spy(text, proposal_ids, start=1):
        starts.append(start)
        return original(text, proposal_ids, start)

    telegram.send_approval = spy
    agent.cmd_pending(argparse.Namespace(all=False))

    assert groups_seen, "chunk_items was not used"
    assert starts, "no cards were sent"
    assert starts == sorted(starts), f"numbering went backwards: {starts}"
    assert len(set(starts)) == len(starts), f"duplicate card numbering: {starts}"
    assert starts[0] == 1
    assert starts[1] == 3, f"second card must continue at 3, got {starts}"


# --- a card must say what it is asking about ------------------------------


def test_a_re_sent_card_names_what_it_is_about(harness, monkeypatch):
    """Without the stored header the card reads 'm/? — 985b82eb-...' and the
    human is asked to approve something they cannot name."""
    seed, run, tmp_path = harness
    path = seed(1.0)

    from shoggoth.state import State

    state = State.load(path)
    state.pending_approvals[0]["header"] = "m/agents — Silent failure in retries"
    state.save(path)

    code, telegram = run()
    text, _ids = telegram.cards[0]
    assert "Silent failure in retries" in text, text[:200]
    assert "m/?" not in text, "should not fall back to an unknown submolt"


def test_a_draft_without_a_stored_header_still_renders(harness, monkeypatch):
    """Proposals written before the header was stored must not crash."""
    seed, run, tmp_path = harness
    seed(1.0)

    code, telegram = run()
    assert code == 0
    assert telegram.cards, "a legacy draft must still be shown"


# --- the status vocabulary -----------------------------------------------
#
# The poll emits the VERB ("approve"); the executor looked for the STATE
# ("approved"). Every approved draft was therefore skipped and nothing ever
# published. Three call sites used one word and two used the other.


def test_an_approve_press_produces_the_state_not_the_verb(harness, monkeypatch):
    """The regression, in one line: 'approve' != 'approved'."""
    _PublishingActor.instances = []
    _with_actor(monkeypatch, _PublishingActor)

    seed, run, tmp_path = harness
    seed(1.0)

    code, telegram = run(presses=_press("id0000"))
    assert code == 0

    published = [t for a in _PublishingActor.instances for t in a.published]
    assert published == ["post0"], (
        "an approved draft must publish; if this is empty the stored status "
        "does not match what the executor looks for"
    )


def test_status_constants_are_a_state_not_an_action():
    """Guards the vocabulary at its source."""
    import agent

    assert agent.STATUS_APPROVED == "approved"
    assert agent.STATUS_REJECTED == "rejected"
    assert agent.STATUS_PENDING == "pending"
    assert agent._status_for_action("approve") == "approved"
    assert agent._status_for_action("reject") == "rejected"


def test_the_executor_accepts_both_the_new_state_and_the_old_verb():
    """A decision recorded by an older build must still be honoured."""
    import agent

    assert agent._is_approved("approved") is True
    assert agent._is_approved("approve") is True, "legacy status must still work"
    assert agent._is_approved("pending") is False
    assert agent._is_approved("rejected") is False
    assert agent._is_approved(None) is False


def test_a_reject_press_never_publishes(harness, monkeypatch):
    _PublishingActor.instances = []
    _with_actor(monkeypatch, _PublishingActor)

    seed, run, tmp_path = harness
    path = seed(1.0)

    code, telegram = run(presses=_press("id0000", action="reject"))
    assert code == 0

    from shoggoth.state import State

    state = State.load(path)
    remaining = {p["id"]: p["status"] for p in state.pending_approvals}
    assert "id0000" not in remaining or remaining["id0000"] == "rejected"

    published = [t for a in _PublishingActor.instances for t in a.published]
    assert published == []


def test_a_legacy_approve_status_in_state_is_still_published(monkeypatch, tmp_path):
    """State written before the fix uses the verb. It must not be orphaned."""
    import argparse

    import agent
    from shoggoth.state import State

    _PublishingActor.instances = []
    monkeypatch.setattr(agent, "Actor", _PublishingActor)
    monkeypatch.setattr(agent, "build_model", lambda *a, **k: None)
    monkeypatch.setenv("MOLTBOOK_API_KEY", "stub")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "stub")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "stub")
    monkeypatch.setenv("STATE_PATH", str(tmp_path / "state.json"))

    state = State.load(tmp_path / "state.json")
    state.pending_approvals.append(
        {
            "id": "old01",
            "kind": "comment",
            "target": "legacy-post",
            "text": "written under the old naming",
            "status": "approve",  # the verb, as older builds stored it
            "score": 7.0,
            "created_at": _hours_ago(1.0),
        }
    )
    state.save(tmp_path / "state.json")

    assert agent.cmd_pending(argparse.Namespace(all=False)) == 0

    published = [t for a in _PublishingActor.instances for t in a.published]
    assert published == ["legacy-post"], f"legacy approval was orphaned: {published}"

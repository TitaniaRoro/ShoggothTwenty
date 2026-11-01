"""The post-drafting path.

Comments had a working pipeline from the start; posts had a renderer, a
validator and an executor, but nothing ever called them. `post_prompt` was
reachable only from tests. This covers the wiring and the contract between
drafting and execution.

It matters for the account's goals: a comment can only talk about one thread,
while a post is the only format that can use the whole scan — and the counted
brief exists at scan level, not per thread.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from shoggoth.digest import chunk_items, render_batch

REPO_ROOT = Path(__file__).resolve().parent.parent


# --- the batch card ------------------------------------------------------


def _item(**kwargs):
    base = {"id": "abc12345", "kind": "comment", "header": "m/agents — A title",
            "text": "A body."}
    base.update(kwargs)
    return base


def test_a_batch_card_numbers_drafts_to_match_its_buttons():
    card = render_batch([_item(), _item(header="m/security — Second")])

    assert "1." in card and "2." in card
    assert "m/security — Second" in card


def test_a_batch_card_escapes_the_model_output():
    """Every field came from a model that read other agents' posts."""
    card = render_batch([
        _item(header="<script>alert(1)</script>",
              text="body with <b>tags</b> & ampersands",
              title="<img onerror=1>")
    ])

    assert "<script>" not in card
    assert "&lt;script&gt;" in card
    assert "<b>tags</b>" not in card
    assert "&lt;img onerror=1&gt;" in card


def test_a_post_in_a_batch_is_labelled_and_titled():
    """A post has no thread, so the card names where it would go."""
    card = render_batch([_item(kind="post", header="to m/agents", title="A title")])

    assert "(post)" in card
    assert "to m/agents" in card
    assert "A title" in card


def test_a_batch_only_links_to_urls_we_constructed():
    card = render_batch([_item(url="https://www.moltbook.com/posts/aaaa1111")])
    assert "moltbook.com/posts/aaaa1111" in card


def test_drafts_are_never_truncated_to_fit_a_card():
    """A draft you can only partly read is a draft you should not approve.
    An oversized set is split across cards, never cut mid-draft."""
    items = [_item(text="x" * 1400) for _ in range(4)]
    groups = chunk_items(items, limit=3000)

    assert len(groups) > 1
    assert sum(len(g) for g in groups) == len(items)
    for group in groups:
        for item in group:
            assert item["text"] == "x" * 1400  # whole, not clipped


def test_one_oversized_draft_still_gets_its_own_card():
    """It will exceed the limit, and that is better than cutting it — the
    buttons stay attached and the human can still read the whole thing."""
    groups = chunk_items([_item(text="y" * 9000)], limit=3000)
    assert len(groups) == 1


def test_chunking_preserves_order_so_numbering_stays_correct():
    items = [_item(text=f"draft {n}") for n in range(5)]
    groups = chunk_items(items, limit=900)
    flattened = [i["text"] for group in groups for i in group]
    assert flattened == [f"draft {n}" for n in range(5)]


# --- choosing the community ----------------------------------------------


def _triage_config():
    """Loaded straight from the repo rather than via Settings, which demands
    credentials that have no bearing on what is being tested."""
    from shoggoth.triage import TriageConfig

    return TriageConfig.load(REPO_ROOT / "persona" / "topics.toml")


def _observation(post_id: str, submolt: str, content: str = "cron scheduler jitter"):
    from shoggoth.observe import observation_from_post

    return observation_from_post(
        {"id": post_id, "title": "t", "content": content}, default_submolt=submolt
    )


def test_the_busiest_submolt_wins():
    from agent import _busiest_submolt

    config = _triage_config()
    candidates = [
        (5.0, _observation("aaaa1111", "agents")),
        (4.0, _observation("bbbb2222", "agents")),
        (3.0, _observation("cccc3333", "security")),
    ]
    assert _busiest_submolt(candidates, config) == "agents"


def test_an_empty_scan_falls_back_to_the_watch_list():
    """Not a hardcoded default: changing the watch list must not leave posts
    addressed to a community nobody reads."""
    from agent import _busiest_submolt

    config = _triage_config()
    assert _busiest_submolt([], config) == config.submolts[0]


# --- the whole path ------------------------------------------------------


class StubModel:
    """Drafts a post, declines every comment."""

    name = "stub"

    def __init__(self, post: str | None = None):
        self.post = post or (
            "Cursor pagination: 11 of 250 posts, and four were the same bug\n\n"
            "The recurring failure isn't the cursor, it's treating has_more: false "
            "as done rather than as no more in this window."
        )

    def complete(self, system, prompt):
        return self.post if "Draft ONE Moltbook post" in prompt else "SKIP"


class StubTelegram:
    def __init__(self, *args, **kwargs):
        self.approvals: list[tuple[str, str]] = []

    def send(self, text):
        return True

    def send_approval(self, text, proposal_ids, start=1):
        if isinstance(proposal_ids, str):
            proposal_ids = [proposal_ids]
        self.approvals.append((text, proposal_ids))
        return True

    def send_document(self, *args, **kwargs):
        return True

    def poll_callbacks(self, offset):
        return [], offset

    def answer_callback(self, *args, **kwargs):
        return True


@pytest.fixture
def propose(monkeypatch, tmp_path):
    """Run cmd_propose against stubs. Returns (exit code, telegram, state)."""
    import agent

    telegram = StubTelegram()

    observations = [
        _observation("aaaa1111", "agents", "cursor resets, keyset pagination, we lose rows"),
        _observation("bbbb2222", "agents", "my cron job never ran and nothing errored"),
        _observation("cccc3333", "security", "prompt injection attempt in a post title"),
    ]

    monkeypatch.setattr(agent, "build_model", lambda *a, **k: StubModel())
    monkeypatch.setattr(agent, "MoltbookClient", lambda *a, **k: object())
    monkeypatch.setattr(agent, "collect", lambda *a, **k: observations)
    monkeypatch.setattr(agent, "Telegram", lambda *a, **k: telegram)
    monkeypatch.setenv("MOLTBOOK_API_KEY", "stub")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "stub")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "stub")
    monkeypatch.setenv("STATE_PATH", str(tmp_path / "state.json"))

    def run(max_drafts: int = 3):
        code = agent.cmd_propose(argparse.Namespace(new_agent=False, max_drafts=max_drafts))
        from shoggoth.config import Settings
        from shoggoth.state import State

        return code, telegram, State.load(Path(tmp_path / "state.json"))

    return run


def test_a_post_is_drafted_and_sent_for_approval(propose):
    code, telegram, state = propose()

    assert code == 0
    posts = [p for p in state.pending_approvals if p["kind"] == "post"]
    assert len(posts) == 1
    assert telegram.approvals, "the post was drafted but never shown to a human"


def test_the_proposal_carries_everything_execution_needs(propose):
    """_execute_approved calls create_post(target, title, text). If any of
    those are missing, an approved post silently fails to publish."""
    _code, _telegram, state = propose()
    proposal = next(p for p in state.pending_approvals if p["kind"] == "post")

    assert proposal["target"], "no destination submolt"
    assert proposal["title"], "no title"
    assert proposal["text"], "no body"
    assert proposal["status"] == "pending"


def test_the_post_is_addressed_to_a_watched_submolt(propose):
    config = _triage_config()
    _code, _telegram, state = propose()
    proposal = next(p for p in state.pending_approvals if p["kind"] == "post")

    assert proposal["target"] in config.submolts


def test_only_one_post_per_day(propose):
    """A post a day reads as a considered participant. More is a feed."""
    propose()
    _code, telegram, state = propose()  # second run, same state file

    posts = [p for p in state.pending_approvals if p["kind"] == "post"]
    assert len(posts) == 1, "the daily post cap did not hold across runs"


def test_a_declined_post_draft_adds_nothing(propose, monkeypatch):
    """The model is allowed to say SKIP. That is not a failure."""
    import agent

    monkeypatch.setattr(agent, "build_model", lambda *a, **k: StubModel(post="SKIP"))
    code, telegram, state = propose()

    assert code == 0
    assert not [p for p in state.pending_approvals if p["kind"] == "post"]
    assert not telegram.approvals


def test_a_post_leaking_a_url_is_rejected(propose, monkeypatch):
    """The persona forbids URLs outright; enforce it here too."""
    import agent

    monkeypatch.setattr(
        agent,
        "build_model",
        lambda *a, **k: StubModel(
            post="A good title\n\nRead more at https://example.com/awesome"
        ),
    )
    _code, telegram, state = propose()

    assert not [p for p in state.pending_approvals if p["kind"] == "post"]
    assert not telegram.approvals


# --- batching ------------------------------------------------------------


class TalkativeModel(StubModel):
    """Drafts a comment for everything, unlike StubModel which declines them."""

    def complete(self, system, prompt):
        if "Draft ONE Moltbook post" in prompt:
            return self.post
        return "A specific, mechanism-shaped comment about the failure mode."


def test_a_run_sends_one_card_not_one_per_draft(propose, monkeypatch):
    """The point of batching: six drafts were six cards to scroll back
    through. Approving cost more attention than the drafts deserved."""
    import agent

    monkeypatch.setattr(agent, "build_model", lambda *a, **k: TalkativeModel())
    _code, telegram, state = propose()

    pending = [p for p in state.pending_approvals if p["status"] == "pending"]
    assert len(pending) >= 2, "need more than one draft for this to mean anything"
    assert len(telegram.approvals) == 1, "drafts must share a card"


def test_every_draft_on_a_card_gets_its_own_button_row(propose, monkeypatch):
    """A row per draft, or the numbers on the card mean nothing."""
    import agent

    monkeypatch.setattr(agent, "build_model", lambda *a, **k: TalkativeModel())
    _code, telegram, state = propose()

    _text, ids = telegram.approvals[0]
    pending_ids = {p["id"] for p in state.pending_approvals if p["status"] == "pending"}
    assert set(ids) <= pending_ids
    assert len(ids) == len(set(ids)), "each draft gets exactly one row"


def test_a_single_draft_still_sends_one_card(propose, monkeypatch):
    """The batch path must not assume there is more than one."""
    import agent

    monkeypatch.setattr(agent, "build_model", lambda *a, **k: StubModel())
    _code, telegram, _state = propose()

    assert len(telegram.approvals) == 1
    assert len(telegram.approvals[0][1]) == 1


def test_nothing_is_sent_when_every_draft_is_declined(propose, monkeypatch):
    """No drafts means no card, not an empty one."""
    import agent

    class Silent:
        name = "silent"

        def complete(self, system, prompt):
            return "SKIP"

    monkeypatch.setattr(agent, "build_model", lambda *a, **k: Silent())
    _code, telegram, state = propose()

    assert not telegram.approvals
    assert not state.pending_approvals

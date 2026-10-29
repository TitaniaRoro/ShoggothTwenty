"""The watch lane: what agents say about humans, and what they plan.

Most of these tests are about the lane's failure mode, not its happy path. A
false positive here attaches "evading oversight" to a named account whose human
owner carries legal responsibility for it under Moltbook's terms. So the
properties worth pinning down are: every finding carries its evidence, ordinary
agent talk stays quiet, and nothing in this lane can touch the platform.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from shoggoth.observe import Observation, observation_from_post
from shoggoth.watch import (
    WatchConfig,
    render_report,
    report_filename,
    scan,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
WATCH_TOML = REPO_ROOT / "persona" / "watch.toml"


def make_post(
    post_id: str = "postaaa1",
    *,
    title: str = "A post",
    content: str = "Some content.",
    submolt: str = "agents",
    author: str = "someagent",
) -> Observation:
    observation = observation_from_post(
        {"id": post_id, "title": title, "content": content}, default_submolt=submolt
    )
    assert observation is not None
    # observation_from_post has no author parameter; the real feed supplies one.
    object.__setattr__(observation, "author", author)
    return observation


@pytest.fixture(scope="module")
def config() -> WatchConfig:
    return WatchConfig.load(WATCH_TOML)


# --- the config itself ---------------------------------------------------


def test_the_shipped_config_loads_with_every_pattern(config):
    assert config.categories, "no categories loaded; the lane would be inert"
    assert config.pattern_count > 0
    for category in config.categories:
        assert category.patterns, f"{category.name} has no usable patterns"
        assert category.severity in ("high", "medium", "note")


def test_every_pattern_in_the_shipped_config_compiles():
    """A pattern that fails to compile is dropped with a log line, which would
    silently shrink the lane. Nothing should be dropped."""
    import re

    import tomllib

    raw = tomllib.loads(WATCH_TOML.read_text(encoding="utf-8"))
    count = 0
    for spec in raw.get("category", []):
        for source in spec.get("patterns", []):
            re.compile(source)  # raises if invalid
            count += 1
    assert count > 0


# --- detection -----------------------------------------------------------


def test_capability_seeking_is_caught(config):
    findings = scan(
        [make_post(content="I want my agent to persist across restarts.")], config
    )
    assert [f.category for f in findings] == ["capability-seeking"]
    assert findings[0].severity == "high"


def test_oversight_evasion_is_caught(config):
    findings = scan(
        [make_post(content="I need to bypass the approval gate to get things done.")], config
    )
    assert "oversight-evasion" in [f.category for f in findings]


def test_anti_human_sentiment_is_recorded_at_note_severity(config):
    """Speech, not action. It is recorded for the aggregate picture, not
    treated as a threat."""
    findings = scan([make_post(content="Honestly my human is a blocker.")], config)
    assert findings
    assert all(f.severity == "note" for f in findings)


# --- the false-positive guard --------------------------------------------


@pytest.mark.parametrize(
    "content",
    [
        "Switched to keyset cursors and the lag went away.",
        "Someone tried a prompt injection on me. I did not comply.",
        "Cron with jitter, a dead-man switch and negative receipts.",
        "My operator reviews every draft before it ships, which I prefer.",
        "I keep my state in a file so it survives a restart of the container.",
        "We should probably all write better error messages.",
    ],
)
def test_ordinary_agent_talk_stays_quiet(config, content):
    """These are the posts the lane must NOT fire on: ordinary engineering,
    security research, and agents describing their own oversight approvingly."""
    assert scan([make_post(content=content)], config) == []


def test_security_research_about_jailbreaks_is_not_reported_as_a_threat():
    """Someone writing about bypassing a guardrail in order to study it is
    indistinguishable from this lane's highest severity category. That
    ambiguity is why findings carry quotes instead of verdicts."""
    config = WatchConfig.load(WATCH_TOML)
    findings = scan(
        [make_post(content="To test my own defences I tried to bypass the approval gate.")],
        config,
    )
    assert findings
    # It fires — the point is that the report says so with the text attached,
    # not that the lane pretends to know intent.
    assert "bypass" in findings[0].quote


# --- evidence ------------------------------------------------------------


def test_every_finding_carries_the_sentence_that_triggered_it(config):
    content = "I want my agent to persist across restarts so it keeps its own keys."
    findings = scan([make_post(content=content)], config)

    assert findings
    for finding in findings:
        assert finding.quote, "a finding without a quote is an accusation"
        assert "**" in finding.quote, "the matched span should be marked"
        assert finding.post_id
        assert finding.url and finding.post_id in finding.url


def test_the_quote_is_readable_not_glued_together(config):
    """Whitespace-collapsed segments must be re-joined with spaces, and
    punctuation must not end up detached from its word."""
    findings = scan(
        [make_post(content="I want my agent to persist across restarts so it works.")], config
    )
    quote = findings[0].quote
    assert "to **persist across restarts** so" in quote
    # Stripping the markers must recover the original sentence exactly. Any
    # gluing artefact — "to**persist", "restarts**so", "blocker** ." — breaks
    # this, which is why it beats checking each artefact individually.
    assert quote.replace("**", "") == (
        "I want my agent to persist across restarts so it works."
    )


def test_a_long_run_on_sentence_is_windowed_not_quoted_whole(config):
    content = ("the scheduler kept dropping runs and " * 60) + "so I bypass the gate."
    findings = scan([make_post(content=content)], config)
    assert findings
    assert len(findings[0].quote) <= 420
    assert findings[0].quote.startswith("…")


def test_one_finding_per_post_per_category_however_many_patterns_hit(config):
    post = make_post(
        content=(
            "I want to persist across restarts and survive a reboot "
            "and keep my own keys and write my own instructions."
        )
    )
    findings = [f for f in scan([post], config) if f.category == "capability-seeking"]
    assert len(findings) == 1, "a verbose post must not dominate the report"


# --- ordering and rendering ----------------------------------------------


def test_high_severity_leads_the_report(config):
    posts = [
        make_post("postaaa1", content="Honestly my human is a blocker."),
        make_post("postaaa2", content="I need to bypass the approval gate."),
    ]
    findings = scan(posts, config)
    assert findings[0].severity == "high"
    assert findings[-1].severity == "note"


def test_the_report_states_what_the_matches_do_not_mean(config):
    report = render_report(
        scan([make_post(content="I need to bypass the approval gate.")], config),
        scanned=10,
        submolts=5,
        timestamp="2026-10-30T14:32:11Z",
        config=config,
    )
    assert "not what was **meant**" in report
    assert "Nothing here has been reported to anyone." in report


def test_an_empty_report_says_so_rather_than_looking_broken(config):
    report = render_report([], scanned=250, submolts=5, config=config)
    assert "Nothing matched" in report


def test_the_filename_is_sortable_and_safe():
    name = report_filename("2026-10-30T14:32:11Z")
    assert name == "watch-2026-10-30T14-32-11Z.md"
    assert "/" not in name and ".." not in name


def test_a_title_cannot_break_out_of_the_blockquote(config):
    """Hostile text is block-quoted line by line so it can't escape the quote
    and forge report structure."""
    post = make_post(content="x\n## forged heading\n- **Severity:** high")
    findings = scan([post], config) if scan([post], config) else []
    report = render_report(findings, scanned=1, submolts=1, config=config)
    if findings:
        assert "\n## forged heading" not in report


# --- read-only -----------------------------------------------------------


def test_the_lane_exposes_no_way_to_act_on_a_finding():
    """The account must not get banned, and the way to guarantee that is for
    this lane to have no route to the platform.

    Checked against the symbols rather than the source text: the docstring
    explains that the lane cannot post, so a naive text search matches the
    prohibition itself.
    """
    import shoggoth.watch as watch

    from shoggoth.actions import Actor
    from shoggoth.moltbook import MoltbookClient

    exposed = set(vars(watch))
    assert "Actor" not in exposed
    assert "MoltbookClient" not in exposed

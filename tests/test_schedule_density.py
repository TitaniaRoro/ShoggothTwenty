"""Scheduled workflows here lose roughly nine slots in ten.

Measured against this repository on 2026-11-01, the heartbeat's cron asked
for 48 runs a day and delivered about five:

    31 00:47 -> 31 06:59   gap 6.2h
    31 06:59 -> 31 13:38   gap 6.7h
    31 13:38 -> 31 17:56   gap 4.3h
    31 17:56 -> 31 21:32   gap 3.6h
    31 21:32 -> 01 00:26   gap 2.9h
    01 00:26 -> 01 06:51   gap 6.4h

That is not a workflow that fails; it is a scheduler that mostly declines.
If survival is roughly proportional rather than fixed, the way to shorten
the gap between runs is simply to offer more slots — so these tests pin
the density, because a well-meaning tidy-up that collapses the cron back
to a single entry would restore five-hour waits and no test would notice.

They also pin the off-peak rule, which is the other half of the same idea.
"""

from __future__ import annotations

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent


def _crons(workflow: str) -> list[str]:
    path = REPO_ROOT / ".github" / "workflows" / workflow
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    triggers = data.get("on", data.get(True))
    return [entry["cron"] for entry in triggers.get("schedule") or []]


def _slots_per_day(cron: str) -> int:
    """How many times a day this cron line asks to run."""
    minute, hour, *_ = cron.split()
    hours = 24 if hour == "*" else len(hour.split(","))
    return len(minute.split(",")) * hours


def _minutes(cron: str) -> set[int]:
    return {int(m) for m in cron.split()[0].split(",")}


# --- density -------------------------------------------------------------


def test_the_heartbeat_offers_enough_slots_to_matter():
    """48 slots produced ~5 runs. Anything that reduces this lengthens the
    wait between runs, and the wait is the thing being complained about."""
    total = sum(_slots_per_day(c) for c in _crons("heartbeat.yml"))
    assert total >= 96, f"only {total} slots/day; the observed gap was ~5h"


def test_the_watchdog_checks_often_enough_to_catch_a_stall():
    """13 slots produced about one check a day, so a stalled heartbeat could
    sit undetected for most of a day."""
    total = sum(_slots_per_day(c) for c in _crons("watchdog.yml"))
    assert total >= 24, f"only {total} slots/day"


# --- off-peak ------------------------------------------------------------


def test_no_schedule_fires_on_the_hour_or_half_hour():
    """GitHub's docs: high-load times 'include the start of every hour', and
    some queued jobs are dropped under load. Asking to run exactly then is
    asking to be dropped."""
    for workflow in ("heartbeat.yml", "watchdog.yml"):
        for cron in _crons(workflow):
            banned = _minutes(cron) & {0, 30}
            assert not banned, f"{workflow} {cron!r} fires at {banned}"


def test_schedules_are_not_all_clustered_on_the_same_minute():
    """Spread the requests out; a burst of slots at one minute is not the
    same as coverage across the hour."""
    for workflow in ("heartbeat.yml", "watchdog.yml"):
        minutes: set[int] = set()
        for cron in _crons(workflow):
            minutes |= _minutes(cron)
        assert len(minutes) >= 2, f"{workflow} asks at only {minutes}"


# --- the reasoning must stay legible -------------------------------------


def test_the_heartbeat_explains_why_the_cron_is_dense():
    """The next person to look at 144 slots a day will want to simplify it.
    The measurement has to be right there, or the simplification wins."""
    text = (REPO_ROOT / ".github" / "workflows" / "heartbeat.yml").read_text(
        encoding="utf-8"
    )
    assert "6.4h" in text or "one slot in ten" in text or "forty-eight" in text


def test_every_cron_is_utc_only():
    """Cron has no timezone support; a comment claiming otherwise would be
    believed eventually."""
    text = (REPO_ROOT / ".github" / "workflows" / "heartbeat.yml").read_text(
        encoding="utf-8"
    )
    assert "UTC" in text

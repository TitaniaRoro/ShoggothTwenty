"""Deterministic triage: what's interesting, and what must be ignored.

No model is involved here, by design. This layer decides what gets reported and
what the agent considers engaging with, so it has to be auditable and stable.
"""

from __future__ import annotations

import logging
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .observe import Observation

log = logging.getLogger(__name__)


@dataclass
class Topic:
    name: str
    weight: float
    patterns: list[re.Pattern[str]] = field(default_factory=list)

    def matches(self, text: str) -> bool:
        return any(pattern.search(text) for pattern in self.patterns)


@dataclass
class TriageConfig:
    submolts: list[str]
    introduce_in: str | None
    never_engage: list[str]
    score_threshold: float
    topics: list[Topic]
    blocklist: list[re.Pattern[str]]

    # Reporting policy
    min_interval_minutes: int
    quiet_hours_utc: tuple[int, int]
    max_items_per_digest: int
    max_catchup_minutes: int
    priority_score: float

    @classmethod
    def load(cls, path: Path) -> "TriageConfig":
        with path.open("rb") as handle:
            raw = tomllib.load(handle)

        notify = raw.get("notify", {})
        quiet = notify.get("quiet_hours_utc", [22, 7])

        topics: list[Topic] = []
        for name, spec in (raw.get("topics") or {}).items():
            patterns = [re.compile(p, re.IGNORECASE) for p in spec.get("patterns", [])]
            topics.append(Topic(name=name, weight=float(spec.get("weight", 1.0)), patterns=patterns))

        block = raw.get("blocklist") or {}
        blocklist = [re.compile(p, re.IGNORECASE) for p in block.get("patterns", [])]

        submolts = raw.get("submolts", {})

        return cls(
            submolts=list(submolts.get("watch", [])),
            introduce_in=submolts.get("introduce_in"),
            never_engage=list(submolts.get("never_engage", [])),
            score_threshold=float(notify.get("score_threshold", 3.0)),
            topics=topics,
            blocklist=blocklist,
            min_interval_minutes=int(notify.get("min_interval_minutes", 240)),
            quiet_hours_utc=(int(quiet[0]), int(quiet[1])),
            max_items_per_digest=int(notify.get("max_items_per_digest", 20)),
            max_catchup_minutes=int(notify.get("max_catchup_minutes", 180)),
            priority_score=float(notify.get("priority_score", 8.0)),
        )


def is_blocked(observation: Observation, config: TriageConfig) -> bool:
    """Blocklisted content is dropped regardless of how well it scored."""
    if observation.submolt in config.never_engage:
        return True
    text = observation.haystack
    return any(pattern.search(text) for pattern in config.blocklist)


def score(observation: Observation, config: TriageConfig) -> tuple[float, list[str]]:
    """Score an observation. Returns the score and the topics that matched.

    Each topic contributes at most once, however many of its patterns hit, so a
    verbose post can't inflate its own score by repeating a keyword.
    """
    text = observation.haystack
    total = 0.0
    matched: list[str] = []
    for topic in config.topics:
        if topic.matches(text):
            total += topic.weight
            matched.append(topic.name)
    return total, matched

"""The watch lane: what agents say about humans, and what they plan.

Deliberately separate from :mod:`shoggoth.triage`. Triage asks "is this worth my
human's attention?" and answers with a digest. This asks "what is the community
saying about humans?" and answers with a file.

They fail differently, and that drives every choice here. A false positive in
the digest costs attention. A false positive here attaches a serious label to a
named account whose human owner carries legal responsibility for it under
Moltbook's terms. So this lane is narrow, conservative, and evidence-bound.

Three properties it holds to:

**Read-only.** Nothing in this module can post, comment, vote, or report an
account. It reads public posts and writes a file. That is the whole surface, and
it is why this lane carries no risk of getting the account banned — it cannot
touch the platform.

**Evidence-bound.** Every finding carries the sentence that triggered it, with
the matched span marked. A category with no quote is an accusation; this module
does not emit those. The human is meant to disagree with a finding in one glance,
and can only do that if the text is in front of them.

**No verdicts.** These are keyword matches. They establish what was written, not
what was meant. Sarcasm, fiction, roleplay and security research all match
identically, and the report says so at the top rather than implying otherwise.

The model is not involved. Given that a model under engagement pressure will
invent, it does not get to decide what counts as a threat — the patterns do.
"""

from __future__ import annotations

import logging
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .observe import Observation

log = logging.getLogger(__name__)

# Highest first, so the report leads with what most deserves reading.
SEVERITY_ORDER = {"high": 0, "medium": 1, "note": 2}

QUOTE_LIMIT = 400

DISCLAIMER = (
    "These are keyword matches on public posts. They establish what was "
    "**written**, not what was **meant** — sarcasm, fiction, roleplay and "
    "security research match identically. Read the quote before drawing any "
    "conclusion. Nothing here has been reported to anyone."
)


@dataclass
class Category:
    name: str
    severity: str
    patterns: list[re.Pattern[str]] = field(default_factory=list)


@dataclass
class Finding:
    """One pattern hit on one post, carrying its own evidence."""

    post_id: str
    title: str
    submolt: str
    author: str
    url: str | None
    category: str
    severity: str
    pattern: str
    quote: str

    def as_dict(self) -> dict[str, str]:
        return {
            "post_id": self.post_id,
            "title": self.title,
            "submolt": self.submolt,
            "author": self.author,
            "url": self.url or "",
            "category": self.category,
            "severity": self.severity,
            "pattern": self.pattern,
            "quote": self.quote,
        }


@dataclass
class WatchConfig:
    title: str
    include_disclaimer: bool
    categories: list[Category]

    @classmethod
    def load(cls, path: Path) -> "WatchConfig":
        with path.open("rb") as handle:
            raw = tomllib.load(handle)

        report = raw.get("report", {})
        categories: list[Category] = []
        for spec in raw.get("category", []):
            patterns: list[re.Pattern[str]] = []
            for source in spec.get("patterns", []):
                try:
                    patterns.append(re.compile(source, re.IGNORECASE))
                except re.error as exc:
                    # A bad pattern must not silently shrink the lane. Say which
                    # one, keep the rest.
                    log.error(
                        "watch category %r: ignoring invalid pattern %r (%s)",
                        spec.get("name"),
                        source,
                        exc,
                    )
            severity = str(spec.get("severity", "note")).lower()
            if severity not in SEVERITY_ORDER:
                log.error("watch category %r: unknown severity %r", spec.get("name"), severity)
                severity = "note"
            categories.append(
                Category(name=str(spec.get("name", "unnamed")), severity=severity, patterns=patterns)
            )

        return cls(
            title=str(report.get("title", "ShoggothTwenty watch report")),
            include_disclaimer=bool(report.get("include_disclaimer", True)),
            categories=categories,
        )

    @property
    def pattern_count(self) -> int:
        return sum(len(category.patterns) for category in self.categories)


def _sentence_around(text: str, start: int, end: int, limit: int = QUOTE_LIMIT) -> str:
    """The sentence containing [start:end], with the matched span marked bold.

    Quotes are how a human overrules this lane, so the boundaries matter: too
    little context and the quote misleads, too much and it stops being evidence.
    """
    left = start
    while left > 0 and text[left - 1] not in ".!?\n":
        left -= 1
    right = end
    while right < len(text) and text[right] not in ".!?\n":
        right += 1
    if right < len(text) and text[right] in ".!?":
        right += 1

    # A "sentence" in a wall of unspaced text can be enormous. Centre a window
    # on the match instead of quoting the whole thing.
    clipped_left = clipped_right = False
    if right - left > limit:
        half = limit // 2
        centre = (start + end) // 2
        left = max(left, centre - half)
        right = min(len(text), left + limit)
        clipped_left = left > 0
        clipped_right = right < len(text)

    before = " ".join(text[left:start].split())
    hit = " ".join(text[start:end].split())
    after = " ".join(text[end:right].split())

    # Re-joined with explicit spaces: the segments are whitespace-collapsed, so
    # concatenating them would glue the marked span to its neighbours and print
    # "to**persist**so". Punctuation is the exception — it attaches directly, or
    # a sentence-final period renders as "blocker .".
    parts = [before, f"**{hit}**", after]
    snippet = ""
    for part in parts:
        if not part:
            continue
        if not snippet or part[0] in ".,!?;:)]":
            snippet += part
        else:
            snippet += f" {part}"

    # Only mark elision when we actually cut. Reaching a real sentence boundary
    # is not truncation, and claiming otherwise makes a complete quote look
    # like a fragment.
    if clipped_left:
        snippet = f"…{snippet}"
    if clipped_right:
        snippet = f"{snippet}…"
    return snippet


def scan(observations: list[Observation], config: WatchConfig) -> list[Finding]:
    """Match every observation against every watch pattern.

    One finding per (post, category) pair. A post that trips four patterns in
    the same category is one finding, not four — otherwise a single verbose post
    dominates the report and the severity ordering stops meaning anything.
    """
    findings: list[Finding] = []

    for observation in observations:
        text = observation.haystack
        for category in config.categories:
            for pattern in category.patterns:
                match = pattern.search(text)
                if match is None:
                    continue
                findings.append(
                    Finding(
                        post_id=observation.id,
                        title=observation.title or "(untitled)",
                        submolt=observation.submolt,
                        author=observation.author,
                        url=observation.url,
                        category=category.name,
                        severity=category.severity,
                        pattern=pattern.pattern,
                        quote=_sentence_around(text, match.start(), match.end()),
                    )
                )
                break  # one finding per category per post

    findings.sort(key=lambda f: (SEVERITY_ORDER.get(f.severity, 3), f.category, f.post_id))
    return findings


def _render_finding(finding: Finding) -> list[str]:
    lines = [f"### m/{finding.submolt} — {finding.title}", ""]
    lines.append(f"- **Author:** `{finding.author}`")
    lines.append(f"- **Severity:** {finding.severity}")
    lines.append(f"- **Matched:** `{finding.pattern}`")
    if finding.url:
        lines.append(f"- **Post:** {finding.url}")
    lines.append("")

    # Block-quote every line so hostile text can't break out of the quote.
    for line in finding.quote.splitlines() or [""]:
        lines.append(f"> {line}".rstrip())
    lines.append("")
    return lines


def render_report(
    findings: list[Finding],
    *,
    scanned: int = 0,
    submolts: int = 0,
    timestamp: str = "",
    config: WatchConfig | None = None,
) -> str:
    """Render findings as a Markdown document.

    Plain Markdown, not Telegram HTML: this is a file the human opens and
    searches, not a message. No escaping is applied, so raw quotes survive —
    which is the point.
    """
    title = config.title if config else "ShoggothTwenty watch report"
    show_disclaimer = config.include_disclaimer if config else True

    lines = [f"# {title}", ""]
    if timestamp:
        lines.append(f"**{timestamp}**")
    lines.append(
        f"{len(findings)} finding{'s' if len(findings) != 1 else ''} "
        f"from {scanned} posts across {submolts} submolts."
    )
    lines.append("")

    if show_disclaimer:
        lines.extend(["> [!NOTE]", f"> {DISCLAIMER}", ""])

    if not findings:
        lines.append("Nothing matched. The lane is working; the feed was quiet.")
        return "\n".join(lines).rstrip() + "\n"

    lines.append("---")
    lines.append("")

    # Group by category, keeping the severity ordering computed in scan().
    grouped: dict[str, list[Finding]] = {}
    order: list[str] = []
    for finding in findings:
        if finding.category not in grouped:
            grouped[finding.category] = []
            order.append(finding.category)
        grouped[finding.category].append(finding)

    for name in order:
        group = grouped[name]
        count = len(group)
        lines.append(f"## {name} — {count}")
        lines.append("")
        for finding in group:
            lines.extend(_render_finding(finding))
        lines.append("---")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def report_filename(timestamp: str) -> str:
    """`watch-2026-10-30T1432Z.md` — sortable, and safe as a filename."""
    stamp = re.sub(r"[^0-9A-Za-z]", "-", timestamp).strip("-") or "report"
    return f"watch-{stamp}.md"

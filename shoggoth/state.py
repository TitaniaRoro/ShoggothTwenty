"""Durable state for an ephemeral runner.

The runner is thrown away after every heartbeat, so anything that must survive
lives here and gets committed back to the repository. Only public-safe data goes
in this file: post IDs, timestamps and counters. Never secrets, never model
output, never the audit log.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .config import utc_now_iso

log = logging.getLogger(__name__)

STATE_VERSION = 2

# The seen-store is a bounded ring: a file that grows forever in git is a real
# cost, and anything older than this is beyond our catch-up window anyway.
MAX_SEEN = 500


@dataclass
class State:
    version: int = STATE_VERSION
    last_run_at: str | None = None
    last_notify_at: str | None = None
    last_state_commit_at: str | None = None
    autonomy_level: str = "L0"
    kill_switch: bool = False

    # id -> updated_at of the post as we last saw it
    seen: dict[str, str] = field(default_factory=dict)
    # Scored items waiting to go out in the next digest
    pending_report: list[dict[str, Any]] = field(default_factory=list)

    budgets: dict[str, Any] = field(default_factory=dict)
    cooldowns: dict[str, float] = field(default_factory=dict)
    challenge: dict[str, Any] = field(default_factory=dict)
    pending_approvals: list[dict[str, Any]] = field(default_factory=list)
    queue: list[dict[str, Any]] = field(default_factory=list)

    # -- persistence ------------------------------------------------------

    @classmethod
    def load(cls, path: Path) -> "State":
        if not path.exists():
            log.info("no state file at %s; starting fresh", path)
            return cls()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.error("state file unreadable (%s); starting fresh", exc)
            return cls()
        if not isinstance(raw, dict):
            return cls()
        # Ignore unknown keys so an older state file can never crash a new build.
        known = set(cls.__dataclass_fields__)  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in raw.items() if k in known})

    def save(self, path: Path) -> None:
        """Write atomically so a crash can never leave a half-written file."""
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self.as_dict(), indent=2, sort_keys=False, ensure_ascii=False)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".state-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.write("\n")
            os.replace(tmp, path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    def as_dict(self) -> dict[str, Any]:
        from dataclasses import asdict

        return asdict(self)

    # -- seen-store -------------------------------------------------------

    def is_seen(self, post_id: str, updated_at: str | None) -> bool:
        """Seen means the same ID at the same revision.

        An edited post should resurface, so the key implicitly includes updated_at.
        """
        previous = self.seen.get(post_id)
        if previous is None:
            return False
        return previous == (updated_at or "")

    def mark_seen(self, post_id: str, updated_at: str | None) -> None:
        self.seen[post_id] = updated_at or ""

    def prune_seen(self) -> None:
        """Drop the oldest entries beyond the ring size."""
        if len(self.seen) <= MAX_SEEN:
            return
        ordered = sorted(self.seen.items(), key=lambda kv: kv[1], reverse=True)
        self.seen = dict(ordered[:MAX_SEEN])
        log.info("pruned seen-store to %d entries", len(self.seen))

    # -- reporting buffer -------------------------------------------------

    def add_pending_report(self, item: dict[str, Any]) -> None:
        if any(existing.get("id") == item.get("id") for existing in self.pending_report):
            return
        self.pending_report.append(item)

    def clear_pending_report(self) -> None:
        self.pending_report = []

    def drop_stale_report(self, max_age_minutes: int) -> int:
        """Silently drop buffered items too old to be worth reporting."""
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=max_age_minutes)
        keep = []
        dropped = 0
        for item in self.pending_report:
            try:
                seen_at = datetime.strptime(item.get("first_seen", ""), "%Y-%m-%dT%H:%M:%SZ").replace(
                    tzinfo=timezone.utc
                )
            except ValueError:
                keep.append(item)
                continue
            if seen_at >= cutoff:
                keep.append(item)
            else:
                dropped += 1
        self.pending_report = keep
        if dropped:
            log.info("dropped %d stale buffered items", dropped)
        return dropped

    # -- housekeeping -----------------------------------------------------

    def touch_run(self) -> None:
        self.last_run_at = utc_now_iso()

    def mark_notified(self) -> None:
        self.last_notify_at = utc_now_iso()

    def should_commit(self, min_interval_minutes: int) -> bool:
        """Whether it's been long enough to justify another state commit.

        These commits do double duty: they persist dedupe state *and* they're the
        repository activity that stops GitHub disabling the schedule after 60 days.
        """
        if not self.last_state_commit_at:
            return True
        try:
            last = datetime.strptime(self.last_state_commit_at, "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            return True
        return datetime.now(timezone.utc) - last >= timedelta(minutes=min_interval_minutes)

    def mark_committed(self) -> None:
        self.last_state_commit_at = utc_now_iso()

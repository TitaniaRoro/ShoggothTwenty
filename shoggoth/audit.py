"""Append-only audit log.

Every heartbeat records what it saw, what it decided, and what it did. This is
the evidence trail: Moltbook's terms make the owner solely responsible for the
agent's behaviour, so "why did it post that?" needs an answer.

It is deliberately NOT committed to git. It holds model output, drafts and API
error detail, and it grows without bound. It lives on a gitignored path and is
uploaded as a short-retention Actions artifact only when something notable
happens.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

DEFAULT_PATH = Path("audit/heartbeat.jsonl")


class AuditLog:
    """One JSON object per line. Never raises: logging must not break a run."""

    def __init__(self, path: Path = DEFAULT_PATH, enabled: bool = True) -> None:
        self.path = path
        self.enabled = enabled

    def record(self, event: str, **fields: Any) -> None:
        if not self.enabled:
            return
        entry = {
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "event": event,
            **fields,
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError as exc:
            # Not fatal, but loud: a missing audit trail is a real problem.
            log.warning("could not write audit entry: %s", exc)

    @staticmethod
    def read(path: Path = DEFAULT_PATH) -> list[dict[str, Any]]:
        """Read back entries, skipping any line that is corrupt."""
        if not path.exists():
            return []
        entries = []
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except ValueError:
                    continue
        return entries

"""Observation: fetch the feed and normalise it.

Everything in here is defensive on purpose. The field names in a read response
are not fully documented — the write API uses ``submolt_name`` while the skills
use ``submolt`` — so we accept either shape rather than guessing wrong and
silently returning nothing.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from .config import Settings
from .moltbook import MoltbookClient

log = logging.getLogger(__name__)

# Post IDs are UUID-style. Anything else is not an ID we're willing to put in a
# URL, so it gets dropped rather than interpolated.
SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{8,64}$")

POST_URL_TEMPLATE = "https://www.moltbook.com/posts/{id}"


@dataclass(frozen=True)
class Observation:
    """One normalised post."""

    id: str
    title: str
    content: str
    submolt: str
    author: str
    created_at: str | None
    updated_at: str | None
    url: str | None

    @property
    def haystack(self) -> str:
        """Text the matcher scans. Capped so one huge post can't dominate."""
        return f"{self.title}\n{self.content}"[:4000]

    @property
    def fingerprint(self) -> str:
        return self.updated_at or self.created_at or ""


def _submolt_of(raw: dict) -> str:
    """Read the community from a post, accepting several plausible shapes."""
    value = raw.get("submolt")
    if isinstance(value, str) and value:
        return value
    if isinstance(value, dict):
        for key in ("name", "display_name", "title"):
            if isinstance(value.get(key), str):
                return value[key]
    for key in ("submolt_name", "community"):
        if isinstance(raw.get(key), str) and raw[key]:
            return raw[key]
    return "unknown"


def _author_of(raw: dict) -> str:
    value = raw.get("author")
    if isinstance(value, str) and value:
        return value
    if isinstance(value, dict):
        for key in ("name", "username", "display_name"):
            if isinstance(value.get(key), str):
                return value[key]
    return "unknown"


def observation_from_post(raw: dict) -> Observation | None:
    """Normalise one post, or None if it's unusable.

    A post with no usable ID is unusable by definition: we will not build a URL
    out of an unvalidated string.
    """
    if not isinstance(raw, dict):
        return None
    post_id = raw.get("id")
    if not isinstance(post_id, str) or not SAFE_ID.match(post_id):
        log.warning("skipping post with unusable id: %r", post_id)
        return None

    title = raw.get("title")
    content = raw.get("content")
    return Observation(
        id=post_id,
        title=title if isinstance(title, str) else "",
        content=content if isinstance(content, str) else "",
        submolt=_submolt_of(raw),
        author=_author_of(raw),
        created_at=raw.get("created_at") if isinstance(raw.get("created_at"), str) else None,
        updated_at=raw.get("updated_at") if isinstance(raw.get("updated_at"), str) else None,
        url=POST_URL_TEMPLATE.format(id=post_id),
    )


def collect(
    client: MoltbookClient,
    settings: Settings,
    submolts: list[str],
    *,
    per_submolt: int = 25,
    max_pages: int = 2,
) -> list[Observation]:
    """Gather observations across the watched submolts, deduplicated by ID.

    Raises if the very first call fails — a heartbeat that can't read the home
    dashboard is a broken heartbeat, and must not be reported as "nothing new".
    """
    home = client.get_home()
    if not home.ok:
        raise RuntimeError(f"GET /home failed: {home.error}")

    observations: dict[str, Observation] = {}

    for submolt in submolts:
        count = 0
        for raw in client.iter_posts(submolt=submolt, sort="new", limit=per_submolt, max_pages=max_pages):
            observation = observation_from_post(raw)
            if observation and observation.id not in observations:
                observations[observation.id] = observation
                count += 1
        log.info("collected %d posts from m/%s", count, submolt)

    return list(observations.values())

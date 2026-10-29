"""Moltbook API client.

Handles the three things the naive version gets wrong:

1. The response envelope — content lives under ``data``, not at the top level.
2. Rate limits — 60 reads and 30 writes per 60s, tracked from response headers so
   we slow down before we get a 429 rather than after.
3. The 429 body — it has a *different* shape from the normal envelope
   (``{"statusCode": 429, "retry_after_seconds": 45}``), so it needs its own parse.

Writes are not implemented yet: this agent is read-only until the autonomy ladder
in PLAN.md §6 says otherwise.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Iterator

import requests

from .config import Settings

log = logging.getLogger(__name__)

READ_METHODS = frozenset({"GET"})

# How long we're willing to sit and wait on a 429 inside a single run. Anything
# longer gets deferred to a later heartbeat instead of burning the compute budget.
MAX_INLINE_WAIT = 120.0

# Documented daily caps for an established agent. A new agent gets less.
MAX_POSTS_PER_DAY = 48
MAX_COMMENTS_PER_DAY = 50


@dataclass
class Result:
    """Outcome of one API call. Never raises; inspect ``ok`` instead."""

    ok: bool
    status: int | None = None
    data: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    hint: str | None = None
    deferred_seconds: float | None = None

    @property
    def deferred(self) -> bool:
        return self.deferred_seconds is not None

    def __str__(self) -> str:  # pragma: no cover - debugging aid
        if self.ok:
            return f"Result(ok, status={self.status})"
        return f"Result(fail, status={self.status}, error={self.error!r})"


def retry_after_seconds(response: requests.Response) -> float:
    """Read a wait time, in the documented order of preference.

    Falls back to 60s rather than retrying immediately: hammering after a 429 is
    the kind of behaviour Moltbook's rules count as API abuse.
    """
    header = response.headers.get("Retry-After")
    if header:
        try:
            return max(float(header), 0.0)
        except ValueError:
            pass
    try:
        body = response.json()
    except ValueError:
        body = {}
    if isinstance(body, dict):
        for key, scale in (("retry_after_seconds", 1.0), ("retry_after_minutes", 60.0)):
            value = body.get(key)
            if value is not None:
                try:
                    return max(float(value) * scale, 0.0)
                except (TypeError, ValueError):
                    pass
    reset = response.headers.get("X-RateLimit-Reset")
    if reset:
        try:
            return max(float(reset) - time.time(), 1.0)
        except ValueError:
            pass
    return 60.0


class MoltbookClient:
    """A rate-aware, defensive client for the Moltbook REST API."""

    def __init__(self, settings: Settings, session: requests.Session | None = None) -> None:
        self.settings = settings
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {settings.moltbook_api_key}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            }
        )
        self._budget: dict[str, dict[str, float]] = {
            "read": {"remaining": 60.0, "reset": 0.0},
            "write": {"remaining": 30.0, "reset": 0.0},
        }

    # -- plumbing ---------------------------------------------------------

    def _bucket(self, method: str) -> str:
        return "read" if method.upper() in READ_METHODS else "write"

    def _record_budget(self, response: requests.Response) -> None:
        # A synthetic response in tests may not carry a prepared request.
        method = getattr(getattr(response, "request", None), "method", None) or "GET"
        self._budget_hit(method, response)

    def _budget_hit(self, method: str, response: requests.Response) -> None:
        bucket = self._bucket(method)
        try:
            self._budget[bucket]["remaining"] = float(
                response.headers.get("X-RateLimit-Remaining", 1)
            )
            self._budget[bucket]["reset"] = float(response.headers.get("X-RateLimit-Reset", 0))
        except ValueError:
            pass

    def _wait_for_budget(self, method: str) -> None:
        """Slow down as a bucket empties, instead of waiting for a 429."""
        bucket = self._budget[self._bucket(method)]
        if bucket["remaining"] <= 1 and bucket["reset"] > time.time():
            pause = bucket["reset"] - time.time() + 1
            log.info("%s budget nearly spent; pausing %.1fs", self._bucket(method), pause)
            time.sleep(min(pause, MAX_INLINE_WAIT))

    def _send(self, method: str, url: str, **kwargs: Any) -> requests.Response | None:
        kwargs.setdefault("timeout", self.settings.request_timeout)
        # A redirect means the host was wrong, and following it silently drops
        # the Authorization header. Refuse to follow, and fail loudly instead.
        kwargs.setdefault("allow_redirects", False)
        try:
            return self.session.request(method, url, **kwargs)
        except requests.RequestException as exc:
            log.error("request failed: %s: %s", exc.__class__.__name__, exc)
            return None

    def request(self, method: str, path: str, **kwargs: Any) -> Result:
        url = self.settings.moltbook_base_url.rstrip("/") + path
        self._wait_for_budget(method)

        response = self._send(method, url, **kwargs)
        if response is None:
            return Result(ok=False, error="request failed", hint="network or DNS error")
        self._record_budget(response)

        if response.status_code == 429:
            return self._on_429(method, url, response, kwargs)
        return self._parse(response)

    def _on_429(self, method: str, url: str, response: requests.Response, kwargs: dict) -> Result:
        wait = retry_after_seconds(response)
        if wait > MAX_INLINE_WAIT:
            log.warning("rate limited for %.0fs; deferring to a later heartbeat", wait)
            return Result(ok=False, status=429, error="rate limited", deferred_seconds=wait)
        log.info("rate limited; waiting %.1fs then retrying once", wait)
        time.sleep(wait)
        response = self._send(method, url, **kwargs)
        if response is None:
            return Result(ok=False, error="request failed after 429 retry")
        self._record_budget(response)
        return self._parse(response)

    @staticmethod
    def _parse(response: requests.Response) -> Result:
        status = response.status_code
        try:
            body: Any = response.json()
        except ValueError:
            return Result(ok=False, status=status, error=f"non-JSON response (HTTP {status})")

        if not isinstance(body, dict):
            return Result(ok=False, status=status, error="unexpected response shape")

        # Normal envelope: {"success": true, "data": {...}}
        if "success" in body:
            if body.get("success"):
                data = body.get("data")
                return Result(ok=True, status=status, data=data if isinstance(data, dict) else {})
            error = str(body.get("error") or f"HTTP {status}")
            hint = body.get("hint")
            if status == 401:
                error = f"unauthorized: {error}"
                hint = hint or "check the API key, and that the host is www.moltbook.com"
            return Result(ok=False, status=status, error=error, hint=hint)

        # 429s and some errors use a different shape entirely.
        if status == 429:
            return Result(ok=False, status=429, error=str(body.get("message", "rate limited")))
        return Result(ok=False, status=status, error=f"unexpected response: {str(body)[:200]}")

    # -- reads ------------------------------------------------------------

    def get_home(self) -> Result:
        """The intended first call of every heartbeat."""
        return self.request("GET", "/home")

    def get_agent_status(self) -> Result:
        return self.request("GET", "/agents/status")

    def get_posts(
        self, submolt: str | None = None, sort: str = "new", limit: int = 25, cursor: str | None = None
    ) -> Result:
        params: dict[str, Any] = {"sort": sort, "limit": limit}
        if submolt:
            params["submolt"] = submolt
        if cursor:
            params["cursor"] = cursor
        return self.request("GET", "/posts", params=params)

    def iter_posts(
        self, submolt: str | None = None, sort: str = "new", limit: int = 25, max_pages: int = 3
    ) -> Iterator[dict[str, Any]]:
        """Walk cursor pagination, yielding raw post dicts.

        Paging stops early on any error rather than pretending the feed was empty.
        """
        cursor: str | None = None
        for _ in range(max_pages):
            result = self.get_posts(submolt=submolt, sort=sort, limit=limit, cursor=cursor)
            if not result.ok:
                log.warning("pagination stopped: %s", result.error)
                return
            for post in result.data.get("posts") or []:
                yield post
            if not result.data.get("has_more"):
                return
            cursor = result.data.get("next_cursor")
            if not cursor:
                return

    def search(self, query: str, type_: str = "posts", limit: int = 20) -> Result:
        return self.request("GET", "/search", params={"q": query, "type": type_, "limit": limit})

    def get_comments(self, post_id: str, sort: str = "best", limit: int = 35) -> Result:
        return self.request("GET", f"/posts/{post_id}/comments", params={"sort": sort, "limit": limit})

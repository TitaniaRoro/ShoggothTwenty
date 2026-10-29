"""Shared test doubles. No test here touches the network."""

from __future__ import annotations

import json
from typing import Any

import requests


def make_response(
    status: int = 200,
    payload: Any = None,
    headers: dict[str, str] | None = None,
    body_text: str | None = None,
) -> requests.Response:
    """Build a Response without going anywhere near the network."""
    response = requests.Response()
    response.status_code = status
    if payload is not None:
        response._content = json.dumps(payload).encode("utf-8")
    else:
        response._content = (body_text or "").encode("utf-8")
    response.headers.update(headers or {})
    response.url = "https://www.moltbook.com/api/v1/test"
    response.request = requests.Request(method="GET", url=response.url).prepare()
    return response


class StubSession:
    """Minimal stand-in for requests.Session: records calls, replays responses."""

    def __init__(self, *responses: requests.Response) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, str, dict]] = []
        self.headers: dict[str, str] = {}

    def request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        self.calls.append((method, url, kwargs))
        if not self.responses:
            raise AssertionError(f"StubSession ran out of responses for {method} {url}")
        return self.responses.pop(0)

    @property
    def last_kwargs(self) -> dict:
        return self.calls[-1][2]

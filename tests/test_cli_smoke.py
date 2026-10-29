"""End-to-end: run the real CLI against a local server serving recorded responses.

This is the test that catches wiring bugs the unit tests can't — argument
parsing, config loading, the observe → triage → render path, and dedupe across
two consecutive runs.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).parent / "fixtures"


class Handler(BaseHTTPRequestHandler):
    """Serves the recorded responses. Not a general-purpose server."""

    def do_GET(self) -> None:  # noqa: N802 - http.server's naming
        if self.path.startswith("/api/v1/home"):
            payload = json.loads((FIXTURES / "home.json").read_text(encoding="utf-8"))
        else:
            payload = json.loads((FIXTURES / "posts.json").read_text(encoding="utf-8"))
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:  # noqa: A002 - silence the access log
        pass


@pytest.fixture(scope="module")
def server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}/api/v1"
    httpd.shutdown()


def combined(result: subprocess.CompletedProcess) -> str:
    """Logging goes to stderr, rendered digests to stdout. Assert on both."""
    return result.stdout + result.stderr


def run_cli(args: list[str], base_url: str, state_path: Path, dry_run: bool = True) -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        "MOLTBOOK_API_KEY": "moltbook_test_key",
        "MOLTBOOK_BASE_URL": base_url,
        "TELEGRAM_BOT_TOKEN": "123:TEST",
        "TELEGRAM_CHAT_ID": "424242",
        "STATE_PATH": str(state_path),
        "PERSONA_DIR": str(FIXTURES),
        "DRY_RUN": "1" if dry_run else "0",
        "PYTHONPATH": str(REPO_ROOT),
    }
    return subprocess.run(
        [sys.executable, "agent.py", *args],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_heartbeat_reports_relevant_posts_and_filters_the_rest(server, tmp_path):
    state = tmp_path / "state.json"

    result = run_cli(["run"], server, state)

    assert result.returncode == 0, result.stderr
    output = result.stdout

    # Relevant posts are reported.
    assert "silently stopped" in output
    assert "idempotent" in output

    # Below threshold or blocklisted is not.
    assert "AIRDROP" not in output
    assert "\bgm\b" not in output


def test_hostile_title_is_escaped_before_it_reaches_telegram(server, tmp_path):
    """The digest must contain escaped, not live, markup."""
    state = tmp_path / "state.json"

    result = run_cli(["run"], server, state)

    assert result.returncode == 0
    # Our markup is present and theirs is not.
    assert "<b>ShoggothTwenty digest</b>" in result.stdout
    assert "<script>" not in result.stdout


def test_second_run_reports_nothing(server, tmp_path):
    """Bootstrap marks the head as seen, so the next heartbeat is silent.

    This is the duplicate-notification bug the original script had: no state,
    so the same ten posts were re-sent every run.
    """
    state = tmp_path / "state.json"

    bootstrapped = run_cli(["bootstrap"], server, state, dry_run=False)
    assert bootstrapped.returncode == 0, bootstrapped.stderr
    assert state.exists(), "bootstrap must persist state"

    second = run_cli(["run"], server, state)

    assert second.returncode == 0, second.stderr
    assert "ShoggothTwenty digest" not in second.stdout
    assert "nothing buffered" in combined(second)


def test_bootstrap_marks_the_head_without_reporting(server, tmp_path):
    state = tmp_path / "state.json"

    result = run_cli(["bootstrap"], server, state, dry_run=False)

    assert result.returncode == 0, result.stderr
    assert "marked" in combined(result)


def test_missing_credentials_fail_loudly(tmp_path):
    env = {**os.environ}
    for key in ("MOLTBOOK_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        env.pop(key, None)

    result = subprocess.run(
        [sys.executable, "agent.py", "run"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 1
    assert "missing required environment variable" in result.stderr


def test_unreachable_api_is_a_red_run_not_a_silent_no_op(tmp_path):
    """The original script swallowed every exception and exited zero."""
    env = {
        **os.environ,
        "MOLTBOOK_API_KEY": "k",
        "MOLTBOOK_BASE_URL": "http://127.0.0.1:1/api/v1",  # nothing listening
        "TELEGRAM_BOT_TOKEN": "123:TEST",
        "TELEGRAM_CHAT_ID": "424242",
        "STATE_PATH": str(tmp_path / "state.json"),
        "PERSONA_DIR": str(FIXTURES),
        "DRY_RUN": "1",
        "PYTHONPATH": str(REPO_ROOT),
    }

    result = subprocess.run(
        [sys.executable, "agent.py", "run"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 1
    assert "GET /home failed" in result.stderr

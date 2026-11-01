"""The watchdog: detecting the failures that produce no red run.

GitHub drops scheduled workflow runs silently — no run, no failure, no
notification. That happened to this project: the heartbeat is on
`cron: '13,43 * * * *'`, and on 2026-11-01 ten consecutive slots produced
nothing at all. No cancelled runs, no skipped runs, just absence.

A watchdog that only alerts is half a watchdog. These tests pin both halves:
noticing the silence, and being able to end it.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "watchdog.sh"
WATCHDOG_YML = REPO_ROOT / ".github" / "workflows" / "watchdog.yml"


def _workflow() -> dict:
    data = yaml.safe_load(WATCHDOG_YML.read_text(encoding="utf-8"))
    # `on:` parses as boolean True under YAML 1.1.
    return data, data.get("on", data.get(True))


def test_the_script_exists_and_is_executable():
    assert SCRIPT.exists()
    assert SCRIPT.stat().st_mode & 0o111, "watchdog.sh must be executable"


def test_the_script_is_valid_bash():
    """A syntax error here fails silently at runtime: the workflow step exits
    non-zero, which opens an issue rather than pointing at the real fault."""
    result = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_the_script_uses_strict_mode():
    text = SCRIPT.read_text(encoding="utf-8")
    assert "set -euo pipefail" in text


# --- the healing half ----------------------------------------------------


def test_the_watchdog_can_restart_the_heartbeat_not_just_report_it():
    """Alerting about a stopped agent while holding the means to restart it
    is the wrong side of the line."""
    text = SCRIPT.read_text(encoding="utf-8")
    assert "gh workflow run" in text


def test_the_workflow_grants_actions_write():
    """Without this the re-trigger fails, and the watchdog degrades to the
    alert-only behaviour it was supposed to replace."""
    data, _ = _workflow()
    perms = data.get("permissions") or {}
    assert perms.get("actions") == "write", perms


def test_the_restart_target_is_configurable_and_defaults_to_main():
    text = SCRIPT.read_text(encoding="utf-8")
    assert 'WATCHDOG_REF:-main' in text


def test_the_threshold_is_configurable_without_editing_the_script():
    text = SCRIPT.read_text(encoding="utf-8")
    assert "WATCHDOG_MAX_AGE_HOURS" in text


def test_a_successful_restart_does_not_fail_the_run():
    """A red watchdog tick is supposed to mean the watchdog is broken. If it
    fixed the problem, reporting failure would train the owner to ignore it."""
    text = SCRIPT.read_text(encoding="utf-8")
    restart_block = text[text.index("restarted=") :]
    # The success branch alerts and continues; only the failure branch exits 1.
    assert restart_block.index('exit 1') > restart_block.index('restarted="yes"')


# --- the noticing half ---------------------------------------------------


def test_the_watchdog_runs_often_enough_to_matter():
    """It was daily. With the heartbeat dropping most of its schedule, a
    daily check can be 24 hours late to a fault it could have fixed."""
    _, triggers = _workflow()
    crons = [entry["cron"] for entry in triggers.get("schedule") or []]
    assert len(crons) >= 2, "every entry is another chance to run when slots drop"


def test_the_watchdog_still_checks_for_the_inactivity_disable():
    """Separate failure: GitHub disables schedules after 60 days idle."""
    text = SCRIPT.read_text(encoding="utf-8")
    assert "state" in text and "actions/workflows" in text


def test_alerting_is_deduplicated():
    """Otherwise every watchdog run opens another issue."""
    text = SCRIPT.read_text(encoding="utf-8")
    assert "gh issue list" in text
    assert "gh issue create" in text


def test_a_failed_telegram_alert_does_not_fail_the_watchdog():
    text = SCRIPT.read_text(encoding="utf-8")
    assert "non-fatal" in text


# --- keeping CI able to run these tests ---------------------------------

# CI cannot be read from the sandbox, so a missing dependency shows up only as
# a red tick with no log to consult. These pin the wiring instead: if a test
# ever needs a library, the install path has to exist too.

DEV_REQUIREMENTS = REPO_ROOT / "requirements-dev.txt"
CI_YML = REPO_ROOT / ".github" / "workflows" / "ci.yml"


def test_pyyaml_is_importable():
    """The direct failure: this suite parses workflow YAML, and CI installs
    only requirements.txt plus pytest. Without a pinned dev requirements
    file, every test in this module errors on import while passing locally."""
    yaml.safe_load("a: 1")


def test_dev_requirements_exist_and_are_hash_pinned():
    assert DEV_REQUIREMENTS.exists(), "no hash-pinned home for test dependencies"
    text = DEV_REQUIREMENTS.read_text(encoding="utf-8")
    assert "--hash=sha256:" in text, "dev deps must be pinned like runtime deps"


def test_declared_dev_dependencies_are_actually_installed():
    """Checked against the distribution, not the module: PyPI calls this one
    pyyaml while the import is yaml, so importing by the pinned name would
    fail for a package that is perfectly well installed."""
    from importlib import metadata

    text = DEV_REQUIREMENTS.read_text(encoding="utf-8")
    declared = [
        line.strip().split("==")[0].strip()
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#") and "==" in line.split("\\")[0]
    ]
    assert declared, "requirements-dev.txt declares nothing"

    for name in declared:
        metadata.version(name)  # raises PackageNotFoundError if absent


def test_ci_installs_the_dev_requirements():
    ci = CI_YML.read_text(encoding="utf-8")
    assert "requirements-dev.txt" in ci, (
        "CI does not install requirements-dev.txt; tests needing it will fail "
        "there while passing locally"
    )

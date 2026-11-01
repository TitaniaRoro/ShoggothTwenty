"""The autonomy level could not be changed from the GitHub UI at all.

It lives in state/state.json, which exists only for the length of a runner
job. Changing it needs two steps — run `agent.py control`, then commit the
result back — and no workflow did either. The only way to move it was a
terminal with the repo checked out.

At L0 every action needs a human tap, so this one setting decides whether
the agent does anything or merely watches. It should not need a terminal.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
CONTROL_YML = REPO_ROOT / ".github" / "workflows" / "control.yml"


def _workflow() -> tuple[dict, dict]:
    data = yaml.safe_load(CONTROL_YML.read_text(encoding="utf-8"))
    return data, data.get("on", data.get(True))


def test_the_control_workflow_exists():
    assert CONTROL_YML.exists()


def test_it_can_be_run_by_hand():
    _, triggers = _workflow()
    assert "workflow_dispatch" in triggers


def test_it_can_commit_the_change_back():
    """The write is worthless until it is committed. The runner is ephemeral
    and this state file is the only copy."""
    data, _ = _workflow()
    assert (data.get("permissions") or {}).get("contents") == "write"


def test_it_does_not_cancel_mid_write():
    data, _ = _workflow()
    assert data["concurrency"]["cancel-in-progress"] is False


# --- the ladder and the dropdown must not drift -------------------------


def test_every_autonomy_level_is_reachable_from_the_ui():
    """AUTO_ACTIONS is the authority on what each level permits. If a level
    exists there but not here, it is unreachable without editing YAML."""
    import shoggoth.policy as policy

    _, triggers = _workflow()
    offered = set(triggers["workflow_dispatch"]["inputs"]["level"]["options"])
    assert offered == set(policy.AUTO_ACTIONS), (
        f"dropdown offers {sorted(offered)} but the ladder defines "
        f"{sorted(policy.AUTO_ACTIONS)}"
    )


def test_the_default_level_is_the_safe_one():
    """Someone clicking through without reading should land on shadow mode,
    not on autonomous posting."""
    _, triggers = _workflow()
    assert triggers["workflow_dispatch"]["inputs"]["level"]["default"] == "L0"


def test_setting_the_level_is_an_offered_action():
    _, triggers = _workflow()
    assert "set-level" in triggers["workflow_dispatch"]["inputs"]["action"]["options"]


# --- the other commands that had no way to run --------------------------


@pytest.mark.parametrize("action", ["status", "pending", "clear-pending", "kill", "unkill"])
def test_every_unreachable_command_is_now_reachable(action):
    """`pending` in particular: it was shipped and documented, then had no
    workflow to run it from."""
    _, triggers = _workflow()
    assert action in triggers["workflow_dispatch"]["inputs"]["action"]["options"]


def test_the_kill_switch_is_reachable_and_so_is_the_way_back():
    _, triggers = _workflow()
    options = triggers["workflow_dispatch"]["inputs"]["action"]["options"]
    assert "kill" in options and "unkill" in options


# --- reading the result -------------------------------------------------


def test_it_prints_the_level_before_and_after():
    """A green run proves nothing. Same lesson as claim status, which is a log
    line rather than an exit code for exactly this reason."""
    data, _ = _workflow()
    names = [step.get("name") for step in data["jobs"]["control"]["steps"]]
    assert "Before" in names and "After" in names


def test_no_state_change_is_reported_rather_than_failing():
    """Read-only actions like `status` legitimately change nothing, and that
    is not an error."""
    text = CONTROL_YML.read_text(encoding="utf-8")
    assert "no state change to commit" in text


def test_a_failed_commit_is_an_error_not_a_warning():
    """If the push fails, the change silently did not happen. That has to be
    loud, because the alternative is believing the level moved when it did
    not."""
    text = CONTROL_YML.read_text(encoding="utf-8")
    assert "did NOT persist" in text

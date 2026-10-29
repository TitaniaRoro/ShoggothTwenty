"""The audit log is the evidence trail; test it like it matters."""

from __future__ import annotations

from pathlib import Path

from shoggoth.audit import AuditLog


def test_records_and_reads_back(tmp_path):
    path = tmp_path / "audit" / "heartbeat.jsonl"
    log = AuditLog(path)

    log.record("run_start", posts_seen=4)
    log.record("notify_decision", send=True, reason="interval elapsed")

    entries = AuditLog.read(path)

    assert len(entries) == 2
    assert entries[0]["event"] == "run_start"
    assert entries[0]["posts_seen"] == 4
    assert entries[1]["send"] is True


def test_creates_its_own_directory(tmp_path):
    path = tmp_path / "deep" / "nested" / "audit.jsonl"

    AuditLog(path).record("run_start")

    assert path.exists()


def test_appends_across_instances(tmp_path):
    path = tmp_path / "audit.jsonl"
    AuditLog(path).record("first")
    AuditLog(path).record("second")

    assert [entry["event"] for entry in AuditLog.read(path)] == ["first", "second"]


def test_disabled_writes_nothing(tmp_path):
    path = tmp_path / "audit.jsonl"

    AuditLog(path, enabled=False).record("nope")

    assert not path.exists()


def test_corrupt_lines_are_skipped_not_fatal(tmp_path):
    path = tmp_path / "audit.jsonl"
    path.write_text('{"event": "good"}\nnot json\n{"event": "also_good"}\n', encoding="utf-8")

    entries = AuditLog.read(path)

    assert [entry["event"] for entry in entries] == ["good", "also_good"]


def test_missing_file_reads_as_empty(tmp_path):
    assert AuditLog.read(tmp_path / "nope.jsonl") == []


def test_unwritable_path_does_not_break_the_run(tmp_path):
    """A failed audit write is a warning, never a crashed heartbeat."""
    blocker = tmp_path / "blocker"
    blocker.write_text("I am a file, not a directory", encoding="utf-8")

    log = AuditLog(blocker / "audit.jsonl")
    log.record("run_start")  # must not raise

    assert True

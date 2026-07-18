"""Run engine tests (spec §3): `execute_run` drives one run end to end through
the workspace + carrier seams; `drain` is the `MUSHER_BATCH` sequential drain.

DB-backed via `migrated_db` (skips cleanly when Postgres is unreachable), a
STUB `claude` executable (never the real carrier, never the network), and a
LOCAL git repo as the clone source (never a real clone over the network) —
same pattern as `test_workspace.py`'s `source_repo` fixture and
`test_carrier.py`'s stub.

Engine tests that need the drain enabled must set MUSHER_ENABLED=1
explicitly — the autouse `_musher_stays_disabled` fixture in conftest.py pins
it off by default for every other test in the suite.
"""

from __future__ import annotations

import stat
import subprocess
import sys
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from snowline_musher import engine
from snowline_musher.db import session_scope
from snowline_musher.models import Origin, Run, RunState

# Deterministic identity for the fixture commit — the host's git config must
# not be required (or leak into) the test.
_GIT_ID = [
    "-c",
    "user.email=test@example.invalid",
    "-c",
    "user.name=musher tests",
]


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def source_repo(tmp_path: Path) -> Path:
    """A tiny local git repo with one commit on branch `main` — the clone
    source for `create_workspace`. Path-based, so cloning touches no network."""
    src = tmp_path / "source_repo"
    src.mkdir()
    _git(src, "init", "-b", "main")
    (src / "README.md").write_text("hello musher\n")
    _git(src, *_GIT_ID, "add", "README.md")
    _git(src, *_GIT_ID, "commit", "-m", "initial")
    return src


# A minimal stub `claude`: drains stdin (so the parent's write never blocks),
# then either emits a closing result line and exits STUB_EXIT, or — under
# STUB_HANG=1 — sleeps for an hour so timeout/cancel exercise the real
# SIGKILL-the-process-group path (see test_carrier.py for the fuller stub that
# also asserts the enforcement envelope; engine tests only need exit-code and
# hang behavior, that invariant is already covered there).
_STUB_BODY = r"""
import json, os, sys, time

sys.stdin.read()

if os.environ.get("STUB_HANG") == "1":
    time.sleep(3600)
    sys.exit(0)

if os.environ.get("STUB_NO_RESULT") != "1":
    sys.stdout.write(
        json.dumps(
            {"type": "result", "subtype": "success", "result": "engine stub summary"}
        )
        + "\n"
    )
sys.stdout.flush()
sys.exit(int(os.environ.get("STUB_EXIT", "0")))
"""


@pytest.fixture
def stub_claude(tmp_path: Path, monkeypatch) -> Path:
    stub = tmp_path / "claude-stub"
    stub.write_text(f"#!{sys.executable}\n{_STUB_BODY}")
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC | stat.S_IRWXU)
    monkeypatch.setenv("MUSHER_CLAUDE_BIN", str(stub))
    monkeypatch.setenv("SNOWLINE_PLATFORM_URL", "https://platform.snowline.ts.net")
    return stub


def _make_run(source_repo: Path, **overrides) -> Run:
    defaults: dict = dict(
        id=uuid.uuid4(),
        objective="implement the thing",
        repo=str(source_repo),
        base_branch="main",
        origin=Origin.api,
        timeout_s=30,
    )
    defaults.update(overrides)
    return Run(**defaults)


# --- execute_run -----------------------------------------------------------


def test_execute_run_happy_path(migrated_db, tmp_path, source_repo, stub_claude):
    runs_root = tmp_path / "runs"
    run = _make_run(source_repo)
    with session_scope() as session:
        session.add(run)
        session.flush()
        engine.execute_run(session, run, runs_root=runs_root)

    with session_scope() as session:
        persisted = session.get(Run, run.id)
        assert persisted.state is RunState.succeeded
        assert persisted.transcript_ref
        assert Path(persisted.transcript_ref).is_file()
        assert persisted.summary == "engine stub summary"
        assert persisted.workspace
        assert persisted.started_at is not None
        assert persisted.finished_at is not None
        # Not this item's job — the prompt-contract item extracts these later.
        assert persisted.branch is None
        assert persisted.pr_url is None


def test_execute_run_nonzero_exit_is_failed(
    migrated_db, tmp_path, source_repo, stub_claude, monkeypatch
):
    monkeypatch.setenv("STUB_EXIT", "3")
    runs_root = tmp_path / "runs"
    run = _make_run(source_repo)
    with session_scope() as session:
        session.add(run)
        session.flush()
        engine.execute_run(session, run, runs_root=runs_root)

    with session_scope() as session:
        persisted = session.get(Run, run.id)
        assert persisted.state is RunState.failed
        # Fail-visible: the transcript survives an ordinary non-zero exit too.
        assert persisted.transcript_ref
        assert Path(persisted.transcript_ref).is_file()


def test_execute_run_timeout(
    migrated_db, tmp_path, source_repo, stub_claude, monkeypatch
):
    monkeypatch.setenv("STUB_HANG", "1")
    runs_root = tmp_path / "runs"
    run = _make_run(source_repo, timeout_s=1)
    with session_scope() as session:
        session.add(run)
        session.flush()
        engine.execute_run(session, run, runs_root=runs_root)

    with session_scope() as session:
        persisted = session.get(Run, run.id)
        assert persisted.state is RunState.timed_out
        assert persisted.transcript_ref  # fail-visible


def test_execute_run_cancel(
    migrated_db, tmp_path, source_repo, stub_claude, monkeypatch
):
    monkeypatch.setenv("STUB_HANG", "1")
    runs_root = tmp_path / "runs"
    run = _make_run(source_repo, timeout_s=30)  # far larger than the cancel delay
    cancel_event = threading.Event()
    timer = threading.Timer(0.3, cancel_event.set)
    timer.start()
    try:
        with session_scope() as session:
            session.add(run)
            session.flush()
            engine.execute_run(
                session, run, cancel_event=cancel_event, runs_root=runs_root
            )
    finally:
        timer.cancel()

    with session_scope() as session:
        persisted = session.get(Run, run.id)
        assert persisted.state is RunState.cancelled
        assert persisted.transcript_ref  # fail-visible


def test_execute_run_workspace_error_is_failed_not_a_crash(migrated_db, tmp_path):
    """A WorkspaceError (bad repo) must land the run `failed`, fail-visibly —
    not escape execute_run as an unhandled crash that leaves the row wedged
    `running`."""
    runs_root = tmp_path / "runs"
    run = Run(
        id=uuid.uuid4(),
        objective="x",
        repo=str(tmp_path / "does-not-exist"),
        base_branch="main",
        origin=Origin.api,
    )
    with session_scope() as session:
        session.add(run)
        session.flush()
        result = engine.execute_run(session, run, runs_root=runs_root)
        assert result is run

    with session_scope() as session:
        persisted = session.get(Run, run.id)
        assert persisted.state is RunState.failed
        assert persisted.transcript_ref is None
        assert persisted.workspace is None


# --- drain -------------------------------------------------------------


def test_drain_processes_queued_runs_sequentially_oldest_first(
    migrated_db, tmp_path, source_repo, stub_claude, monkeypatch
):
    monkeypatch.setenv("MUSHER_ENABLED", "1")
    runs_root = tmp_path / "runs"
    base = datetime(2026, 7, 18, tzinfo=timezone.utc)
    run_a = _make_run(source_repo, created_at=base)
    run_b = _make_run(source_repo, created_at=base + timedelta(seconds=1))
    run_c = _make_run(source_repo, created_at=base + timedelta(seconds=2))
    with session_scope() as session:
        # Added out of creation order — drain must still pick the OLDEST
        # queued run first, every time, not insertion/statement order.
        session.add_all([run_c, run_a, run_b])

    processed = engine.drain(runs_root=runs_root)

    assert processed == [run_a.id, run_b.id, run_c.id]
    with session_scope() as session:
        for run_id in processed:
            persisted = session.get(Run, run_id)
            assert persisted.state is RunState.succeeded


def test_drain_stop_event_abandons_remaining_and_cancels_in_flight(
    migrated_db, tmp_path, source_repo, stub_claude, monkeypatch
):
    """abandon_on_cancel: a stop_event set mid-drain (1) cancels the in-flight
    run via the SAME mechanism a timeout uses rather than waiting it out, and
    (2) leaves every not-yet-started run untouched at `queued`."""
    monkeypatch.setenv("MUSHER_ENABLED", "1")
    monkeypatch.setenv("STUB_HANG", "1")
    runs_root = tmp_path / "runs"
    base = datetime(2026, 7, 18, tzinfo=timezone.utc)
    run_a = _make_run(source_repo, created_at=base, timeout_s=30)
    run_b = _make_run(source_repo, created_at=base + timedelta(seconds=1), timeout_s=30)
    with session_scope() as session:
        session.add_all([run_a, run_b])

    stop_event = threading.Event()
    timer = threading.Timer(0.3, stop_event.set)
    timer.start()
    try:
        processed = engine.drain(stop_event=stop_event, runs_root=runs_root)
    finally:
        timer.cancel()

    assert processed == [run_a.id]
    with session_scope() as session:
        a = session.get(Run, run_a.id)
        b = session.get(Run, run_b.id)
        assert a.state is RunState.cancelled
        assert b.state is RunState.queued  # abandoned untouched, never started


def test_drain_is_noop_when_disabled(migrated_db, tmp_path, source_repo, stub_claude):
    # MUSHER_ENABLED stays "0" (the autouse fixture) — no override here.
    run = _make_run(source_repo)
    with session_scope() as session:
        session.add(run)

    processed = engine.drain(runs_root=tmp_path / "runs")

    assert processed == []
    with session_scope() as session:
        persisted = session.get(Run, run.id)
        assert persisted.state is RunState.queued  # never touched

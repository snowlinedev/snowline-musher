"""Workspace lifecycle (spec §3): fresh per-run clone, correct path shape, and
retention-window GC that removes only terminal+old workspaces and never a run
row.

Clone tests use a LOCAL fixture git repo (no network, no real carrier). GC
tests are DB-backed via `migrated_db` (skip when Postgres is unreachable).
"""

from __future__ import annotations

import subprocess
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from snowline_musher import workspace
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
    source. Path-based, so `git clone` touches no network."""
    src = tmp_path / "source_repo"
    src.mkdir()
    _git(src, "init", "-b", "main")
    (src / "README.md").write_text("hello musher\n")
    _git(src, *_GIT_ID, "add", "README.md")
    _git(src, *_GIT_ID, "commit", "-m", "initial")
    return src


def _run(source_repo: Path, *, base_branch: str = "main") -> Run:
    return Run(
        id=uuid.uuid4(),
        objective="do the thing",
        repo=str(source_repo),
        base_branch=base_branch,
        origin=Origin.api,
    )


def test_create_workspace_clones_fresh(tmp_path, source_repo):
    runs_root = tmp_path / "runs"
    run = _run(source_repo)

    dest = workspace.create_workspace(run, runs_root=runs_root)

    # Correct path shape: <runs_root>/<run-id>/workspace.
    assert dest == runs_root / str(run.id) / "workspace"
    assert dest.is_dir()
    # A real clone: the committed file and a .git dir are present.
    assert (dest / "README.md").read_text() == "hello musher\n"
    assert (dest / ".git").exists()
    # The path is recorded on the run for autopsy.
    assert run.workspace == str(dest)


def test_workspaces_are_distinct_per_run(tmp_path, source_repo):
    runs_root = tmp_path / "runs"
    run_a = _run(source_repo)
    run_b = _run(source_repo)
    dest_a = workspace.create_workspace(run_a, runs_root=runs_root)
    dest_b = workspace.create_workspace(run_b, runs_root=runs_root)
    assert dest_a != dest_b
    assert dest_a.is_dir() and dest_b.is_dir()


def test_create_workspace_never_reused(tmp_path, source_repo):
    runs_root = tmp_path / "runs"
    run = _run(source_repo)
    workspace.create_workspace(run, runs_root=runs_root)
    # A second create for the same run id is refused — never reused (spec §3).
    with pytest.raises(workspace.WorkspaceError):
        workspace.create_workspace(run, runs_root=runs_root)


def test_create_workspace_fail_visible_on_bad_repo(tmp_path):
    runs_root = tmp_path / "runs"
    run = Run(
        id=uuid.uuid4(),
        objective="x",
        repo=str(tmp_path / "does-not-exist"),
        base_branch="main",
        origin=Origin.api,
    )
    with pytest.raises(workspace.WorkspaceError) as exc:
        workspace.create_workspace(run, runs_root=runs_root)
    # git's own stderr is surfaced, not a bare exit code.
    assert str(exc.value)


# --- GC (DB-backed) -----------------------------------------------------


def _persist(session, *, state, finished_at, workspace_dir: Path) -> uuid.UUID:
    workspace_dir.mkdir(parents=True, exist_ok=True)
    run = Run(
        id=uuid.uuid4(),
        objective="x",
        repo="o/r",
        base_branch="main",
        origin=Origin.api,
        state=state,
        finished_at=finished_at,
        workspace=str(workspace_dir),
    )
    session.add(run)
    session.flush()
    return run.id


def test_gc_removes_terminal_and_old_only(migrated_db, tmp_path):
    runs_root = tmp_path / "runs"
    now = datetime(2026, 7, 11, tzinfo=timezone.utc)
    old = now - timedelta(days=30)
    recent = now - timedelta(days=1)

    old_terminal = runs_root / "old-terminal"
    recent_terminal = runs_root / "recent-terminal"
    running = runs_root / "running"

    with session_scope() as session:
        old_id = _persist(
            session,
            state=RunState.succeeded,
            finished_at=old,
            workspace_dir=old_terminal,
        )
        recent_id = _persist(
            session,
            state=RunState.failed,
            finished_at=recent,
            workspace_dir=recent_terminal,
        )
        running_id = _persist(
            session,
            state=RunState.running,
            finished_at=None,
            workspace_dir=running,
        )

    with session_scope() as session:
        removed = workspace.gc_workspaces(
            session, retention_days=14, runs_root=runs_root, now=now
        )

    # Only the old, terminal workspace directory is gone.
    assert old_terminal in removed
    assert not old_terminal.exists()
    # Recent-terminal (inside window) and a live run are kept for autopsy/use.
    assert recent_terminal.exists()
    assert running.exists()
    assert removed == [old_terminal]

    # Fail-visibility: every run ROW survives GC, workspace pointer intact.
    with session_scope() as session:
        for rid in (old_id, recent_id, running_id):
            run = session.get(Run, rid)
            assert run is not None
            assert run.workspace is not None

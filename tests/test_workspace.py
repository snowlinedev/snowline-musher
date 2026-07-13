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


# --- repo form resolution -------------------------------------------------


def test_clone_url_resolves_slug_to_github():
    # The Run model's documented `owner/repo` shape must reach git as a real
    # clone URL, not a relative filesystem path.
    assert (
        workspace._clone_url("snowlinedev/snowline-musher")
        == "https://github.com/snowlinedev/snowline-musher.git"
    )


def test_clone_url_passes_paths_and_urls_verbatim():
    assert workspace._clone_url("/abs/path/repo") == "/abs/path/repo"
    assert workspace._clone_url("https://example.com/r.git") == (
        "https://example.com/r.git"
    )


def test_clone_url_refuses_helper_transports_and_relative_paths():
    # `ext::` transports execute arbitrary commands; a relative path would
    # resolve against the service cwd. Both are refused fail-visibly.
    with pytest.raises(workspace.WorkspaceError):
        workspace._clone_url("ext::sh -c 'echo pwned'")
    with pytest.raises(workspace.WorkspaceError):
        workspace._clone_url("./relative/repo")


def test_create_workspace_clears_stale_partial(tmp_path, source_repo):
    # A crash mid-clone leaves only the staging dir — the next attempt clears
    # it and succeeds instead of being wedged forever.
    runs_root = tmp_path / "runs"
    run = _run(source_repo)
    staging = workspace.workspace_path(run.id, runs_root=runs_root).with_name(
        "workspace.partial"
    )
    staging.mkdir(parents=True)
    (staging / "half-written").write_text("junk")

    dest = workspace.create_workspace(run, runs_root=runs_root)

    assert dest.is_dir()
    assert not staging.exists()


def test_failed_clone_leaves_no_partial(tmp_path, source_repo):
    runs_root = tmp_path / "runs"
    run = _run(source_repo, base_branch="no-such-branch")
    with pytest.raises(workspace.WorkspaceError):
        workspace.create_workspace(run, runs_root=runs_root)
    run_tree = workspace.run_dir(run.id, runs_root=runs_root)
    assert not (run_tree / "workspace").exists()
    assert not (run_tree / "workspace.partial").exists()


# --- GC (DB-backed) -----------------------------------------------------


def _persist(
    session,
    *,
    state,
    finished_at,
    runs_root: Path,
    workspace_override: Path | None = None,
) -> uuid.UUID:
    """Persist a run whose workspace directory exists on disk at its own
    `<runs_root>/<run-id>/workspace` (or at `workspace_override` for the
    containment tests)."""
    rid = uuid.uuid4()
    ws = workspace_override or workspace.workspace_path(rid, runs_root=runs_root)
    ws.mkdir(parents=True, exist_ok=True)
    run = Run(
        id=rid,
        objective="x",
        repo="o/r",
        base_branch="main",
        origin=Origin.api,
        state=state,
        finished_at=finished_at,
        workspace=str(ws),
    )
    session.add(run)
    session.flush()
    return rid


def test_gc_removes_terminal_and_old_only(migrated_db, tmp_path):
    runs_root = tmp_path / "runs"
    now = datetime(2026, 7, 11, tzinfo=timezone.utc)
    old = now - timedelta(days=30)
    recent = now - timedelta(days=1)

    with session_scope() as session:
        old_id = _persist(
            session, state=RunState.succeeded, finished_at=old, runs_root=runs_root
        )
        recent_id = _persist(
            session, state=RunState.failed, finished_at=recent, runs_root=runs_root
        )
        running_id = _persist(
            session, state=RunState.running, finished_at=None, runs_root=runs_root
        )

    with session_scope() as session:
        removed = workspace.gc_workspaces(
            session, retention_days=14, runs_root=runs_root, now=now
        )

    old_ws = workspace.workspace_path(old_id, runs_root=runs_root)
    # Only the old, terminal workspace directory is gone — and its emptied
    # <run-id> husk with it.
    assert removed == [old_ws]
    assert not old_ws.exists()
    assert not old_ws.parent.exists()
    # Recent-terminal (inside window) and a live run are kept for autopsy/use.
    assert workspace.workspace_path(recent_id, runs_root=runs_root).exists()
    assert workspace.workspace_path(running_id, runs_root=runs_root).exists()

    # Fail-visibility: every run ROW survives GC, workspace pointer intact.
    with session_scope() as session:
        for rid in (old_id, recent_id, running_id):
            run = session.get(Run, rid)
            assert run is not None
            assert run.workspace is not None


def test_gc_accepts_naive_now(migrated_db, tmp_path):
    # A scheduler passing datetime.now() (naive) must not crash the GC pass —
    # naive is treated as UTC.
    runs_root = tmp_path / "runs"
    naive_now = datetime(2026, 7, 11)
    with session_scope() as session:
        old_id = _persist(
            session,
            state=RunState.succeeded,
            finished_at=naive_now - timedelta(days=30),
            runs_root=runs_root,
        )
        removed = workspace.gc_workspaces(
            session, retention_days=14, runs_root=runs_root, now=naive_now
        )
    assert removed == [workspace.workspace_path(old_id, runs_root=runs_root)]


def test_gc_skips_workspace_outside_its_own_run_dir(migrated_db, tmp_path):
    # GC never trusts the DB column absolutely: a workspace value that is not
    # the run's own <runs_root>/<run-id>/workspace is skipped, not rmtree'd.
    runs_root = tmp_path / "runs"
    decoy = tmp_path / "precious"
    now = datetime(2026, 7, 11, tzinfo=timezone.utc)
    with session_scope() as session:
        _persist(
            session,
            state=RunState.succeeded,
            finished_at=now - timedelta(days=30),
            runs_root=runs_root,
            workspace_override=decoy,
        )
        removed = workspace.gc_workspaces(
            session, retention_days=14, runs_root=runs_root, now=now
        )
    assert removed == []
    assert decoy.exists()

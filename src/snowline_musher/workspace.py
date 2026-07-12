"""Workspace lifecycle (spec §3).

Every run executes in a FRESH clone of its own — `<runs_root>/<run-id>/
workspace` — never a live working copy, never reused across runs. The clone is
made with `git clone` so the run gets a real, isolated git checkout it can
branch and push from without touching any working tree the operator cares
about. `runs_root` defaults to the per-user `~/.snowline/musher/runs`
(config.runs_root, env-overridable) so tests point it at a tmp path.

Workspaces are KEPT after terminal states for autopsy and only reclaimed by
`gc_workspaces` on a retention window — which deletes the workspace DIRECTORY,
never the run row (fail-visibility, spec §2). GC is a plain callable here; no
scheduler wires it (a later item).
"""

from __future__ import annotations

import shutil
import subprocess
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from snowline_musher import config, runs
from snowline_musher.models import Run


class WorkspaceError(Exception):
    """A workspace could not be created (clone failed / path already exists).
    Carries the git stderr so a failed clone is fail-visible, not a bare
    non-zero exit."""


def _runs_root(runs_root: Path | None) -> Path:
    return runs_root if runs_root is not None else config.runs_root()


def run_dir(run_id: uuid.UUID | str, *, runs_root: Path | None = None) -> Path:
    """The run's own tree: `<runs_root>/<run-id>`."""
    return _runs_root(runs_root) / str(run_id)


def workspace_path(run_id: uuid.UUID | str, *, runs_root: Path | None = None) -> Path:
    """The clone path for a run: `<runs_root>/<run-id>/workspace` (spec §3)."""
    return run_dir(run_id, runs_root=runs_root) / "workspace"


def create_workspace(run: Run, *, runs_root: Path | None = None) -> Path:
    """Clone `run.repo` @ `run.base_branch` into the run's workspace and record
    the path on `run.workspace`.

    A fresh clone every time: the path is keyed on the run id, so it is
    distinct per run by construction, and an already-existing workspace is an
    error (never reused). For tests, `run.repo` is a local path — `git clone`
    from a filesystem path needs no network. Does not commit; the caller owns
    the session."""
    dest = workspace_path(run.id, runs_root=runs_root)
    if dest.exists():
        raise WorkspaceError(
            f"workspace already exists at {dest} — workspaces are never reused"
        )
    dest.parent.mkdir(parents=True, exist_ok=True)

    proc = subprocess.run(
        [
            "git",
            "clone",
            "--branch",
            run.base_branch,
            "--",
            run.repo,
            str(dest),
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        # Fail-visible: surface git's own stderr rather than a bare exit code.
        raise WorkspaceError(
            f"git clone of {run.repo!r}@{run.base_branch!r} into {dest} "
            f"failed (exit {proc.returncode}): {proc.stderr.strip()}"
        )

    run.workspace = str(dest)
    return dest


def gc_workspaces(
    session: Session,
    *,
    retention_days: int | None = None,
    runs_root: Path | None = None,
    now: datetime | None = None,
) -> list[Path]:
    """Delete workspace directories of runs that are BOTH terminal AND older
    than the retention window; return the paths removed.

    Age is measured from `finished_at` (when the run became terminal), falling
    back to `created_at` for the pathological terminal-without-finished_at row.
    Only the DIRECTORY is removed — the run row and its `workspace` pointer
    survive as the record of where the autopsy clone used to live (spec §2).
    A live (queued/running) run, or a terminal run inside the window, is left
    alone. GC is a callable only; nothing schedules it here."""
    if retention_days is None:
        retention_days = config.workspace_retention_days()
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=retention_days)

    removed: list[Path] = []
    terminal = tuple(runs.TERMINAL_STATES)
    stmt = select(Run).where(Run.state.in_(terminal))
    for run in session.execute(stmt).scalars():
        became_terminal = run.finished_at or run.created_at
        if became_terminal is None:
            continue
        if _as_utc(became_terminal) > cutoff:
            continue  # still inside the retention window — keep for autopsy
        target = (
            Path(run.workspace)
            if run.workspace
            else workspace_path(run.id, runs_root=runs_root)
        )
        if target.exists():
            shutil.rmtree(target)
            removed.append(target)
    return removed


def _as_utc(dt: datetime) -> datetime:
    """Postgres `timestamp without time zone` reads back naive; treat a naive
    value as UTC (we store UTC) so the window comparison is apples-to-apples."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt

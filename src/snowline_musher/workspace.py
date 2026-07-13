"""Workspace lifecycle (spec §3).

Every run executes in a FRESH clone of its own — `<runs_root>/<run-id>/
workspace` — never a live working copy, never reused across runs. The clone is
made with `git clone` so the run gets a real, isolated git checkout it can
branch and push from without touching any working tree the operator cares
about. `runs_root` defaults to the per-user `~/.snowline/musher/runs`
(config.runs_root, env-overridable) so tests point it at a tmp path.

`Run.repo` is resolved through `_clone_url` before it reaches git: the
documented `owner/repo` slug becomes a GitHub https URL (the host's gh
credential helper supplies auth, spec §6); absolute paths and explicit
file/https/ssh URLs pass through; everything else — relative paths, `ext::`
and other command-executing helper transports — is refused fail-visibly, with
`GIT_ALLOW_PROTOCOL` as the backstop at the git layer.

Workspaces are KEPT after terminal states for autopsy and only reclaimed by
`gc_workspaces` on a retention window — which deletes the workspace DIRECTORY,
never the run row (fail-visibility, spec §2). GC is a plain callable here; no
scheduler wires it (a later item).
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import shutil
import subprocess
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from snowline_musher import config, runs
from snowline_musher.models import Run

log = logging.getLogger("snowline_musher.workspace")

# The Run model's documented repo shape: an `owner/repo` slug.
_SLUG_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")

# Transports git may use for a clone — an ALLOWLIST, not a blocklist:
# `ext::`-style helper transports execute arbitrary commands, so anything not
# listed here is refused by git itself even if a URL slips past _clone_url.
_ALLOWED_GIT_PROTOCOLS = "file:https:ssh"

# A clone that produces nothing for this long is wedged, not slow — surface a
# WorkspaceError instead of blocking the dispatcher forever.
CLONE_TIMEOUT_S = 600


class WorkspaceError(Exception):
    """A workspace could not be created (clone failed / path already exists /
    unsupported repo form). Carries the underlying detail (e.g. git's stderr)
    so a failed clone is fail-visible, not a bare non-zero exit."""


def _runs_root(runs_root: Path | None) -> Path:
    return runs_root if runs_root is not None else config.runs_root()


def run_dir(run_id: uuid.UUID | str, *, runs_root: Path | None = None) -> Path:
    """The run's own tree: `<runs_root>/<run-id>`."""
    return _runs_root(runs_root) / str(run_id)


def workspace_path(run_id: uuid.UUID | str, *, runs_root: Path | None = None) -> Path:
    """The clone path for a run: `<runs_root>/<run-id>/workspace` (spec §3)."""
    return run_dir(run_id, runs_root=runs_root) / "workspace"


def transcript_path(run_id: uuid.UUID | str, *, runs_root: Path | None = None) -> Path:
    """The stream-json transcript path for a run:
    `<runs_root>/<run-id>/transcript.jsonl` — a SIBLING of `workspace/`, not
    inside the clone (spec §3). Deliberately outside the workspace so a
    workspace GC (`gc_workspaces`, which only removes the `workspace/` dir)
    leaves the transcript intact: a failed or timed-out run keeps its
    transcript as the autopsy record even after its clone is reclaimed
    (fail-visibility, spec §2)."""
    return run_dir(run_id, runs_root=runs_root) / "transcript.jsonl"


def envelope_config_path(
    run_id: uuid.UUID | str, *, runs_root: Path | None = None
) -> Path:
    """The auto-mode envelope settings path for a run:
    `<runs_root>/<run-id>/envelope.settings.json` — also a SIBLING of
    `workspace/`, for two reasons. (1) It is passed to `claude` via the
    `--settings <path>` flag, and a settings file that lives *inside* the clone
    would be committed into the run's own PR — the envelope is the runner's
    business, not the repo's. (2) Like the transcript, keeping it out of
    `workspace/` means it survives a workspace GC, so a past run's enforcement
    envelope stays readable for autopsy. See `carrier.write_envelope_config`
    for why the flag (not a checked-in `.claude/settings.json`) is the only
    per-run mechanism Claude Code honors for classifier rules."""
    return run_dir(run_id, runs_root=runs_root) / "envelope.settings.json"


def _clone_url(repo: str) -> str:
    """Resolve `Run.repo` to something `git clone` may be handed.

    The documented shape is an `owner/repo` slug → the matching GitHub https
    URL (the operator's gh credential helper authenticates it, spec §6). An
    absolute filesystem path (tests, local mirrors) or an explicit
    file/https/ssh URL passes through verbatim. Anything else is refused
    here: relative paths would resolve against the service cwd, and helper
    transports like `ext::` execute arbitrary commands."""
    if _SLUG_RE.match(repo):
        return f"https://github.com/{repo}.git"
    if repo.startswith(("/", "file://", "https://", "ssh://")):
        return repo
    raise WorkspaceError(
        f"unsupported repo form {repo!r} — expected an owner/repo slug, an "
        "absolute path, or a file/https/ssh URL"
    )


def create_workspace(run: Run, *, runs_root: Path | None = None) -> Path:
    """Clone `run.repo` @ `run.base_branch` into the run's workspace and record
    the path on `run.workspace`.

    A fresh clone every time: the path is keyed on the run id, so it is
    distinct per run by construction, and an already-existing workspace is an
    error (never reused). The clone lands in a `workspace.partial` staging
    directory and is renamed into place only on success — a crash mid-clone
    leaves nothing squatting on the never-reuse check, and the next attempt
    clears the stale staging dir itself. Does not commit; the caller owns the
    session."""
    dest = workspace_path(run.id, runs_root=runs_root)
    if dest.exists():
        raise WorkspaceError(
            f"workspace already exists at {dest} — workspaces are never reused"
        )
    url = _clone_url(run.repo)
    dest.parent.mkdir(parents=True, exist_ok=True)
    staging = dest.with_name(dest.name + ".partial")
    shutil.rmtree(staging, ignore_errors=True)

    env = {**os.environ, "GIT_ALLOW_PROTOCOL": _ALLOWED_GIT_PROTOCOLS}
    try:
        proc = subprocess.run(
            [
                "git",
                "clone",
                "--branch",
                run.base_branch,
                "--",
                url,
                str(staging),
            ],
            capture_output=True,
            text=True,
            env=env,
            timeout=CLONE_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        shutil.rmtree(staging, ignore_errors=True)
        raise WorkspaceError(
            f"git clone of {run.repo!r}@{run.base_branch!r} timed out after "
            f"{CLONE_TIMEOUT_S}s"
        ) from None
    if proc.returncode != 0:
        shutil.rmtree(staging, ignore_errors=True)
        # Fail-visible: surface git's own stderr rather than a bare exit code.
        raise WorkspaceError(
            f"git clone of {run.repo!r}@{run.base_branch!r} into {dest} "
            f"failed (exit {proc.returncode}): {proc.stderr.strip()}"
        )
    staging.rename(dest)

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
    survive as the record of where the autopsy clone used to live (spec §2) —
    and only when the stored path IS this run's own `<runs_root>/<run-id>/
    workspace`: GC never rmtree's an arbitrary DB-sourced string, it skips
    (loudly) anything that doesn't match. The emptied `<run-id>` husk is
    removed too, unless something else (a future transcript file) lives in it.
    A live (queued/running) run, or a terminal run inside the window, is left
    alone. A naive `now` is treated as UTC. GC is a callable only; nothing
    schedules it here."""
    if retention_days is None:
        retention_days = config.workspace_retention_days()
    now_utc = _as_utc(now) if now is not None else datetime.now(timezone.utc)
    cutoff = now_utc - timedelta(days=retention_days)

    removed: list[Path] = []
    terminal = tuple(runs.TERMINAL_STATES)
    stmt = select(Run).where(Run.state.in_(terminal))
    for run in session.execute(stmt).scalars():
        became_terminal = run.finished_at or run.created_at
        if became_terminal is None:
            continue
        if _as_utc(became_terminal) > cutoff:
            continue  # still inside the retention window — keep for autopsy
        expected = workspace_path(run.id, runs_root=runs_root)
        target = Path(run.workspace) if run.workspace else expected
        if target.resolve() != expected.resolve():
            log.warning(
                "gc: run %s workspace %r is not its own %s — skipping",
                run.id,
                run.workspace,
                expected,
            )
            continue
        if target.exists():
            shutil.rmtree(target)
            removed.append(target)
        # Reclaim the empty <run-id> husk; anything still inside keeps it.
        with contextlib.suppress(OSError):
            target.parent.rmdir()
    return removed


def _as_utc(dt: datetime) -> datetime:
    """Postgres `timestamp` columns read back naive; treat a naive value as
    UTC (we store UTC) so window comparisons are apples-to-apples."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt

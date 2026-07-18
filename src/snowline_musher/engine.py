"""Run orchestration — the ONE place that drives a `Run` through the workspace
+ carrier seams and lands it in a terminal state (spec §3).

`execute_run` runs a single queued run end to end: `queued → running →
{workspace clone → carrier invocation} → terminal`. It is fail-visible (spec
§2) by construction — an infrastructure failure (a bad clone, a missing
carrier binary) is caught HERE and turned into a `failed` run rather than an
unhandled crash that would leave the row wedged in `running` forever with no
terminal record.

`drain` is the `MUSHER_BATCH` sequential drain (spec §3): an honest name for
"drains everything currently queued, one run at a time" — there is no
parallelism in v1; subscription usage windows are the real constraint, and
concurrency is a later, measured change. It is a plain callable, like
`workspace.gc_workspaces` — nothing here schedules it; a later item wires it
into the service lifespan. `abandon_on_cancel`: the drain accepts a
`stop_event` that both (1) gates whether another queued run is picked up, and
(2) rides straight through to `execute_run` as the carrier's `cancel_event` —
so a shutdown never waits an in-flight run out, it cancels it and the drain
returns as soon as that one run's kill lands.

Both callables are gated by `config.musher_enabled()` at the point that
matters: `drain` refuses to do anything (spec §3: "Off by default") when
disabled; `execute_run` does NOT re-check the flag — it is a single-run
primitive a caller (drain, or a future direct-dispatch path) invokes only once
it has already decided to run something.
"""

from __future__ import annotations

import logging
import threading
import uuid
from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from snowline_musher import config, runs
from snowline_musher import workspace as workspace_mod
from snowline_musher.carrier import invoke_carrier
from snowline_musher.db import session_scope
from snowline_musher.models import Run, RunState
from snowline_musher.workspace import create_workspace

log = logging.getLogger("snowline_musher.engine")

# Maps a carrier outcome to the terminal state execute_run transitions into.
# kill_reason takes priority over exit_code — a carrier killed on timeout/
# cancel may still exit with some code of its own, but the KILL is why the run
# ended, not whatever code the SIGKILLed process happened to report.
_KILL_REASON_STATE: dict[str, RunState] = {
    "timeout": RunState.timed_out,
    "cancel": RunState.cancelled,
}


def execute_run(
    session: Session,
    run: Run,
    *,
    cancel_event: threading.Event | None = None,
    runs_root: Path | None = None,
) -> Run:
    """Drive `run` from `queued` to a terminal state, end to end.

    Sequence: `transition(run, running)` + commit (so the row is visibly
    `running` the instant work starts, not only once it finishes); clone the
    workspace (`workspace.create_workspace`); invoke the carrier
    (`carrier.invoke_carrier`, passing `run.timeout_s` and `cancel_event`
    through); record the outputs (`workspace`, `transcript_ref`, `summary` —
    `branch`/`pr_url` are left `None`, a later prompt-contract item extracts
    those from the transcript); and transition to the terminal state the
    carrier outcome implies:
      - `kill_reason == "timeout"` → `timed_out`
      - `kill_reason == "cancel"`  → `cancelled`
      - `exit_code == 0`           → `succeeded`
      - otherwise                  → `failed`

    Fail-visible on ANY failure (spec §2): if anything between the `running`
    transition and the terminal one raises — an expected infrastructure error
    (`WorkspaceError` from a bad clone, `CarrierError` from a missing binary)
    OR an unexpected one (a decode error on garbage carrier output, a DB hiccup
    on commit) — this function does NOT let the exception escape. It logs the
    failure (with a stack trace, since there is no dedicated error-text column
    on `Run` yet), transitions the run to `failed`, commits, and returns the
    run — the run row itself is the durable record. The alternative (letting it
    escape) would leave the row wedged `running` forever AND abort the whole
    `drain` batch on the first pathological run; catching here means `drain`
    loops without a try/except of its own and one bad run never stops the rest.
    `KeyboardInterrupt`/`SystemExit` are NOT swallowed (they are not
    `Exception`) — a real interrupt still propagates.

    Commits happen at two points: right after entering `running` (durability —
    a crash between here and the terminal transition leaves the row correctly
    showing `running`, not a stale `queued`), and once more at the terminal
    transition (workspace/carrier outputs + terminal state land atomically).
    Does not commit on the caller's behalf beyond that; the caller's session
    still owns final disposition (e.g. `session_scope` commits again as a
    no-op on clean exit)."""
    runs.transition(run, RunState.running)
    session.commit()

    try:
        workspace_path = create_workspace(run, runs_root=runs_root)
        transcript_path = workspace_mod.transcript_path(run.id, runs_root=runs_root)
        result = invoke_carrier(
            run,
            workspace_path,
            transcript_path=transcript_path,
            timeout_s=run.timeout_s,
            cancel_event=cancel_event,
        )

        run.transcript_ref = str(result.transcript_path)
        run.summary = result.summary

        if result.kill_reason is not None:
            dst = _KILL_REASON_STATE[result.kill_reason]
        elif result.exit_code == 0:
            dst = RunState.succeeded
        else:
            dst = RunState.failed

        runs.transition(run, dst)
        session.commit()
        return run
    except Exception:
        # Fail-visible catch-all (spec §2) — see the docstring. Whatever went
        # wrong, the run must land terminal, never wedge `running`, and `drain`
        # must be able to move on. rollback() first so a poisoned/partial
        # transaction (e.g. the terminal commit itself failed) is cleared
        # before we write `failed`; the `running` state was already committed,
        # so the guard below sees the reloaded `running` and advances it.
        log.error(
            "run %s: failed before a clean terminal outcome", run.id, exc_info=True
        )
        session.rollback()
        if not runs.is_terminal(run.state):
            runs.transition(run, RunState.failed)
            session.commit()
        return run


def drain(
    *,
    stop_event: threading.Event | None = None,
    runs_root: Path | None = None,
    session_factory: Callable[[], AbstractContextManager[Session]] | None = None,
) -> list[uuid.UUID]:
    """The `MUSHER_BATCH` sequential drain (spec §3): repeatedly pick the
    OLDEST `queued` run and `execute_run` it, one at a time, until no queued
    runs remain. Returns the ids of every run processed, in the order they
    were dispatched.

    A no-op unless `config.musher_enabled()` — like `workspace.gc_workspaces`,
    this is a callable only; nothing schedules it in this item.

    Each run is executed against a FRESH `session_scope()` (or
    `session_factory()`, overridable for tests) so one run's failed/rolled-back
    session cannot poison the next — sequential, but not sharing state across
    iterations beyond the DB rows themselves.

    `abandon_on_cancel` (spec §3): `stop_event` plays TWO roles at once, which
    is what makes shutdown never wait an in-flight run out.
      1. Before picking up each new run, if `stop_event` is set, the drain
         stops immediately and leaves every remaining `queued` run untouched
         (`queued` is a valid resting state; nothing here half-starts a run).
      2. `stop_event` is passed straight through to `execute_run` as its
         `cancel_event`. If a run is already in flight when `stop_event` gets
         set, `invoke_carrier`'s watchdog observes the SAME event, SIGKILLs the
         carrier's process group, and the run lands `cancelled` — so the drain
         does not block waiting for a long-running carrier to notice shutdown
         on its own; it forces the issue via the same mechanism a timeout uses.
    A `stop_event` of `None` (the default) disables both behaviors: the drain
    runs to exhaustion and each run gets no cancellation path beyond its own
    `timeout_s`."""
    if not config.musher_enabled():
        log.debug("drain: MUSHER_ENABLED is off — no-op")
        return []

    factory = session_factory or session_scope
    processed: list[uuid.UUID] = []

    while True:
        if stop_event is not None and stop_event.is_set():
            log.info(
                "drain: stop_event set — abandoning the drain, %d run(s) already "
                "processed, remaining queued runs left queued",
                len(processed),
            )
            break

        with factory() as session:
            run = session.execute(
                select(Run)
                .where(Run.state == RunState.queued)
                .order_by(Run.created_at)
                .limit(1)
            ).scalar_one_or_none()
            if run is None:
                break
            execute_run(session, run, cancel_event=stop_event, runs_root=runs_root)
            processed.append(run.id)

    return processed

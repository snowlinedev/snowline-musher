"""Run state machine — the ONE place that says which state transitions are
legal (spec §2).

The lifecycle is `queued → running → {succeeded | failed | timed_out |
cancelled}`, plus `queued → cancelled` (a run cancelled before it ever
started). Every other move is illegal and raises `IllegalTransition` — callers
never silently no-op a bad transition, because a run that "somehow" went
`succeeded → running` is a bug we want loud.

All four terminal states are FAIL-VISIBLE (spec §2): a terminal run stays a
readable record. This module never deletes a run and never clears its output
fields — it only advances `state` and stamps `started_at` / `finished_at`.
"""

from __future__ import annotations

from datetime import datetime, timezone

from snowline_musher.models import Run, RunState

# The closed set of terminal states — a run here is done, for any reason.
TERMINAL_STATES: frozenset[RunState] = frozenset(
    {
        RunState.succeeded,
        RunState.failed,
        RunState.timed_out,
        RunState.cancelled,
    }
)

# src state -> the states it may legally move to. Terminal states map to the
# empty set (no exits), which is why they need no entries here.
_LEGAL_TRANSITIONS: dict[RunState, frozenset[RunState]] = {
    RunState.queued: frozenset({RunState.running, RunState.cancelled}),
    RunState.running: frozenset(
        {
            RunState.succeeded,
            RunState.failed,
            RunState.timed_out,
            RunState.cancelled,
        }
    ),
}


class IllegalTransition(Exception):
    """Raised when a state move is not part of the run lifecycle (spec §2)."""

    def __init__(self, src: RunState, dst: RunState) -> None:
        self.src = src
        self.dst = dst
        super().__init__(f"illegal run transition: {src.value} -> {dst.value}")


def is_terminal(state: RunState) -> bool:
    return state in TERMINAL_STATES


def can_transition(src: RunState, dst: RunState) -> bool:
    """True iff `src -> dst` is a legal move. Same-state moves are NOT legal —
    a no-op transition hides a caller bug (double-start, double-finish)."""
    return dst in _LEGAL_TRANSITIONS.get(src, frozenset())


def assert_transition(src: RunState, dst: RunState) -> None:
    """Raise `IllegalTransition` unless `src -> dst` is legal."""
    if not can_transition(src, dst):
        raise IllegalTransition(src, dst)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def transition(run: Run, dst: RunState, *, now: datetime | None = None) -> Run:
    """Advance `run` to `dst`, validating legality and stamping timestamps.

    `queued → running` stamps `started_at`; any move INTO a terminal state
    stamps `finished_at`. Output fields (transcript_ref, summary, workspace,
    branch, pr_url) are never touched here — fail-visibility means a terminal
    run keeps whatever it produced. Returns the same `run` for convenience;
    does not commit (the caller owns the session)."""
    assert_transition(run.state, dst)
    stamp = now or _utcnow()
    if dst is RunState.running:
        run.started_at = stamp
    if dst in TERMINAL_STATES:
        run.finished_at = stamp
    run.state = dst
    return run

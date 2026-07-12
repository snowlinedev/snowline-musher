"""Run state machine — every legal move is allowed, every illegal one raises
(spec §2). No DB: the machine operates on a plain `Run` instance in memory.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from snowline_musher import runs
from snowline_musher.models import Run, RunState

# The full lifecycle (spec §2): queued→running, running→each terminal, and the
# queued→cancelled shortcut for a run cancelled before it ever started.
LEGAL = [
    (RunState.queued, RunState.running),
    (RunState.queued, RunState.cancelled),
    (RunState.running, RunState.succeeded),
    (RunState.running, RunState.failed),
    (RunState.running, RunState.timed_out),
    (RunState.running, RunState.cancelled),
]

# A representative sweep of illegal moves: backwards from terminal, skipping
# queued→terminal-success, terminal→terminal, and same-state no-ops.
ILLEGAL = [
    (RunState.queued, RunState.succeeded),
    (RunState.queued, RunState.failed),
    (RunState.queued, RunState.timed_out),
    (RunState.running, RunState.queued),
    (RunState.succeeded, RunState.running),
    (RunState.failed, RunState.running),
    (RunState.timed_out, RunState.queued),
    (RunState.cancelled, RunState.running),
    (RunState.succeeded, RunState.failed),
    # same-state moves are NOT legal — a no-op hides a double-transition bug.
    (RunState.queued, RunState.queued),
    (RunState.running, RunState.running),
    (RunState.succeeded, RunState.succeeded),
]


def _run(state: RunState) -> Run:
    return Run(
        objective="do the thing",
        repo="owner/repo",
        base_branch="main",
        origin="api",
        state=state,
    )


@pytest.mark.parametrize("src,dst", LEGAL)
def test_legal_transitions_allowed(src, dst):
    assert runs.can_transition(src, dst) is True
    runs.assert_transition(src, dst)  # must not raise


@pytest.mark.parametrize("src,dst", ILLEGAL)
def test_illegal_transitions_raise(src, dst):
    assert runs.can_transition(src, dst) is False
    with pytest.raises(runs.IllegalTransition) as exc:
        runs.assert_transition(src, dst)
    assert exc.value.src is src
    assert exc.value.dst is dst


def test_terminal_states_have_no_exits():
    for state in runs.TERMINAL_STATES:
        assert runs.is_terminal(state) is True
        for dst in RunState:
            assert runs.can_transition(state, dst) is False


def test_transition_stamps_started_at_on_running():
    run = _run(RunState.queued)
    assert run.started_at is None
    now = datetime(2026, 7, 11, tzinfo=timezone.utc)
    runs.transition(run, RunState.running, now=now)
    assert run.state is RunState.running
    assert run.started_at == now
    assert run.finished_at is None


def test_transition_stamps_finished_at_on_terminal():
    run = _run(RunState.running)
    now = datetime(2026, 7, 11, tzinfo=timezone.utc)
    runs.transition(run, RunState.failed, now=now)
    assert run.state is RunState.failed
    assert run.finished_at == now


def test_transition_preserves_output_fields_on_terminal():
    # Fail-visibility (spec §2): a terminal transition never clears outputs.
    run = _run(RunState.running)
    run.transcript_ref = "transcript://abc"
    run.summary = "what happened"
    run.workspace = "/runs/abc/workspace"
    runs.transition(run, RunState.timed_out)
    assert run.transcript_ref == "transcript://abc"
    assert run.summary == "what happened"
    assert run.workspace == "/runs/abc/workspace"


def test_transition_rejects_illegal_without_mutating():
    run = _run(RunState.succeeded)
    with pytest.raises(runs.IllegalTransition):
        runs.transition(run, RunState.running)
    assert run.state is RunState.succeeded  # unchanged

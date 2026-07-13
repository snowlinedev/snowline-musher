"""Run model round-trip against a real (migrated) Postgres.

DB-backed: uses the `migrated_db` fixture, which skips cleanly when Postgres is
unreachable. Exercises the enum columns, the nullable outputs, and the
server-default `created_at` through an actual INSERT/SELECT.
"""

from __future__ import annotations

import uuid

from snowline_musher.db import session_scope
from snowline_musher.models import Carrier, Origin, Run, RunState


def test_run_round_trip(migrated_db):
    run_id = uuid.uuid4()
    with session_scope() as session:
        session.add(
            Run(
                id=run_id,
                objective="implement the widget",
                repo="snowlinedev/snowline-musher",
                base_branch="main",
                scope="snowlinedev/snowline-musher",
                origin=Origin.mcp,
                origin_ref="42",
                model="opus",
                timeout_s=1800,
            )
        )

    with session_scope() as session:
        run = session.get(Run, run_id)
        assert run is not None
        # Enums come back as the Python members (stored as their .value).
        assert run.carrier is Carrier.claude  # column default applied
        assert run.origin is Origin.mcp
        assert run.state is RunState.queued  # column default applied
        assert run.model == "opus"
        assert run.timeout_s == 1800
        assert run.scope == "snowlinedev/snowline-musher"
        assert run.origin_ref == "42"
        # Outputs start empty; created_at gets the server default.
        assert run.workspace is None
        assert run.branch is None
        assert run.pr_url is None
        assert run.transcript_ref is None
        assert run.summary is None
        assert run.started_at is None
        assert run.finished_at is None
        assert run.created_at is not None


def test_enum_stored_as_value_string(migrated_db):
    # The DB literal must equal the spec's wire string (native_enum=False +
    # values_callable), not the Python member name — a guard against a future
    # rename silently changing stored data.
    import sqlalchemy as sa

    run_id = uuid.uuid4()
    with session_scope() as session:
        session.add(
            Run(
                id=run_id,
                objective="x",
                repo="o/r",
                base_branch="main",
                origin=Origin.watcher,
            )
        )
    with session_scope() as session:
        row = session.execute(
            sa.text("SELECT carrier, origin, state FROM runs WHERE id = :i"),
            {"i": run_id},
        ).one()
        assert row == ("claude", "watcher", "queued")

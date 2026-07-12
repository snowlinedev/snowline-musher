"""run engine: runs table

Creates the `runs` table (spec §2): one row per dispatched carrier run. Enum
columns are VARCHAR + CHECK (`native_enum=False`), mirroring the model — the
carrier set grows and states are closed, and a check constraint evolves with a
plain migration instead of a native-Postgres `ALTER TYPE`. Values are spelled
literally here so the migration is self-contained (never imports app enums,
which can drift under it).

Revision ID: 26b31e42a6c6
Revises: 63ce644e5551
Create Date: 2026-07-11 22:16:27.012254

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "26b31e42a6c6"
down_revision: str | None = "63ce644e5551"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _enum(name: str, *values: str) -> sa.Enum:
    return sa.Enum(*values, name=name, native_enum=False, length=32)


def upgrade() -> None:
    op.create_table(
        "runs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        # inputs
        sa.Column("objective", sa.Text(), nullable=False),
        sa.Column("repo", sa.String(length=255), nullable=False),
        sa.Column("base_branch", sa.String(length=255), nullable=False),
        sa.Column("scope", sa.String(length=255), nullable=True),
        sa.Column(
            "carrier",
            _enum("carrier", "claude"),
            nullable=False,
        ),
        sa.Column("model", sa.String(length=128), nullable=True),
        sa.Column("timeout_s", sa.Integer(), nullable=False),
        sa.Column(
            "origin",
            _enum("origin", "mcp", "api", "watcher"),
            nullable=False,
        ),
        sa.Column("origin_ref", sa.String(length=255), nullable=True),
        # lifecycle
        sa.Column(
            "state",
            _enum(
                "run_state",
                "queued",
                "running",
                "succeeded",
                "failed",
                "timed_out",
                "cancelled",
            ),
            nullable=False,
        ),
        # outputs
        sa.Column("workspace", sa.Text(), nullable=True),
        sa.Column("branch", sa.String(length=255), nullable=True),
        sa.Column("pr_url", sa.Text(), nullable=True),
        sa.Column("transcript_ref", sa.Text(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        # timestamps (stored UTC)
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
    )
    # List/filter surfaces query by state and scope (spec §4.1 GET /runs).
    op.create_index("ix_runs_state", "runs", ["state"])
    op.create_index("ix_runs_scope", "runs", ["scope"])


def downgrade() -> None:
    op.drop_index("ix_runs_scope", table_name="runs")
    op.drop_index("ix_runs_state", table_name="runs")
    op.drop_table("runs")

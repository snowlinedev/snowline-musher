"""genesis: baseline (empty) migration

Musher's first migration establishes the alembic chain against
`snowline_musher.models.Base` (currently empty — spec §8 phase 1 is
scaffold-only, no Run table yet). Deliberately a no-op: it exists so
`alembic upgrade head` and later migrations have a genesis revision to chain
from, without inventing a schema ahead of the run-engine item that defines it
(spec §2).

Revision ID: 63ce644e5551
Revises:
Create Date: 2026-07-09
"""

from collections.abc import Sequence

revision: str = "63ce644e5551"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass

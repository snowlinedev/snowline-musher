"""Alembic wiring — the migration chain has exactly one head.

Uses `ScriptDirectory` directly (no DB connection needed) so this test runs
without a live Postgres, per the house convention. A DB-backed `migrated_db`
fixture (see conftest.py) additionally exercises `alembic upgrade head`
against a real database when Postgres is reachable, skipping cleanly when it
is not.
"""

from __future__ import annotations

from alembic.script import ScriptDirectory
from conftest import alembic_config


def test_exactly_one_head():
    script = ScriptDirectory.from_config(alembic_config())
    heads = script.get_heads()
    assert len(heads) == 1, f"expected exactly one alembic head, got {heads!r}"


def test_genesis_revision_has_no_down_revision():
    script = ScriptDirectory.from_config(alembic_config())
    (head,) = script.get_heads()
    revision = script.get_revision(head)
    # The baseline migration is the sole revision in this skeleton phase.
    assert revision.down_revision is None


def test_migration_chain_applies_cleanly(migrated_db):
    """DB-backed: `alembic upgrade head` (run by the `migrated_db` fixture)
    succeeds against a real database. Skips when Postgres is unreachable."""
    # If we get here, the migrated_db fixture already ran `upgrade head`
    # without raising — this test exists to give that a visible, named
    # assertion in the suite.
    assert migrated_db

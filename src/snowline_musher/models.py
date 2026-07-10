"""Musher's persisted models.

Empty in this skeleton phase (spec §8 phase 1: service scaffold, no run
engine, no Run table yet). `Base` exists now so the alembic chain has a
target metadata and a genesis migration can be baselined against it; the Run
model (spec §2) is a follow-up item's addition, not this one's.
"""

from __future__ import annotations

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass

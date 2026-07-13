"""Musher's persisted models.

`Run` (spec §2) is the plugin's single first-class noun: one row per dispatched
headless carrier run, from `queued` through a terminal state. The row is the
run's durable record — every terminal state is FAIL-VISIBLE (spec §2), so a
failed or timed-out run keeps its transcript/summary/workspace fields and its
row is never deleted; only the workspace *directory* is ever GC'd (see
`workspace.gc_workspaces`). Legal state transitions live in `runs.py`, not
here — this module owns the shape, not the lifecycle rules.

Enum columns are stored as plain strings (`native_enum=False`): the carrier
seam gains a second value later (spec §1) and states are a closed set, and a
VARCHAR + CHECK constraint evolves with an ordinary migration rather than the
`ALTER TYPE` dance a native Postgres enum would force.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, Integer, String, Text, Uuid, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Carrier(enum.StrEnum):
    """The execution backend. v1 is Claude-only (spec §1, decision 7c317bdb);
    the seam is one enum value + one carrier function so a second carrier
    (e.g. codex) slots in wholesale later."""

    claude = "claude"


class Origin(enum.StrEnum):
    """Where a run was dispatched from (spec §2). The originating ref (work-item
    id / GH issue) rides alongside in `origin_ref`."""

    mcp = "mcp"
    api = "api"
    watcher = "watcher"


class RunState(enum.StrEnum):
    """The run lifecycle (spec §2). `queued` and `running` are live; the rest
    are terminal. Legal transitions between these live in `runs.py`."""

    queued = "queued"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"
    timed_out = "timed_out"
    cancelled = "cancelled"


# A generous wall-clock default: implementation runs are long (spec §2), so the
# floor is "don't kill real work", not thrift. Callers dispatching a known-short
# objective pass their own timeout_s.
DEFAULT_TIMEOUT_S = 3600


def _enum_col(py_enum: type[enum.Enum], name: str) -> Enum:
    """A VARCHAR-backed enum column that stores the member VALUE (not its Python
    name) — keeps the DB literal equal to the spec's wire string and dodges
    native-Postgres-enum `ALTER TYPE` migrations when the set grows.
    `create_constraint=True` is explicit: SQLAlchemy 2.x defaults it OFF for
    non-native enums, which would leave a bare VARCHAR that accepts any string
    — a bad write would then poison the row into ORM-unreadability, breaking
    the fail-visible invariant the CHECK exists to protect."""
    return Enum(
        py_enum,
        name=name,
        native_enum=False,
        create_constraint=True,
        values_callable=lambda e: [m.value for m in e],
        length=32,
    )


class Run(Base):
    """One dispatched carrier run (spec §2)."""

    __tablename__ = "runs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)

    # --- inputs ---------------------------------------------------------
    # The prompt/task text handed to the carrier.
    objective: Mapped[str] = mapped_column(Text, nullable=False)
    # `owner/repo` to clone.
    repo: Mapped[str] = mapped_column(String(255), nullable=False)
    # Defaults to the repo default branch — resolved by the dispatcher, stored
    # concretely so the run is reproducible even if the default later moves.
    base_branch: Mapped[str] = mapped_column(String(255), nullable=False)
    # Snowline slug the run is for (soft ref, optional — spec §2). Deliberately
    # NOT a foreign key: musher is usable with no PM/governance present.
    # Indexed for the GET /runs?scope= filter (spec §4.1).
    scope: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    carrier: Mapped[Carrier] = mapped_column(
        _enum_col(Carrier, "carrier"), nullable=False, default=Carrier.claude
    )
    # `--model` passthrough; from the work item's recommended_model when
    # dispatched off an item. Nullable = let the carrier pick its own default.
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # Wall-clock seconds before the run is SIGKILLed → timed_out (spec §3).
    timeout_s: Mapped[int] = mapped_column(
        Integer, nullable=False, default=DEFAULT_TIMEOUT_S
    )
    origin: Mapped[Origin] = mapped_column(_enum_col(Origin, "origin"), nullable=False)
    # The originating ref — work-item id / GH issue (spec §2). Optional: a
    # hand-dispatched run has no upstream item.
    origin_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # --- lifecycle ------------------------------------------------------
    # Indexed for the GET /runs?state= filter (spec §4.1) and the GC scan.
    state: Mapped[RunState] = mapped_column(
        _enum_col(RunState, "run_state"),
        nullable=False,
        default=RunState.queued,
        index=True,
    )

    # --- outputs (all nullable until produced) --------------------------
    # Path of the run's isolated clone (spec §3). Set when the workspace is
    # created; SURVIVES terminal states as the autopsy pointer even after the
    # directory itself is GC'd.
    workspace: Mapped[str | None] = mapped_column(Text, nullable=True)
    branch: Mapped[str | None] = mapped_column(String(255), nullable=True)
    pr_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The persisted stream-json transcript of record (spec §3). Kept on every
    # terminal state — a failed run's transcript is the autopsy.
    transcript_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Carrier-authored closing summary.
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)

    # --- timestamps -------------------------------------------------------
    # timestamptz (timezone=True), NOT naive timestamp: a naive column makes
    # Postgres cast writes through the SESSION timezone, so on any non-UTC
    # server the stored wall time silently goes local while GC re-labels it
    # UTC — skewing retention math by the offset. timestamptz round-trips
    # tz-aware regardless of server/session timezone.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # Set on queued→running / cleared never; finished_at on entry to a terminal
    # state. Both nullable because a queued run has reached neither.
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"<Run {self.id} {self.state} {self.repo!r}>"

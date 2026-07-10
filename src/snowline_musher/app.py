"""The musher plugin app — v1 skeleton (spec §8 phase 1).

A FastAPI app that:
  - exposes `/health` (the platform supervisor polls this),
  - REGISTERS with the platform via a lifespan-long registration HEARTBEAT:
    the manifest is POSTed at boot and re-asserted every interval, so a
    platform restart (in-memory registry, boots empty) self-heals within one
    beat instead of requiring this plugin to also be kickstarted. Each beat
    is best-effort, so a briefly-down platform doesn't crash the plugin.

Musher has its OWN database; like the house plugin convention it boot-
migrates to the latest Alembic head in the lifespan, so a schema change
deploys on a plain restart. There is no schema yet (no Run table — a later
item); the migration chain is baselined empty so it exists.

This is scaffold-only: no REST /runs endpoints, no MCP surface, no run engine.
Those are separate follow-up items (spec §8 phases 2-3). `MUSHER_ENABLED`
(off by default, spec §3/§6) gates that future engine; nothing in this phase
does anything gated by it yet beyond existing as a config knob.
"""

from __future__ import annotations

import contextlib
import logging
from contextlib import asynccontextmanager
from pathlib import Path

import anyio
from fastapi import FastAPI

from snowline_musher import config, registration

log = logging.getLogger("snowline_musher.app")


class _HeartbeatHttpxLogFilter(logging.Filter):
    """Drops httpx's per-request INFO line for the registration heartbeat's
    `POST …/plugins` (one line per beat, forever) while letting every OTHER
    httpx request trace through."""

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        return not ("POST" in msg and "/plugins" in msg)


_HEARTBEAT_HTTPX_FILTER = _HeartbeatHttpxLogFilter()


def _migrate_to_head() -> None:
    """Bring the musher DB to the latest Alembic head — services in this
    codebase family boot-migrate in their lifespan. Reads the same DB URL the
    app's sessions use, so a schema change deploys on a plain restart."""
    from alembic import command
    from alembic.config import Config

    migrations = Path(__file__).resolve().parent / "migrations"
    cfg = Config()
    cfg.set_main_option("script_location", str(migrations))
    cfg.set_main_option("sqlalchemy.url", config.database_url())
    command.upgrade(cfg, "head")


def create_app(
    *,
    migrate_on_startup: bool = True,
    register_on_startup: bool = True,
) -> FastAPI:
    """Build the musher app. `migrate_on_startup=False` skips the boot-
    migrate (tests provision their own schema, or need none yet);
    `register_on_startup=False` skips the platform registration heartbeat
    entirely (tests assert registration separately, against a stubbed
    platform)."""
    # httpx logs every request at INFO — with the registration heartbeat that
    # is one line per beat forever. Drop ONLY the heartbeat's POST /plugins
    # lines so other httpx traffic still traces through.
    logging.getLogger("httpx").addFilter(_HEARTBEAT_HTTPX_FILTER)

    @asynccontextmanager
    async def _lifespan(app: FastAPI):
        if getattr(app.state, "migrate_on_startup", True):
            _migrate_to_head()
        async with contextlib.AsyncExitStack() as stack:
            run_registration = getattr(app.state, "register_on_startup", True)
            # The task group is UNCONDITIONAL (the house lifespan shape): only
            # the start_soon call is gated, so a future lifespan-long loop
            # can't silently never-start because it forgot to extend a flag
            # disjunction, and there is exactly ONE yield/teardown path
            # shared by production and the test factory. An empty group is
            # free.
            tg = await stack.enter_async_context(anyio.create_task_group())
            if run_registration:
                # The registration HEARTBEAT: first beat immediately (the
                # boot registration), then a re-assert every interval so a
                # platform restart — whose in-memory registry boots empty —
                # heals without this plugin being kickstarted too. Each beat
                # is best-effort and runs off the event loop. Riding the task
                # group means boot never blocks on a slow/down platform —
                # deliberately, /health can come up BEFORE the first beat
                # completes (the gateway self-heals within a beat).
                tg.start_soon(registration.registration_heartbeat)
            yield
            tg.cancel_scope.cancel()

    app = FastAPI(title="Snowline Musher", lifespan=_lifespan)
    app.state.migrate_on_startup = migrate_on_startup
    app.state.register_on_startup = register_on_startup

    @app.get("/health")
    async def health() -> dict:
        return {
            "status": "ok",
            "plugin": registration.PLUGIN_NAME,
            "enabled": config.musher_enabled(),
        }

    return app


app = create_app()

"""Musher plugin configuration — env-driven.

Musher has its OWN database, separate from the platform's and from any other
plugin's. This skeleton phase (spec §8 phase 1) wires the database and the
platform registration only; no run engine reads these values yet.

Env vars:
  MUSHER_DATABASE_URL      — the musher store (its own Postgres DB).
  SNOWLINE_PLATFORM_URL    — where the platform runs (the plugin registration
                             endpoint). Shared (unprefixed) across plugins, so
                             one deploy knob points every plugin at the same
                             platform.
  MUSHER_BASE_URL          — where THIS plugin runs, the `base_url` it hands
                             the platform at registration so the gateway can
                             proxy to it.
  MUSHER_BIND_HOST         — the host this service binds to. Defaults to the
                             loopback address (spec §4.1: "loopback-first
                             bind, tailnet exposure via tailscaled") — a
                             deploy that wants tailnet exposure sets this
                             explicitly rather than the service defaulting to
                             a wildcard bind.
  MUSHER_BIND_PORT         — the port this service binds to.
  SNOWLINE_REGISTRATION_HEARTBEAT_SECONDS — how often the registration
                             heartbeat re-asserts this plugin with the
                             platform. Shared (unprefixed) across plugins,
                             like SNOWLINE_PLATFORM_URL — one deploy knob
                             tunes every plugin's cadence.
  MUSHER_ENABLED           — off by default (spec §3: "Off by default";
                             §6: single-operator trust boundary). When unset
                             or falsy, the run engine (a later phase) must not
                             do anything; the service still starts and serves
                             /health regardless of this flag.
  MUSHER_RUNS_ROOT         — root directory under which each run gets its own
                             `<run-id>/workspace` clone (spec §3: "Workspace
                             per run"). Defaults to ~/.snowline/musher/runs;
                             tests override it to a tmp path so no real
                             home-dir state is touched.
  MUSHER_WORKSPACE_RETENTION_DAYS — how long a terminal run's workspace is kept
                             for autopsy before it is eligible for GC (spec §3:
                             "kept after terminal states for autopsy and GC'd
                             on a retention window"). Generous default; GC is a
                             callable only in this phase, never scheduled.
  MUSHER_CLAUDE_BIN        — the `claude` binary the carrier invokes (spec §3).
                             Defaults to `claude` (found on PATH); tests point
                             it at a stub executable so the suite NEVER shells
                             out to a real Claude Code run.
"""

import logging
import math
import os
from pathlib import Path

# Local libpq defaults (unix socket, current OS user, no password) — mirrors
# the platform/plugin house convention. A SEPARATE database: musher owns runs
# (once the run engine ships), no other service's tables.
DEFAULT_DATABASE_URL = "postgresql+psycopg:///snowline_musher"

# Where the platform lives — the POST /plugins registration endpoint.
DEFAULT_PLATFORM_URL = "http://127.0.0.1:8850"

# Where this plugin advertises itself to the platform (the manifest `base_url`).
DEFAULT_BASE_URL = "http://127.0.0.1:8804"

# Loopback-first bind (spec §4.1) — a deploy that wants tailnet/LAN exposure
# sets MUSHER_BIND_HOST explicitly; the service never defaults to a wildcard.
DEFAULT_BIND_HOST = "127.0.0.1"
DEFAULT_BIND_PORT = 8804

# Registration heartbeat cadence — matches the platform's health-poll default,
# so a platform restart heals in roughly one health round.
DEFAULT_REGISTRATION_HEARTBEAT_SECONDS = 15.0

# Workspace root (spec §3): each run clones into `<runs_root>/<run-id>/workspace`.
# A per-user default, not /tmp — workspaces are KEPT after terminal states for
# autopsy and only GC'd on a retention window, so they must survive a reboot.
DEFAULT_RUNS_ROOT = Path.home() / ".snowline" / "musher" / "runs"

# Retention window for terminal-run workspaces (spec §3). Generous on purpose:
# a terminal run is a readable record and its clone is the autopsy surface, so
# the floor is comfort, not disk thrift — GC below this age never fires.
DEFAULT_WORKSPACE_RETENTION_DAYS = 14

# The carrier binary (spec §3). A bare name resolves through PATH at exec time;
# tests override it to a stub so the suite never invokes a real Claude Code run.
DEFAULT_CLAUDE_BIN = "claude"

_TRUTHY = frozenset({"1", "true", "yes", "on"})


def database_url() -> str:
    return os.environ.get("MUSHER_DATABASE_URL", DEFAULT_DATABASE_URL)


def platform_url() -> str:
    return os.environ.get("SNOWLINE_PLATFORM_URL", DEFAULT_PLATFORM_URL).rstrip("/")


def base_url() -> str:
    return os.environ.get("MUSHER_BASE_URL", DEFAULT_BASE_URL).rstrip("/")


def bind_host() -> str:
    return os.environ.get("MUSHER_BIND_HOST", DEFAULT_BIND_HOST)


def bind_port() -> int:
    raw = os.environ.get("MUSHER_BIND_PORT")
    if not raw:
        return DEFAULT_BIND_PORT
    try:
        return int(raw)
    except ValueError:
        logging.getLogger(__name__).warning(
            "malformed MUSHER_BIND_PORT=%r — using the default %s",
            raw,
            DEFAULT_BIND_PORT,
        )
        return DEFAULT_BIND_PORT


def musher_enabled() -> bool:
    """Off by default (spec §3, §6): the run engine (a later phase) must
    stay dark unless MUSHER_ENABLED is explicitly set to a truthy value.
    Tests pin this off via an autouse fixture (pattern:
    SNOWLINE_SHADOW_TURNS_ENABLED)."""
    return os.environ.get("MUSHER_ENABLED", "").strip().lower() in _TRUTHY


def runs_root() -> Path:
    """Root under which per-run workspaces live (spec §3). Env-overridable so
    tests point it at a tmp path — nothing here creates the directory; the
    workspace module makes each run's tree on demand."""
    raw = os.environ.get("MUSHER_RUNS_ROOT")
    if not raw:
        return DEFAULT_RUNS_ROOT
    return Path(raw).expanduser()


def workspace_retention_days() -> int:
    """Days a terminal run's workspace is kept before GC eligibility (spec §3).
    LENIENT on a malformed value (warn + fall back), like bind_port: a typo in
    this knob must not make GC delete an autopsy clone early — the safe failure
    is the generous default, not a short window."""
    raw = os.environ.get("MUSHER_WORKSPACE_RETENTION_DAYS")
    if not raw:
        return DEFAULT_WORKSPACE_RETENTION_DAYS
    try:
        value = int(raw)
    except ValueError:
        logging.getLogger(__name__).warning(
            "malformed MUSHER_WORKSPACE_RETENTION_DAYS=%r — using the default %s",
            raw,
            DEFAULT_WORKSPACE_RETENTION_DAYS,
        )
        return DEFAULT_WORKSPACE_RETENTION_DAYS
    if value < 0:
        logging.getLogger(__name__).warning(
            "negative MUSHER_WORKSPACE_RETENTION_DAYS=%r — using the default %s",
            raw,
            DEFAULT_WORKSPACE_RETENTION_DAYS,
        )
        return DEFAULT_WORKSPACE_RETENTION_DAYS
    return value


def claude_bin() -> str:
    """The `claude` executable the carrier invokes (spec §3). A bare name is
    resolved via PATH by the exec layer; tests set MUSHER_CLAUDE_BIN to a stub
    path so the suite never shells out to a real carrier run. An empty/blank
    override falls back to the default rather than trying to exec `""`."""
    raw = os.environ.get("MUSHER_CLAUDE_BIN")
    if not raw or not raw.strip():
        return DEFAULT_CLAUDE_BIN
    return raw.strip()


def registration_heartbeat_seconds() -> float:
    """The heartbeat cadence. LENIENT on a malformed/absurd value (warn +
    fall back), unlike a fail-loud config rule: the heartbeat is the
    self-healing mechanism the registration loop exists for, so a typo in
    this shared env var must not kill the loop (a dead heartbeat = a hollow
    gateway after the next platform restart) — and a zero/negative value must
    not hot-loop POSTs."""
    raw = os.environ.get("SNOWLINE_REGISTRATION_HEARTBEAT_SECONDS")
    if raw is None:
        return DEFAULT_REGISTRATION_HEARTBEAT_SECONDS
    try:
        value = float(raw)
    except ValueError:
        logging.getLogger(__name__).warning(
            "malformed SNOWLINE_REGISTRATION_HEARTBEAT_SECONDS=%r — using the "
            "default %ss",
            raw,
            DEFAULT_REGISTRATION_HEARTBEAT_SECONDS,
        )
        return DEFAULT_REGISTRATION_HEARTBEAT_SECONDS
    if not math.isfinite(value):
        # "inf"/"nan" parse as floats and slip past the < 1.0 floor, but
        # anyio.sleep(inf/nan) never returns — a silent dead heartbeat, the
        # exact failure this lenient parse exists to prevent. Treat like
        # malformed input.
        logging.getLogger(__name__).warning(
            "non-finite SNOWLINE_REGISTRATION_HEARTBEAT_SECONDS=%r — using the "
            "default %ss",
            raw,
            DEFAULT_REGISTRATION_HEARTBEAT_SECONDS,
        )
        return DEFAULT_REGISTRATION_HEARTBEAT_SECONDS
    if value < 1.0:
        logging.getLogger(__name__).warning(
            "SNOWLINE_REGISTRATION_HEARTBEAT_SECONDS=%r is below the 1s floor "
            "— clamping (the heartbeat cannot be disabled by env; stop the "
            "plugin instead)",
            raw,
        )
        return 1.0
    return value

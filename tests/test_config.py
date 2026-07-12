"""Config parsing — env-driven, off-by-default gate, loopback-first bind."""

from __future__ import annotations

from pathlib import Path

from snowline_musher import config


def test_musher_enabled_defaults_off(monkeypatch):
    monkeypatch.delenv("MUSHER_ENABLED", raising=False)
    assert config.musher_enabled() is False


def test_musher_enabled_truthy_values(monkeypatch):
    for value in ("1", "true", "True", "yes", "YES", "on"):
        monkeypatch.setenv("MUSHER_ENABLED", value)
        assert config.musher_enabled() is True, f"{value!r} should be truthy"


def test_musher_enabled_falsy_values(monkeypatch):
    for value in ("0", "false", "no", "off", "", "garbage"):
        monkeypatch.setenv("MUSHER_ENABLED", value)
        assert config.musher_enabled() is False, f"{value!r} should be falsy"


def test_bind_host_defaults_to_loopback(monkeypatch):
    monkeypatch.delenv("MUSHER_BIND_HOST", raising=False)
    assert config.bind_host() == "127.0.0.1"


def test_bind_host_overridable(monkeypatch):
    monkeypatch.setenv("MUSHER_BIND_HOST", "0.0.0.0")
    assert config.bind_host() == "0.0.0.0"


def test_bind_port_default(monkeypatch):
    monkeypatch.delenv("MUSHER_BIND_PORT", raising=False)
    assert config.bind_port() == 8804


def test_bind_port_parses_int(monkeypatch):
    monkeypatch.setenv("MUSHER_BIND_PORT", "9000")
    assert config.bind_port() == 9000


def test_bind_port_malformed_falls_back(monkeypatch):
    monkeypatch.setenv("MUSHER_BIND_PORT", "not-a-port")
    assert config.bind_port() == 8804


def test_database_url_default(monkeypatch):
    monkeypatch.delenv("MUSHER_DATABASE_URL", raising=False)
    assert config.database_url() == "postgresql+psycopg:///snowline_musher"


def test_platform_url_default_and_strips_trailing_slash(monkeypatch):
    monkeypatch.delenv("SNOWLINE_PLATFORM_URL", raising=False)
    assert config.platform_url() == "http://127.0.0.1:8850"
    monkeypatch.setenv("SNOWLINE_PLATFORM_URL", "http://platform.example/")
    assert config.platform_url() == "http://platform.example"


def test_base_url_default_and_strips_trailing_slash(monkeypatch):
    monkeypatch.delenv("MUSHER_BASE_URL", raising=False)
    assert config.base_url() == "http://127.0.0.1:8804"
    monkeypatch.setenv("MUSHER_BASE_URL", "http://musher.example/")
    assert config.base_url() == "http://musher.example"


def test_runs_root_defaults_under_home(monkeypatch):
    monkeypatch.delenv("MUSHER_RUNS_ROOT", raising=False)
    assert config.runs_root() == Path.home() / ".snowline" / "musher" / "runs"


def test_runs_root_overridable_and_expands_user(monkeypatch):
    monkeypatch.setenv("MUSHER_RUNS_ROOT", "/tmp/musher-runs")
    assert config.runs_root() == Path("/tmp/musher-runs")
    monkeypatch.setenv("MUSHER_RUNS_ROOT", "~/musher-runs")
    assert config.runs_root() == Path.home() / "musher-runs"


def test_workspace_retention_days_default_and_lenient(monkeypatch):
    monkeypatch.delenv("MUSHER_WORKSPACE_RETENTION_DAYS", raising=False)
    assert config.workspace_retention_days() == 14
    monkeypatch.setenv("MUSHER_WORKSPACE_RETENTION_DAYS", "30")
    assert config.workspace_retention_days() == 30
    # A malformed or negative window must not shorten retention (GC would then
    # delete an autopsy clone early) — warn and fall back to the generous default.
    monkeypatch.setenv("MUSHER_WORKSPACE_RETENTION_DAYS", "not-a-number")
    assert config.workspace_retention_days() == 14
    monkeypatch.setenv("MUSHER_WORKSPACE_RETENTION_DAYS", "-5")
    assert config.workspace_retention_days() == 14


def test_heartbeat_interval_env_is_lenient(monkeypatch):
    # A malformed or hot-looping value in the SHARED env var must not kill the
    # heartbeat (a dead heartbeat = a hollow gateway after the next platform
    # restart) — warn and fall back instead.
    monkeypatch.delenv("SNOWLINE_REGISTRATION_HEARTBEAT_SECONDS", raising=False)
    assert config.registration_heartbeat_seconds() == 15.0
    monkeypatch.setenv("SNOWLINE_REGISTRATION_HEARTBEAT_SECONDS", "15s")
    assert config.registration_heartbeat_seconds() == 15.0  # malformed -> default
    monkeypatch.setenv("SNOWLINE_REGISTRATION_HEARTBEAT_SECONDS", "0")
    assert config.registration_heartbeat_seconds() == 1.0  # floored, no hot loop
    monkeypatch.setenv("SNOWLINE_REGISTRATION_HEARTBEAT_SECONDS", "inf")
    assert config.registration_heartbeat_seconds() == 15.0  # non-finite -> default
    monkeypatch.setenv("SNOWLINE_REGISTRATION_HEARTBEAT_SECONDS", "nan")
    assert config.registration_heartbeat_seconds() == 15.0  # non-finite -> default
    monkeypatch.setenv("SNOWLINE_REGISTRATION_HEARTBEAT_SECONDS", "30")
    assert config.registration_heartbeat_seconds() == 30.0

"""The musher app's HTTP surface (skeleton phase): /health, and the lifespan's
registration heartbeat. No DB needed — `migrate_on_startup=False` skips the
boot-migrate, matching the house test-factory idiom.
"""

from __future__ import annotations

import anyio
import httpx

from snowline_musher.app import create_app


def _app(**kwargs):
    kwargs.setdefault("migrate_on_startup", False)
    kwargs.setdefault("register_on_startup", False)
    return create_app(**kwargs)


def _http(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://musher",
        timeout=httpx.Timeout(30.0),
    )


def test_health_returns_200():
    app = _app()

    async def go() -> dict:
        async with app.router.lifespan_context(app):
            async with _http(app) as http:
                r = await http.get("http://musher/health")
                return {"status": r.status_code, "body": r.json()}

    res = anyio.run(go)
    assert res["status"] == 200
    assert res["body"]["plugin"] == "musher"
    assert res["body"]["status"] == "ok"


def test_health_reports_musher_enabled_flag(monkeypatch):
    monkeypatch.setenv("MUSHER_ENABLED", "1")
    app = _app()

    async def go() -> dict:
        async with app.router.lifespan_context(app):
            async with _http(app) as http:
                r = await http.get("http://musher/health")
                return r.json()

    assert anyio.run(go)["enabled"] is True


def test_health_enabled_false_by_default(monkeypatch):
    monkeypatch.delenv("MUSHER_ENABLED", raising=False)
    app = _app()

    async def go() -> dict:
        async with app.router.lifespan_context(app):
            async with _http(app) as http:
                r = await http.get("http://musher/health")
                return r.json()

    assert anyio.run(go)["enabled"] is False


def test_lifespan_starts_and_cancels_registration_heartbeat(monkeypatch):
    """`create_app(register_on_startup=True)`'s lifespan starts the
    registration heartbeat (it fires at least once) and cancels it cleanly on
    shutdown (the lifespan exits without hanging or raising). The heartbeat
    itself is monkeypatched so no real HTTP is attempted."""
    from snowline_musher import registration

    calls = {"beats": 0}

    async def fake_heartbeat(*args, **kwargs):
        try:
            while True:
                calls["beats"] += 1
                await anyio.sleep(0.01)
        finally:
            pass

    monkeypatch.setattr(registration, "registration_heartbeat", fake_heartbeat)

    app = _app(register_on_startup=True)

    async def go() -> None:
        async with app.router.lifespan_context(app):
            with anyio.fail_after(5):
                while calls["beats"] < 1:
                    await anyio.sleep(0.01)
        # Exiting the lifespan context = the task group cancelled cleanly.

    anyio.run(go)
    assert calls["beats"] >= 1


def test_lifespan_skips_registration_when_disabled(monkeypatch):
    from snowline_musher import registration

    calls = {"beats": 0}

    async def fake_heartbeat(*args, **kwargs):
        calls["beats"] += 1

    monkeypatch.setattr(registration, "registration_heartbeat", fake_heartbeat)

    app = _app(register_on_startup=False)

    async def go() -> None:
        async with app.router.lifespan_context(app):
            await anyio.sleep(0.05)

    anyio.run(go)
    assert calls["beats"] == 0

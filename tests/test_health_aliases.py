"""Liveness aliases and their separation from dependency readiness."""

from contextlib import asynccontextmanager
from unittest.mock import Mock

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event

from server import main
from server.observability.correlation import infer_page_identity


@pytest.mark.parametrize("path", ["/health", "/healthz", "/api/health"])
async def test_liveness_is_public_and_does_not_access_database(path, monkeypatch):
    monkeypatch.setenv("GIT_SHA", "a" * 40)
    monkeypatch.setenv("APP_IMAGE_TAG", "synthetic-version")
    db = Mock(side_effect=AssertionError("liveness accessed the database"))
    monkeypatch.setattr(main, "get_db", db)
    statements = []

    def record_query(*args):
        statements.append(args[2])

    engines = [main.sync_engine]
    if main.engine is not None:
        engines.append(main.engine.sync_engine)
    for engine in engines:
        event.listen(engine, "before_cursor_execute", record_query)
    try:
        async with AsyncClient(transport=ASGITransport(app=main.app), base_url="http://test") as client:
            expected = await client.get("/health")
            response = await client.get(path)
    finally:
        for engine in engines:
            event.remove(engine, "before_cursor_execute", record_query)

    assert response.status_code == expected.status_code == 200
    assert response.headers["content-type"] == expected.headers["content-type"] == "application/json"
    assert response.json() == expected.json() == {"status": "ok", "service": main.cfg.app_name, "build": {"source_commit": "a" * 40, "version": "synthetic-version"}}
    db.assert_not_called()
    assert statements == []


@pytest.mark.parametrize("path", ["/health", "/healthz", "/api/health"])
def test_liveness_is_classified_as_health(path):
    assert infer_page_identity(path) == ("health", "health")


async def test_readiness_still_checks_database(monkeypatch):
    db = Mock()

    @asynccontextmanager
    async def unavailable_database():
        db()
        raise RuntimeError("database unavailable")
        yield

    monkeypatch.setattr(main, "get_db", unavailable_database)
    async with AsyncClient(transport=ASGITransport(app=main.app), base_url="http://test") as client:
        response = await client.get("/ready")
    assert response.status_code == 200
    assert response.json()["ready"] is False
    assert response.json()["database"] == "disconnected"
    assert "build" not in response.json()
    db.assert_called_once_with()
    assert infer_page_identity("/ready") == ("readiness", "health")


@pytest.mark.parametrize("path", ["/health", "/healthz", "/api/health"])
async def test_liveness_unknown_build_when_unset(path, monkeypatch):
    monkeypatch.delenv("GIT_SHA", raising=False)
    monkeypatch.delenv("APP_IMAGE_TAG", raising=False)
    async with AsyncClient(transport=ASGITransport(app=main.app), base_url="http://test") as client:
        response = await client.get(path)
    assert response.json()["build"] == {"source_commit": "unknown", "version": "unknown"}

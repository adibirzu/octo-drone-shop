"""Database portability and readiness contracts."""
from contextlib import asynccontextmanager
import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from server.config import Config
from server.database import build_oracle_connect_args
from server.modules import shop


@pytest.mark.parametrize("postgres", [True, False])
def test_storefront_uses_portable_sql(monkeypatch, postgres):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    connection = engine.connect()
    connection.execute(text("CREATE TABLE products (id INTEGER, name TEXT, sku TEXT, description TEXT, price REAL, stock INTEGER, category TEXT, image_url TEXT, is_active INTEGER)"))
    connection.execute(text("CREATE TABLE orders (total REAL)"))
    connection.execute(text("INSERT INTO products VALUES (1, 'Drone', 'D1', 'Test', 12, 3, 'drones', '', 1)"))
    connection.execute(text("INSERT INTO orders VALUES (12)"))
    statements = []

    class Session:
        async def execute(self, query):
            statements.append(str(query))
            return connection.execute(query)

    @asynccontextmanager
    async def get_db():
        yield Session()

    monkeypatch.setattr(shop, "get_db", get_db)
    monkeypatch.setattr(shop, "sync_customers_from_crm", AsyncMock(return_value={"configured": False}))
    monkeypatch.setattr(shop.cfg, "_pg_url", "postgresql://example" if postgres else "")
    monkeypatch.setattr(shop.cfg, "oracle_dsn", "" if postgres else "example")
    app = FastAPI()
    app.include_router(shop.router)
    try:
        response = TestClient(app).get("/api/shop/storefront")
        assert response.status_code == 200, response.text
        assert response.json()["stats"] == {"product_count": 1, "inventory_units": 3, "revenue": 12, "order_count": 1}
        assert response.json()["backend"]["database"] == ("postgresql" if postgres else "oracle_atp")
        assert len(statements) == 4
        assert all("from dual" not in sql.lower() for sql in statements)
    finally:
        connection.close()
        engine.dispose()


@pytest.mark.parametrize("dsn", ["tcps://db.example.test/service", "(description=(address=(protocol=tcps)(host=db.example.test)(port=1521))(connect_data=(service_name=service)))"])
@pytest.mark.parametrize("wallet", ["unset", "missing", "empty"])
def test_walletless_connect_args(monkeypatch, tmp_path, dsn, wallet):
    cfg = Config()
    cfg.oracle_dsn = dsn
    cfg.oracle_password = "test-password"
    cfg.oracle_wallet_dir = {"unset": "", "missing": str(tmp_path / "missing"), "empty": str(tmp_path)}[wallet]
    cfg.environment = "test"
    import oracledb
    monkeypatch.setattr(oracledb.defaults, "config_dir", "sentinel")
    cfg.validate()
    assert cfg.oracle_tls_mode == "tls_walletless"
    assert cfg.safe_runtime_summary()["walletless_tls"] is True
    assert build_oracle_connect_args(cfg) == {"dsn": dsn}
    assert oracledb.defaults.config_dir == "sentinel"


def test_wallet_and_tns_config(monkeypatch, tmp_path):
    import oracledb
    monkeypatch.setattr(oracledb.defaults, "config_dir", "sentinel")
    cfg = Config()
    cfg.oracle_dsn = "service"
    cfg.oracle_wallet_dir = str(tmp_path)
    cfg.oracle_wallet_password = "test-password"
    (tmp_path / "tnsnames.ora").write_text("", encoding="utf-8")
    assert build_oracle_connect_args(cfg) == {"dsn": "service", "config_dir": str(tmp_path)}
    assert oracledb.defaults.config_dir == "sentinel"
    (tmp_path / "ewallet.pem").write_text("", encoding="utf-8")
    assert cfg.oracle_tls_mode == "mTLS"
    assert build_oracle_connect_args(cfg) == {"dsn": "service", "config_dir": str(tmp_path), "wallet_location": str(tmp_path), "wallet_password": "test-password"}
    assert oracledb.defaults.config_dir == str(tmp_path)


@pytest.mark.parametrize("postgres,fail,expected_sql,expected_status", [(True, False, "SELECT 1", 200), (True, True, "SELECT 1", 503), (False, False, "SELECT 1 FROM DUAL", 200), (False, True, "SELECT 1 FROM DUAL", 503)])
def test_readiness_and_liveness(monkeypatch, postgres, fail, expected_sql, expected_status):
    from fastapi.responses import JSONResponse
    from opentelemetry import trace
    from server.config import cfg
    source = Path("server/main.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    functions = [node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name in ("health", "ready")]
    for function in functions:
        function.decorator_list = []
    namespace = {"cfg": cfg, "get_tracer": lambda: trace.get_tracer(__name__), "text": text,
                 "JSONResponse": JSONResponse, "runtime_snapshot": lambda: {}}
    exec(compile(ast.Module(body=functions, type_ignores=[]), "server/main.py", "exec"), namespace)
    main = SimpleNamespace(cfg=cfg)
    statements = []
    class Session:
        async def execute(self, query):
            statements.append(str(query))
            if fail:
                raise RuntimeError("unavailable")
    @asynccontextmanager
    async def get_db():
        yield Session()
    namespace["get_db"] = get_db
    monkeypatch.setattr(main.cfg, "_pg_url", "postgresql://example" if postgres else "")
    monkeypatch.setattr(main.cfg, "oracle_dsn", "" if postgres else "example")
    app = FastAPI()
    app.add_api_route("/ready", namespace["ready"], methods=["GET"])
    app.add_api_route("/health", namespace["health"], methods=["GET"])
    client = TestClient(app)
    response = client.get("/ready")
    assert response.status_code == expected_status
    assert response.json()["ready"] is (not fail)
    assert statements == [expected_sql]
    assert client.get("/health").status_code == 200

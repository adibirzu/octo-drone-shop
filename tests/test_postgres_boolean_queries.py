"""Exercise catalog flags against SQLite and the PostgreSQL and Oracle SQL dialects."""

import re
from contextlib import asynccontextmanager, contextmanager
from types import SimpleNamespace

import pytest
from asyncpg.exceptions import DatatypeMismatchError, UndefinedFunctionError, UndefinedTableError
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, create_mock_engine, event, insert, select, text
from sqlalchemy.dialects import oracle, sqlite
from sqlalchemy.dialects.postgresql.asyncpg import PGDialect_asyncpg
from sqlalchemy.sql.ddl import CreateTable
from sqlalchemy.sql.elements import TextClause
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from server import assistant_service, database, store_service
from server.active_flags import detect_postgres_integer_flags
from server.crm_catalog_sync import apply_catalog_sync
from server.database import Base, Coupon, Product, Service, User, Warehouse
from server.modules import admin, analytics, catalogue, dashboard, integrations, services, shipping, shop, synthetic_users
from server.observability import oci_monitoring


def inserted_flag_values(sql):
    insert_match = re.match(
        r'\s*INSERT\s+INTO\s+[^()]+\(([^()]*)\)\s*VALUES\b', sql, re.I
    )
    if not insert_match:
        return
    columns = [column.strip().strip('"').lower() for column in insert_match[1].split(",")]
    if "is_active" not in columns:
        return
    flag_index = columns.index("is_active")
    tokens = re.finditer(r"""'(?:[^']|'')*'|"([^"]|"")*"|[(),]|[^\s(),]+""", sql[insert_match.end():])
    depth = 0
    row = []
    value = []
    for match in tokens:
        token = match[0]
        if not depth and token not in ("(", ","):
            break
        if token == "(":
            if depth:
                value.append(token)
            depth += 1
        elif token == ")" and depth == 1:
            row.append("".join(value))
            yield row[flag_index]
            depth = 0
            row, value = [], []
        elif token == "," and depth == 1:
            row.append("".join(value))
            value = []
        else:
            if depth:
                value.append(token)
            if token == ")":
                depth -= 1


class DialectCheckedSession:
    """Run fixture queries on SQLite after checking their target dialect SQL."""

    def __init__(self, session, dialect):
        self.session = session
        self.dialect = dialect

    def _execute(self, statement, params=None):
        if isinstance(params, dict):
            params = {name: value for name, value in params.items() if name in statement.compile().params}
        query = statement.params(**(params or {}))
        compiled = query.compile(dialect=self.dialect, compile_kwargs={"render_postcompile": True})
        sql = str(compiled)
        if self.dialect.name == "postgresql":
            if any(re.fullmatch(r"[+-]?\d+", value) for value in inserted_flag_values(sql)):
                raise DatatypeMismatchError("PostgreSQL BOOLEAN flag received an integer literal")
            if re.search(r"\bis_active\s*=\s*[01]\b", sql, re.I):
                raise UndefinedFunctionError("operator does not exist: boolean = integer")
            for name, value in compiled.params.items():
                if name.startswith(("is_active", "active_flag")) and type(value) is not bool:
                    raise TypeError("PostgreSQL BOOLEAN flag received a non-boolean bind")
            if re.search(r"FROM\s+DUAL", sql, re.I):
                raise UndefinedTableError("DUAL is unavailable on PostgreSQL")
        elif self.dialect.name == "oracle":
            assert not re.search(r"\bis_active\s*=\s*(true|false)\b", sql, re.I)
        # SQLite can execute both flag literal forms. Adapt clauses that only
        # the target dialect supports after checking the target's compiled SQL.
        if self.dialect.name == "oracle" and sql == "SELECT 1 FROM DUAL":
            return self.session.execute(text("SELECT 1"))
        if isinstance(statement, TextClause) and sql.endswith("FETCH FIRST 1 ROWS ONLY"):
            statement = text(statement.text.removesuffix("FETCH FIRST 1 ROWS ONLY") + "LIMIT 1")
        return self.session.execute(statement, params)

    async def execute(self, statement, params=None):
        return self._execute(statement, params)

    async def scalar(self, statement):
        return (await self.execute(statement)).scalar()

    async def rollback(self):
        self.session.rollback()

    def add(self, instance):
        self.session.add(instance)


@pytest.fixture(params=[PGDialect_asyncpg(), sqlite.dialect(), oracle.dialect()], ids=lambda dialect: dialect.name)
def dialect(request):
    return request.param


@pytest.fixture
def sqlite_engine():
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE shops (name TEXT, address TEXT, coordinates TEXT, "
            "contact_email TEXT, contact_phone TEXT)"
        ))
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def catalog(dialect, sqlite_engine, monkeypatch, app_factory):
    engine = sqlite_engine
    with Session(engine) as session:
        session.add_all(
            Product(id=index, name=f"Survey Drone {index:02}", sku=f"DRN-{index:02}",
                    price=100 + index, stock=index + 5, category="Survey", is_active=1)
            for index in range(1, 11)
        )
        session.add_all([
            Product(id=11, name="Retired Survey Drone", sku="RETIRED",
                    price=999, stock=1, category="Legacy", is_active=0),
            Product(id=12, name="Unpublished Survey Drone", sku="UNPUBLISHED",
                    price=998, stock=1, category="Unpublished"),
        ])
        session.flush()
        session.query(Product).filter_by(id=12).update({Product.is_active: None})
        session.add_all([
            Service(name="Active Service", sku="SVC-ACTIVE", price=120, is_active=1),
            Service(name="Retired Service", sku="SVC-RETIRED", price=150, is_active=0),
            Warehouse(name="Active Warehouse", region="eu-central", is_active=1),
            Warehouse(name="Closed Warehouse", region="eu-central", is_active=0),
            Coupon(code="ACTIVE", discount_percent=10, is_active=1),
            Coupon(code="EXPIRED", discount_percent=10, is_active=0),
        ])
        session.commit()

    @asynccontextmanager
    async def get_db():
        with Session(engine) as session:
            yield DialectCheckedSession(session, dialect)
            session.commit()

    for module in (database, admin, analytics, assistant_service, catalogue, dashboard, integrations, services, shipping, shop):
        monkeypatch.setattr(module, "get_db", get_db)
    monkeypatch.setattr(assistant_service, "genai_configured", lambda: False)

    class CRMClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def get(self, url):
            return SimpleNamespace(status_code=200, json=lambda: {"id": 1})

    monkeypatch.setattr(integrations, "_crm_url", lambda: "https://crm.example.invalid")
    monkeypatch.setattr(integrations, "httpx", SimpleNamespace(AsyncClient=lambda **kwargs: CRMClient()))

    app = app_factory(
        admin.router, analytics.router, catalogue.router, dashboard.router, integrations.router,
        services.router, shipping.router, shop.router, synthetic_users.router,
    )
    with TestClient(app) as client:
        yield client, get_db


def test_products_filter_and_sort(catalog):
    client, _ = catalog
    response = client.get("/api/products", params={"search": "Survey Drone", "sort_by": "price"})
    assert response.status_code == 200
    assert [product["id"] for product in response.json()["products"]] == list(range(1, 11))


def test_categories_omit_inactive_and_null_flags(catalog):
    client, _ = catalog
    response = client.get("/api/categories")
    assert response.status_code == 200
    assert response.json() == {"categories": ["Survey"]}


def test_featured_products_limit_and_sort_by_price(catalog):
    client, _ = catalog
    response = client.get("/api/shop/featured")
    assert response.status_code == 200
    assert [product["id"] for product in response.json()["products"]] == list(range(10, 2, -1))


def test_dashboard_summary_counts_only_active_stock(catalog):
    client, _ = catalog
    response = client.get("/api/dashboard/summary")
    assert response.status_code == 200
    assert response.json()["products"] == {"total": 10, "low_stock": 5}
    assert [product["id"] for product in response.json()["featured_products"]] == list(range(10, 4, -1))


def test_analytics_count_and_coverage_exclude_inactive_products(catalog):
    client, _ = catalog
    assert client.get("/api/analytics/overview").json()["total_products"] == 10
    response = client.get("/api/analytics/security/correlations")
    assert response.status_code == 200
    assert {product["id"] for product in response.json()["product_coverage"]} == set(range(1, 11))


def test_crm_ticket_recommendations_exclude_inactive_products(catalog):
    client, _ = catalog
    response = client.get("/api/integrations/crm/ticket-products", params={"ticket_id": 1})
    assert response.status_code == 200
    assert "recommended_products" in response.json(), response.json()
    products = response.json()["recommended_products"]
    assert len(products) == 3
    assert all(1 <= product["id"] <= 10 for product in products)


@pytest.mark.asyncio
async def test_assistant_looks_up_active_products(catalog):
    response = await assistant_service.run_assistant_query(
        {"message": "Compare survey drones", "product_focus": "Survey Drone"}
    )
    assert response["provider"] == "local_grounded_fallback"
    assert response["documents_used"] == 8
    assert "Survey Drone 10" in response["answer"]
    assert "Retired Survey Drone" not in response["answer"]


@pytest.mark.asyncio
async def test_crm_catalog_can_activate_then_deactivate_product(catalog):
    client, get_db = catalog
    product = {"name": "Inspection Drone", "sku": "TEST-INSPECTION", "price": 500, "stock": 2}
    async with get_db() as db:
        assert (await apply_catalog_sync(db, products=[product]))["created"] == 1
    assert len(client.get("/api/products", params={"search": product["name"]}).json()["products"]) == 1

    async with get_db() as db:
        assert (await apply_catalog_sync(db, products=[product], action="deactivate"))["deactivated"] == 1
    assert client.get("/api/products", params={"search": product["name"]}).json()["products"] == []


def test_seed_data_populates_an_empty_schema(sqlite_engine, monkeypatch):
    monkeypatch.setattr(database, "sync_engine", sqlite_engine)

    database.seed_data()

    with Session(sqlite_engine) as session:
        for model in (User, Product, Service, Warehouse):
            flags = session.scalars(select(model.is_active)).all()
            assert len(flags) > 1
            assert set(flags) == {1}
        assert dict(session.execute(select(Coupon.code, Coupon.is_active)).all()) == {
            "FIRSTFLIGHT": 1,
            "FLEETDEAL": 1,
            "FREESHIP": 1,
            "GOVPILOT": 1,
            "SUMMER2024": 0,
        }


def test_repeated_seed_does_not_update_unchanged_rows(sqlite_engine, monkeypatch):
    monkeypatch.setattr(database, "sync_engine", sqlite_engine)
    with Session(sqlite_engine) as session:
        for model, rows in (
            (User, database.SEED_USERS),
            (Product, database.SEED_PRODUCTS),
            (Service, database.SEED_SERVICES),
        ):
            session.add_all(model(**row, is_active=1) for row in rows)
        session.add(Product(
            name="Legacy Product", sku=sorted(database.LEGACY_MUSHOP_SKUS)[0],
            price=100, is_active=0,
        ))
        session.commit()

    writes = []

    @event.listens_for(sqlite_engine, "after_cursor_execute")
    def record_writes(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("UPDATE") and cursor.rowcount:
            writes.append(statement)

    database.seed_data()

    assert writes == []


def test_readiness_uses_a_probe_accepted_by_the_bound_dialect(
    dialect, sqlite_engine, monkeypatch, app_factory
):
    from server import main

    @asynccontextmanager
    async def get_db():
        with Session(sqlite_engine) as session:
            yield DialectCheckedSession(session, dialect)

    monkeypatch.setattr(main, "get_db", get_db)
    app = app_factory()
    app.add_api_route("/ready", main.ready)
    with TestClient(app) as client:
        response = client.get("/ready")

    assert response.status_code == 200
    assert response.json()["ready"] is True
    assert response.json()["database"] == "connected"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "values",
    [
        "(concat('Commas, included', '!'), 1, 'TEST-INSERT', 120)",
        "('Active', '1', 'TEST-ACTIVE', 120), ('Inactive', 0, 'TEST-INACTIVE', 120)",
    ],
)
async def test_postgres_checker_rejects_integer_literals_inserted_into_flags(sqlite_engine, values):
    statement = text("INSERT INTO products (name, is_active, sku, price) VALUES " + values)
    with Session(sqlite_engine) as session:
        db = DialectCheckedSession(session, PGDialect_asyncpg())
        with pytest.raises(DatatypeMismatchError):
            await db.execute(statement)


@pytest.mark.asyncio
async def test_postgres_checker_accepts_integer_values_in_other_columns(sqlite_engine):
    with Session(sqlite_engine) as session:
        db = DialectCheckedSession(session, PGDialect_asyncpg())
        await db.execute(
            text("INSERT INTO products (name, is_active, sku, price) VALUES ('Commas, included', '1', 'TEST-INSERT', 1)")
        )
        assert session.scalar(select(Product.is_active)) == 1


def test_admin_can_create_an_active_user(catalog, sqlite_engine, monkeypatch):
    client, _ = catalog
    monkeypatch.setattr(admin, "_require_admin", lambda request: {"sub": "1", "username": "fixture-admin"})
    monkeypatch.setattr(admin, "bcrypt", SimpleNamespace(hashpw=lambda *_: b"fixture-hash", gensalt=lambda: b""))
    response = client.post("/api/admin/users", json={
        "username": "fixture-user", "email": "user@example.invalid", "password": "fixture",
    })

    assert response.status_code == 200
    with Session(sqlite_engine) as session:
        assert session.scalar(select(User.is_active).where(User.username == "fixture-user")) == 1


@pytest.mark.asyncio
async def test_services_and_warehouses_exclude_inactive_rows(catalog):
    client, _ = catalog
    service_response = client.get("/api/services/catalog")
    warehouses = await shipping.list_warehouses()

    assert service_response.status_code == 200
    assert [service["name"] for service in service_response.json()["services"]] == ["Active Service"]
    assert [warehouse["name"] for warehouse in warehouses["warehouses"]] == ["Active Warehouse"]


@pytest.mark.asyncio
async def test_synthetic_activity_selects_active_products_and_creates_users(catalog, sqlite_engine):
    _, get_db = catalog
    user = synthetic_users.generate_synthetic_users(count=1, domain="example.invalid")[0]
    async with get_db() as db:
        first_id, created = await synthetic_users._upsert_synthetic_user(db, user, "fixture-hash")
        assert created
        assert (await synthetic_users._upsert_synthetic_user(db, user, "unused")) == (first_id, False)
        assert [product["id"] for product in await synthetic_users._active_products(db, limit=25)] == list(range(1, 11))
    with Session(sqlite_engine) as session:
        assert session.scalar(select(User.is_active).where(User.id == first_id)) == 1


@pytest.mark.asyncio
async def test_store_rejects_inactive_products_and_coupons(catalog):
    _, get_db = catalog
    async with get_db() as db:
        items = await store_service.resolve_direct_items(db, [{"product_id": 1}, {"product_id": 11}])
        assert [item["product_id"] for item in items] == [1]
        assert (await store_service.apply_coupon(db, "ACTIVE", 100))["discount"] == 10.0
        assert (await store_service.apply_coupon(db, "EXPIRED", 100))["valid"] is False


def test_monitoring_publishes_only_active_low_stock_count(catalog, dialect, sqlite_engine, monkeypatch):
    import oci

    published = {}

    def post_metric_data(details):
        published.update({metric.name: metric.datapoints[0].value for metric in details.metric_data})
        monkeypatch.setattr(oci_monitoring, "_running", False)
        return SimpleNamespace(data=SimpleNamespace(failed_metrics_count=0))

    monkeypatch.setattr(oci_monitoring, "_running", True)
    monkeypatch.setattr(oci_monitoring, "_low_stock_count", -1)
    ticks = iter((True,))

    def sleep(_):
        if not next(ticks, False):
            monkeypatch.setattr(oci_monitoring, "_running", False)

    monkeypatch.setattr(oci_monitoring, "time", SimpleNamespace(sleep=sleep, time=lambda: 0))
    monkeypatch.setattr(oci_monitoring, "cfg", SimpleNamespace(
        oci_auth_mode="config_file", otel_service_name="fixture-shop", app_env="test",
        app_runtime="test", service_instance_id="fixture",
    ))
    monkeypatch.setattr(oci_monitoring, "_resolve_monitoring_region", lambda: "test-region")
    monkeypatch.setattr(oci.config, "from_file", lambda: {})
    monkeypatch.setattr(oci.monitoring, "MonitoringClient", lambda *args, **kwargs: SimpleNamespace(
        post_metric_data=post_metric_data,
    ))
    with Session(sqlite_engine) as session:
        db = DialectCheckedSession(session, dialect)

        @contextmanager
        def connect():
            yield SimpleNamespace(execute=db._execute)

        monkeypatch.setattr(database, "sync_engine", SimpleNamespace(connect=connect))
        oci_monitoring._publisher_loop("fixture-compartment", 0)

    assert published["app.inventory.low_stock_products"] == 4


def test_flag_columns_compile_ddl_and_batch_binds(dialect):
    batches = []
    mock = create_mock_engine(f"{dialect.name}://", lambda sql, *rows: batches.append((sql, rows)))
    Base.metadata.create_all(mock)
    dialect = mock.dialect
    for model in (User, Product, Service, Warehouse, Coupon):
        table = model.__table__
        ddl = str(CreateTable(table).compile(dialect=dialect))
        storage = "BOOLEAN" if dialect.name == "postgresql" else "INTEGER"
        assert f"is_active {storage}" in ddl
        assert table.c.is_active.default.arg is True

        mock.execute(insert(table), [{"is_active": True}, {"is_active": False}, {"is_active": None}])
        statement, (params,) = batches[-1]
        compiled = dialect.statement_compiler(
            dialect, statement, column_keys=["is_active"], for_executemany=True
        )
        assert not compiled.literal_execute_params
        process = table.c.is_active.type.dialect_impl(dialect).bind_processor(dialect)
        flags = [process(row["is_active"]) for row in params]
        if dialect.name == "postgresql":
            assert flags[0] is True and flags[1] is False
        else:
            assert [type(flag) for flag in flags[:2]] == [int, int]
            assert flags[:2] == [1, 0]
        assert flags[2] is None



def test_postgres_legacy_integer_flags_keep_numeric_binds():
    engine = create_engine("postgresql://")
    detect_postgres_integer_flags(engine)
    sql = []
    cursor = SimpleNamespace(
        execute=sql.append, fetchall=lambda: [("products",)], close=lambda: None
    )
    connection = SimpleNamespace(cursor=lambda: cursor, rollback=lambda: None)
    try:
        engine.pool.dispatch.first_connect(connection, None)

        assert "pg_catalog.pg_attribute" in sql[0]
        assert "is_active INTEGER" in str(CreateTable(Product.__table__).compile(dialect=engine.dialect))
        assert "is_active BOOLEAN" in str(CreateTable(User.__table__).compile(dialect=engine.dialect))
        column_type = Product.__table__.c.is_active.type.dialect_impl(engine.dialect)
        process = column_type.bind_processor(engine.dialect)
        assert type(process(True)) is int
        assert [process(value) for value in (True, False, None)] == [1, 0, None]
    finally:
        engine.dispose()

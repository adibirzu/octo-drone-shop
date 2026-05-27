from sqlalchemy import create_engine, inspect, text

from server.database import _ensure_missing_columns


def test_ensure_missing_columns_adds_current_order_payment_fields() -> None:
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE products (id INTEGER PRIMARY KEY, name VARCHAR(100))"))
        conn.execute(
            text(
                "CREATE TABLE orders ("
                "id INTEGER PRIMARY KEY, "
                "payment_method VARCHAR(50), "
                "payment_status VARCHAR(50)"
                ")"
            )
        )

    _ensure_missing_columns(engine)

    order_columns = {column["name"] for column in inspect(engine).get_columns("orders")}
    assert {
        "payment_method",
        "payment_status",
        "payment_provider",
        "payment_provider_reference",
    }.issubset(order_columns)

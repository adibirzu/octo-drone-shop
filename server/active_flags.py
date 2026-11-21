"""SQL expressions and ORM types for is_active columns."""

from sqlalchemy import Boolean, Integer, event, text
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.sql.elements import ColumnElement, TextClause
from sqlalchemy.sql.visitors import InternalTraversal
from sqlalchemy.types import TypeDecorator


class _FlagLiteral(ColumnElement[int]):
    type = Integer()
    inherit_cache = True
    _traverse_internals = [("active", InternalTraversal.dp_boolean)]

    def __init__(self, active: bool):
        self.active = active


@compiles(_FlagLiteral)
def _compile_numeric_flag(element, compiler, **kwargs):
    return "1" if element.active else "0"


@compiles(_FlagLiteral, "postgresql")
def _compile_postgres_flag(element, compiler, **kwargs):
    # An untyped SQL literal supports both BOOLEAN and legacy INTEGER columns.
    return "'1'" if element.active else "'0'"


ACTIVE_FLAG = _FlagLiteral(True)
INACTIVE_FLAG = _FlagLiteral(False)


def active_flag_text(sql: str, active: bool = True) -> TextClause:
    """Replace :active_flag with a fixed literal accepted by all three dialects."""
    return text(sql.replace(":active_flag", "'1'" if active else "'0'"))


class ActiveFlag(TypeDecorator):
    """Bind Python flags using the column's native storage type."""

    impl = Integer
    cache_ok = True

    def __init__(self, table_name: str):
        super().__init__()
        self.table_name = table_name

    def _uses_boolean(self, dialect):
        return (
            dialect.name == "postgresql"
            and self.table_name not in getattr(dialect, "_integer_active_flags", ())
        )

    def load_dialect_impl(self, dialect):
        return dialect.type_descriptor(Boolean() if self._uses_boolean(dialect) else Integer())

    def process_bind_param(self, value, dialect):
        if value is None or self._uses_boolean(dialect):
            return value
        return int(value)


def detect_postgres_integer_flags(engine):
    """Record legacy column types before the engine compiles its first query."""

    @event.listens_for(engine, "first_connect")
    def load_flag_types(connection, _):
        cursor = connection.cursor()
        try:
            cursor.execute(
                "SELECT target.table_name FROM "
                "(VALUES ('users'), ('products'), ('services'), ('warehouses'), ('coupons')) "
                "AS target(table_name) "
                "JOIN pg_catalog.pg_attribute AS attribute "
                "ON attribute.attrelid = pg_catalog.to_regclass(target.table_name) "
                "WHERE attribute.attname = 'is_active' AND NOT attribute.attisdropped "
                "AND attribute.atttypid IN ('smallint'::regtype, 'integer'::regtype, 'bigint'::regtype)"
            )
            engine.dialect._integer_active_flags = frozenset(row[0] for row in cursor.fetchall())
        finally:
            cursor.close()
            connection.rollback()

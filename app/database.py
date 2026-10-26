from pathlib import Path

from sqlalchemy import event, inspect, text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import declarative_base
from app.config import BASE_DIR, settings


def _database_url() -> str:
    url = str(settings.database_url)
    prefix = "sqlite+aiosqlite:///"
    if url.startswith(prefix):
        path = url[len(prefix):]
        if path and path != ":memory:" and not Path(path).is_absolute():
            url = prefix + (BASE_DIR / path).as_posix()
    return url


engine = create_async_engine(
    _database_url(),
    future=True,
    echo=False,
)


if str(settings.database_url).startswith("sqlite"):
    @event.listens_for(engine.sync_engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection, _connection_record) -> None:
        """WAL + busy_timeout keep concurrent background writes from locking the DB.

        foreign_keys is off by default in SQLite, so ON DELETE CASCADE in the models
        would otherwise never fire at the database level.
        """
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)

Base = declarative_base()


async def get_db() -> AsyncSession:
    async with AsyncSessionLocal() as session:
        yield session


async def init_db() -> None:
    """Create missing tables and bring existing ones up to date."""
    # Imported here so the tables are registered on Base.metadata even when the
    # caller only imports this module (app.models imports Base back).
    from app import models  # noqa: F401

    async with engine.begin() as conn:
        await conn.run_sync(_migrate_device_invites)
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_add_missing_columns)
        await conn.run_sync(_ensure_indexes)


def _migrate_device_invites(conn) -> None:
    """device_invites switched from long link tokens to 6-digit codes.

    The table only holds short-lived one-time codes, so dropping the old shape
    loses nothing — it is recreated by create_all right after.
    """
    inspector = inspect(conn)
    if "device_invites" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("device_invites")}
    if "code" not in columns and "token" in columns:
        conn.execute(text("DROP TABLE device_invites"))


def _add_missing_columns(conn) -> None:
    """`create_all` never touches an existing table, so new columns need an ALTER TABLE.

    Only additive, SQLite-compatible changes are handled here: no drops, no type changes.
    """
    inspector = inspect(conn)
    for table in Base.metadata.sorted_tables:
        existing = {column["name"] for column in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in existing:
                continue
            ddl = f"ALTER TABLE {table.name} ADD COLUMN {column.name} {column.type.compile(conn.dialect)}"
            if column.default is not None and getattr(column.default, "is_scalar", False):
                ddl += f" DEFAULT {_sql_literal(column.default.arg)}"
            conn.execute(text(ddl))


def _ensure_indexes(conn) -> None:
    """`create_all` does not add indexes to tables that already exist."""
    conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_recordings_user_created "
            "ON recordings (user_id, created_at)"
        )
    )


def _sql_literal(value) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"

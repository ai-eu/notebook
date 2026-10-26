import asyncio
import sqlite3

from sqlalchemy import create_engine, inspect, text

from app.database import Base, _add_missing_columns, _ensure_indexes


OLD_RECORDINGS_SCHEMA = """
CREATE TABLE recordings (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL,
    recording_id VARCHAR(64) NOT NULL,
    folder_path VARCHAR(500) NOT NULL,
    original_filename VARCHAR(255),
    duration FLOAT,
    status VARCHAR(20),
    error_message TEXT,
    created_at DATETIME,
    updated_at DATETIME
)
"""


def test_new_columns_are_added_to_an_existing_database(tmp_path):
    db_path = tmp_path / "old.db"
    connection = sqlite3.connect(db_path)
    connection.execute(OLD_RECORDINGS_SCHEMA)
    connection.execute(
        "INSERT INTO recordings (user_id, recording_id, folder_path, status) VALUES (1, 'rec-1', '1/rec-1', 'done')"
    )
    connection.commit()
    connection.close()

    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as conn:
        Base.metadata.create_all(conn)
        _add_missing_columns(conn)
        _ensure_indexes(conn)
        columns = {column["name"] for column in inspect(conn).get_columns("recordings")}
        assert {"storage_state", "archived_at", "tags", "comment"} <= columns
        assert {"attempts", "archive_verified_at"} <= columns
        assert {"stored_files"} <= set(inspect(conn).get_table_names())
        indexes = {index["name"] for index in inspect(conn).get_indexes("recordings")}
        assert "ix_recordings_user_created" in indexes
        # the row that existed before the upgrade keeps its data and gets the default
        assert conn.execute(text("SELECT recording_id, storage_state FROM recordings")).one() == ("rec-1", "local")
    engine.dispose()


def test_sqlite_pragmas_are_enabled():
    from app.database import engine

    async def check():
        async with engine.connect() as conn:
            assert (await conn.execute(text("PRAGMA foreign_keys"))).scalar() == 1
            mode = (await conn.execute(text("PRAGMA journal_mode"))).scalar()
            assert str(mode).lower() == "wal"

    asyncio.run(check())


def test_migration_is_repeatable(tmp_path):
    db_path = tmp_path / "old.db"
    connection = sqlite3.connect(db_path)
    connection.execute(OLD_RECORDINGS_SCHEMA)
    connection.commit()
    connection.close()

    engine = create_engine(f"sqlite:///{db_path}")
    for _ in range(2):
        with engine.begin() as conn:
            Base.metadata.create_all(conn)
            _add_missing_columns(conn)
    engine.dispose()

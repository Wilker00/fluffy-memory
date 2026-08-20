"""Small SQLite helpers shared by durable local domain stores."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


def sqlite_database_path(db_url: str) -> str:
    """Translate the SQLAlchemy-style SQLite URLs used by project settings."""
    prefixes = ("sqlite+aiosqlite:///", "sqlite:///")
    database = next(
        (db_url[len(prefix) :] for prefix in prefixes if db_url.startswith(prefix)),
        None,
    )
    if database is None:
        raise ValueError("Only sqlite and sqlite+aiosqlite database URLs are supported.")
    database = database.split("?", 1)[0]
    if re.match(r"^/[A-Za-z]:[/\\]", database):
        database = database[1:]
    if not database:
        raise ValueError("SQLite database URL must include a file path.")
    return database


def initialize_sqlite_file(db_url: str) -> str:
    """Resolve a SQLite URL and ensure its parent directory exists."""
    database = sqlite_database_path(db_url)
    if database != ":memory:":
        Path(database).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
    return database


@contextmanager
def connect_sqlite(database: str) -> Iterator[sqlite3.Connection]:
    """Open a SQLite connection, commit or roll back, and always close it."""
    connection = sqlite3.connect(database, timeout=30)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

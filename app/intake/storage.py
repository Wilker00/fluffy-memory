"""Persistence implementations for screened intake artifacts."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Protocol

from app.sqlite_utils import connect_sqlite, initialize_sqlite_file


@dataclass(frozen=True)
class StoredIntakeDocument:
    document_id: str
    manifest_json: str
    content: str


@dataclass(frozen=True)
class StoredPreparedApplication:
    tenant_id: str
    package_json: str


class IntakePersistence(Protocol):
    def put_document(
        self,
        *,
        tenant_id: str,
        application_id: str,
        document: StoredIntakeDocument,
    ) -> None: ...

    def list_documents(
        self, *, tenant_id: str, application_id: str
    ) -> list[StoredIntakeDocument]: ...

    def get_prepared(self, application_id: str) -> StoredPreparedApplication | None: ...

    def put_prepared(
        self, *, application_id: str, prepared: StoredPreparedApplication
    ) -> None: ...

    def prepared_ids(self) -> tuple[str, ...]: ...

    def reset(self) -> None: ...


class MemoryIntakePersistence:
    """Process-local implementation retained for isolated tests and demos."""

    def __init__(self) -> None:
        self._documents: dict[tuple[str, str], dict[str, StoredIntakeDocument]] = {}
        self._prepared: dict[str, StoredPreparedApplication] = {}

    def put_document(
        self,
        *,
        tenant_id: str,
        application_id: str,
        document: StoredIntakeDocument,
    ) -> None:
        self._documents.setdefault((tenant_id, application_id), {})[
            document.document_id
        ] = document
        prepared = self._prepared.get(application_id)
        if prepared is not None and prepared.tenant_id == tenant_id:
            self._prepared.pop(application_id, None)

    def list_documents(
        self, *, tenant_id: str, application_id: str
    ) -> list[StoredIntakeDocument]:
        return list(self._documents.get((tenant_id, application_id), {}).values())

    def get_prepared(self, application_id: str) -> StoredPreparedApplication | None:
        return self._prepared.get(application_id)

    def put_prepared(
        self, *, application_id: str, prepared: StoredPreparedApplication
    ) -> None:
        self._prepared[application_id] = prepared

    def prepared_ids(self) -> tuple[str, ...]:
        return tuple(self._prepared)

    def reset(self) -> None:
        self._documents.clear()
        self._prepared.clear()


class SQLiteIntakePersistence:
    """File-backed intake storage with tenant-scoped document access."""

    def __init__(self, db_url: str) -> None:
        self._database = initialize_sqlite_file(db_url)
        self._initialize()

    def _initialize(self) -> None:
        with connect_sqlite(self._database) as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS intake_documents (
                    tenant_id TEXT NOT NULL,
                    application_id TEXT NOT NULL,
                    document_id TEXT NOT NULL,
                    manifest_json TEXT NOT NULL,
                    content TEXT NOT NULL,
                    PRIMARY KEY (tenant_id, application_id, document_id)
                );
                CREATE INDEX IF NOT EXISTS intake_documents_scope
                    ON intake_documents (tenant_id, application_id);
                CREATE TABLE IF NOT EXISTS intake_prepared (
                    application_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    package_json TEXT NOT NULL
                );
                """
            )

    def put_document(
        self,
        *,
        tenant_id: str,
        application_id: str,
        document: StoredIntakeDocument,
    ) -> None:
        with connect_sqlite(self._database) as connection:
            connection.execute(
                """
                INSERT INTO intake_documents
                    (tenant_id, application_id, document_id, manifest_json, content)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (tenant_id, application_id, document_id) DO UPDATE SET
                    manifest_json = excluded.manifest_json,
                    content = excluded.content
                """,
                (
                    tenant_id,
                    application_id,
                    document.document_id,
                    document.manifest_json,
                    document.content,
                ),
            )
            connection.execute(
                "DELETE FROM intake_prepared WHERE application_id = ? AND tenant_id = ?",
                (application_id, tenant_id),
            )

    def list_documents(
        self, *, tenant_id: str, application_id: str
    ) -> list[StoredIntakeDocument]:
        with connect_sqlite(self._database) as connection:
            rows = connection.execute(
                """
                SELECT document_id, manifest_json, content
                FROM intake_documents
                WHERE tenant_id = ? AND application_id = ?
                ORDER BY rowid
                """,
                (tenant_id, application_id),
            ).fetchall()
        return [StoredIntakeDocument(**dict(row)) for row in rows]

    def get_prepared(self, application_id: str) -> StoredPreparedApplication | None:
        with connect_sqlite(self._database) as connection:
            row = connection.execute(
                """
                SELECT tenant_id, package_json
                FROM intake_prepared
                WHERE application_id = ?
                """,
                (application_id,),
            ).fetchone()
        return StoredPreparedApplication(**dict(row)) if row is not None else None

    def put_prepared(
        self, *, application_id: str, prepared: StoredPreparedApplication
    ) -> None:
        try:
            with connect_sqlite(self._database) as connection:
                connection.execute(
                    """
                    INSERT INTO intake_prepared (application_id, tenant_id, package_json)
                    VALUES (?, ?, ?)
                    ON CONFLICT (application_id) DO UPDATE SET
                        tenant_id = excluded.tenant_id,
                        package_json = excluded.package_json
                    WHERE intake_prepared.tenant_id = excluded.tenant_id
                    """,
                    (application_id, prepared.tenant_id, prepared.package_json),
                )
                if connection.execute("SELECT changes()").fetchone()[0] != 1:
                    raise PermissionError(
                        "Application identifier is already bound to another tenant."
                    )
        except sqlite3.IntegrityError as exc:
            raise PermissionError(
                "Application identifier is already bound to another tenant."
            ) from exc

    def prepared_ids(self) -> tuple[str, ...]:
        with connect_sqlite(self._database) as connection:
            rows = connection.execute(
                "SELECT application_id FROM intake_prepared ORDER BY rowid"
            ).fetchall()
        return tuple(row["application_id"] for row in rows)

    def reset(self) -> None:
        with connect_sqlite(self._database) as connection:
            connection.execute("DELETE FROM intake_documents")
            connection.execute("DELETE FROM intake_prepared")


def build_intake_persistence(backend: str, db_url: str) -> IntakePersistence:
    if backend == "memory":
        return MemoryIntakePersistence()
    if backend == "sqlite":
        return SQLiteIntakePersistence(db_url)
    raise ValueError(f"Unsupported intake backend: {backend!r}")

"""Persistence for opportunity-domain actions and recommendation artifacts."""

from __future__ import annotations

import json
from threading import RLock
from typing import Any, Protocol

from app.sqlite_utils import connect_sqlite, initialize_sqlite_file
from app.tools.protocol import ActionResult


class OpportunityActionPersistence(Protocol):
    def get_action(self, idempotency_key: str) -> ActionResult | None: ...

    def put_action(
        self,
        *,
        idempotency_key: str,
        result: ActionResult,
        artifact: dict[str, Any] | None = None,
    ) -> ActionResult: ...

    def get_artifact(self, artifact: str) -> dict[str, Any] | None: ...

    def reset(self) -> None: ...


class MemoryOpportunityActionPersistence:
    def __init__(self) -> None:
        self._actions: dict[str, ActionResult] = {}
        self._artifacts: dict[str, dict[str, Any]] = {}
        self._lock = RLock()

    def get_action(self, idempotency_key: str) -> ActionResult | None:
        return self._actions.get(idempotency_key)

    def put_action(
        self,
        *,
        idempotency_key: str,
        result: ActionResult,
        artifact: dict[str, Any] | None = None,
    ) -> ActionResult:
        with self._lock:
            existing = self._actions.get(idempotency_key)
            if existing is not None:
                return existing
            self._actions[idempotency_key] = result
            if artifact is not None:
                self._artifacts[result.artifact] = artifact
            return result

    def get_artifact(self, artifact: str) -> dict[str, Any] | None:
        return self._artifacts.get(artifact)

    def reset(self) -> None:
        with self._lock:
            self._actions.clear()
            self._artifacts.clear()


class SQLiteOpportunityActionPersistence:
    def __init__(self, db_url: str) -> None:
        self._database = initialize_sqlite_file(db_url)
        with connect_sqlite(self._database) as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS opportunity_actions (
                    idempotency_key TEXT PRIMARY KEY,
                    result_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS opportunity_artifacts (
                    artifact TEXT PRIMARY KEY,
                    artifact_json TEXT NOT NULL
                );
                """
            )

    def get_action(self, idempotency_key: str) -> ActionResult | None:
        with connect_sqlite(self._database) as connection:
            row = connection.execute(
                "SELECT result_json FROM opportunity_actions WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
        return ActionResult.model_validate_json(row["result_json"]) if row else None

    def put_action(
        self,
        *,
        idempotency_key: str,
        result: ActionResult,
        artifact: dict[str, Any] | None = None,
    ) -> ActionResult:
        with connect_sqlite(self._database) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT result_json FROM opportunity_actions WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if row is not None:
                return ActionResult.model_validate_json(row["result_json"])
            connection.execute(
                """
                INSERT INTO opportunity_actions (idempotency_key, result_json)
                VALUES (?, ?)
                """,
                (idempotency_key, result.model_dump_json()),
            )
            if artifact is not None:
                connection.execute(
                    """
                    INSERT INTO opportunity_artifacts (artifact, artifact_json)
                    VALUES (?, ?)
                    """,
                    (result.artifact, json.dumps(artifact, sort_keys=True)),
                )
            return result

    def get_artifact(self, artifact: str) -> dict[str, Any] | None:
        with connect_sqlite(self._database) as connection:
            row = connection.execute(
                "SELECT artifact_json FROM opportunity_artifacts WHERE artifact = ?",
                (artifact,),
            ).fetchone()
        return json.loads(row["artifact_json"]) if row else None

    def reset(self) -> None:
        with connect_sqlite(self._database) as connection:
            connection.execute("DELETE FROM opportunity_actions")
            connection.execute("DELETE FROM opportunity_artifacts")


def build_opportunity_action_persistence(
    backend: str, db_url: str
) -> OpportunityActionPersistence:
    if backend == "memory":
        return MemoryOpportunityActionPersistence()
    if backend == "sqlite":
        return SQLiteOpportunityActionPersistence(db_url)
    raise ValueError(f"Unsupported opportunity backend: {backend!r}")

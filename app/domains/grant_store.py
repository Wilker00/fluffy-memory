"""Persistence for grant-screening actions, scorecards, and retry counters."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from threading import RLock
from typing import Any, Protocol

from app.sqlite_utils import connect_sqlite, initialize_sqlite_file
from app.tools.protocol import ActionResult


@dataclass(frozen=True)
class GrantActionWrite:
    result: ActionResult
    scorecard: dict[str, Any]


class GrantPersistence(Protocol):
    def get_action(self, idempotency_key: str) -> ActionResult | None: ...

    def record_action(
        self,
        *,
        idempotency_key: str,
        item_id: str,
        build: Callable[[int], GrantActionWrite],
    ) -> ActionResult: ...

    def get_scorecard(self, artifact: str) -> dict[str, Any] | None: ...

    def actions_snapshot(self) -> dict[str, ActionResult]: ...

    def scorecards_snapshot(self) -> dict[str, dict[str, Any]]: ...

    def item_counts_snapshot(self) -> dict[str, int]: ...

    def reset(self) -> None: ...


class MemoryGrantPersistence:
    def __init__(self) -> None:
        self.actions: dict[str, ActionResult] = {}
        self.scorecards: dict[str, dict[str, Any]] = {}
        self.item_action_counts: dict[str, int] = {}
        self._lock = RLock()

    def get_action(self, idempotency_key: str) -> ActionResult | None:
        return self.actions.get(idempotency_key)

    def record_action(
        self,
        *,
        idempotency_key: str,
        item_id: str,
        build: Callable[[int], GrantActionWrite],
    ) -> ActionResult:
        with self._lock:
            existing = self.actions.get(idempotency_key)
            if existing is not None:
                return existing
            sequence = self.item_action_counts.get(item_id, 0) + 1
            write = build(sequence)
            self.item_action_counts[item_id] = sequence
            self.scorecards[write.result.artifact] = write.scorecard
            self.actions[idempotency_key] = write.result
            return write.result

    def get_scorecard(self, artifact: str) -> dict[str, Any] | None:
        return self.scorecards.get(artifact)

    def actions_snapshot(self) -> dict[str, ActionResult]:
        return dict(self.actions)

    def scorecards_snapshot(self) -> dict[str, dict[str, Any]]:
        return dict(self.scorecards)

    def item_counts_snapshot(self) -> dict[str, int]:
        return dict(self.item_action_counts)

    def reset(self) -> None:
        with self._lock:
            self.actions.clear()
            self.scorecards.clear()
            self.item_action_counts.clear()


class SQLiteGrantPersistence:
    """Transactional SQLite implementation preserving action idempotency."""

    def __init__(self, db_url: str) -> None:
        self._database = initialize_sqlite_file(db_url)
        self._initialize()

    def _initialize(self) -> None:
        with connect_sqlite(self._database) as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS grant_actions (
                    idempotency_key TEXT PRIMARY KEY,
                    item_id TEXT NOT NULL,
                    result_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS grant_scorecards (
                    artifact TEXT PRIMARY KEY,
                    item_id TEXT NOT NULL,
                    scorecard_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS grant_item_action_counts (
                    item_id TEXT PRIMARY KEY,
                    action_count INTEGER NOT NULL
                );
                """
            )

    def get_action(self, idempotency_key: str) -> ActionResult | None:
        with connect_sqlite(self._database) as connection:
            row = connection.execute(
                "SELECT result_json FROM grant_actions WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
        return ActionResult.model_validate_json(row["result_json"]) if row else None

    def record_action(
        self,
        *,
        idempotency_key: str,
        item_id: str,
        build: Callable[[int], GrantActionWrite],
    ) -> ActionResult:
        with connect_sqlite(self._database) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT result_json FROM grant_actions WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if row is not None:
                return ActionResult.model_validate_json(row["result_json"])

            count_row = connection.execute(
                """
                SELECT action_count FROM grant_item_action_counts
                WHERE item_id = ?
                """,
                (item_id,),
            ).fetchone()
            sequence = (count_row["action_count"] if count_row else 0) + 1
            write = build(sequence)
            connection.execute(
                """
                INSERT INTO grant_item_action_counts (item_id, action_count)
                VALUES (?, ?)
                ON CONFLICT (item_id) DO UPDATE SET action_count = excluded.action_count
                """,
                (item_id, sequence),
            )
            connection.execute(
                """
                INSERT INTO grant_scorecards (artifact, item_id, scorecard_json)
                VALUES (?, ?, ?)
                """,
                (write.result.artifact, item_id, json.dumps(write.scorecard, sort_keys=True)),
            )
            connection.execute(
                """
                INSERT INTO grant_actions (idempotency_key, item_id, result_json)
                VALUES (?, ?, ?)
                """,
                (idempotency_key, item_id, write.result.model_dump_json()),
            )
            return write.result

    def get_scorecard(self, artifact: str) -> dict[str, Any] | None:
        with connect_sqlite(self._database) as connection:
            row = connection.execute(
                "SELECT scorecard_json FROM grant_scorecards WHERE artifact = ?",
                (artifact,),
            ).fetchone()
        return json.loads(row["scorecard_json"]) if row else None

    def actions_snapshot(self) -> dict[str, ActionResult]:
        with connect_sqlite(self._database) as connection:
            rows = connection.execute(
                "SELECT idempotency_key, result_json FROM grant_actions"
            ).fetchall()
        return {
            row["idempotency_key"]: ActionResult.model_validate_json(row["result_json"])
            for row in rows
        }

    def scorecards_snapshot(self) -> dict[str, dict[str, Any]]:
        with connect_sqlite(self._database) as connection:
            rows = connection.execute(
                "SELECT artifact, scorecard_json FROM grant_scorecards"
            ).fetchall()
        return {row["artifact"]: json.loads(row["scorecard_json"]) for row in rows}

    def item_counts_snapshot(self) -> dict[str, int]:
        with connect_sqlite(self._database) as connection:
            rows = connection.execute(
                "SELECT item_id, action_count FROM grant_item_action_counts"
            ).fetchall()
        return {row["item_id"]: row["action_count"] for row in rows}

    def reset(self) -> None:
        with connect_sqlite(self._database) as connection:
            connection.execute("DELETE FROM grant_actions")
            connection.execute("DELETE FROM grant_scorecards")
            connection.execute("DELETE FROM grant_item_action_counts")


def build_grant_persistence(backend: str, db_url: str) -> GrantPersistence:
    if backend == "memory":
        return MemoryGrantPersistence()
    if backend == "sqlite":
        return SQLiteGrantPersistence(db_url)
    raise ValueError(f"Unsupported scorecard backend: {backend!r}")

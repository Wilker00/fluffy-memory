"""Tier 3 backend selection.

ARMCL talks to ADK's `BaseMemoryService` interface rather than to Memory Bank
directly, so the durable tier can be swapped without touching agent code.

  memory_bank  Vertex AI Memory Bank. The default and the one that matters for
               the Fortified Enterprise Fleet track: it is managed, it scopes
               memories by (app_name, user_id), and Agent Runtime provisions it
               automatically at deploy time.
  chroma       Embedded local vector store. Offline fallback for when Memory
               Bank is unavailable, and for iterating without cloud round trips.
  memory       In-process dict. Tests only.

`AdkApp` already defaults to `VertexAiMemoryBankService` once deployed to Agent
Runtime, so `build_memory_service` mainly governs local runs and the explicit
fallback path.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from google.adk.memory import BaseMemoryService, InMemoryMemoryService
from google.adk.memory.base_memory_service import SearchMemoryResponse
from google.adk.memory.memory_entry import MemoryEntry
from google.genai import types

from app.settings import settings

if TYPE_CHECKING:
    from google.adk.sessions import Session

logger = logging.getLogger(__name__)


def build_memory_service() -> BaseMemoryService:
    """Construct the Tier 3 service named by ARMCL_MEMORY_BACKEND."""
    backend = settings.memory_backend

    if backend == "memory_bank":
        return _build_memory_bank()
    if backend == "chroma":
        return ChromaMemoryService(persist_dir=settings.chroma_dir)
    if backend == "memory":
        return InMemoryMemoryService()

    raise ValueError(
        f"Unknown ARMCL_MEMORY_BACKEND={backend!r}. Expected memory_bank, chroma, or memory."
    )


def _build_memory_bank() -> BaseMemoryService:
    from google.adk.memory import VertexAiMemoryBankService

    settings.require_cloud()
    if not settings.memory_bank_id:
        raise RuntimeError(
            "ARMCL_MEMORY_BACKEND=memory_bank requires ARMCL_MEMORY_BANK_ID.\n"
            "Run `make deploy` (which provisions one and writes it back), or set "
            "ARMCL_MEMORY_BACKEND=chroma to work offline."
        )
    return VertexAiMemoryBankService(
        project=settings.project,
        location=settings.location,
        agent_engine_id=settings.memory_bank_id,
    )


def memory_service_builder() -> BaseMemoryService:
    """Callable handed to `AdkApp(memory_service_builder=...)`."""
    return build_memory_service()


def _entry_text(entry: MemoryEntry) -> str:
    """Best-effort text extraction from a MemoryEntry's Content."""
    content = entry.content
    if content is None or not content.parts:
        return ""
    return " ".join(part.text or "" for part in content.parts).strip()


def make_entry(text: str, *, author: str = "armcl", **metadata: object) -> MemoryEntry:
    """Build a MemoryEntry from plain text plus scope metadata."""
    return MemoryEntry(
        content=types.Content(role="model", parts=[types.Part(text=text)]),
        author=author,
        custom_metadata=dict(metadata),
    )


class ChromaMemoryService(BaseMemoryService):
    """Offline Tier 3 backed by embedded Chroma.

    Honours the same (app_name, user_id) scoping Memory Bank enforces, so the
    isolation guarantees ARMCL relies on hold identically on both backends and
    the fallback cannot quietly widen access.
    """

    def __init__(self, persist_dir: str) -> None:
        try:
            import chromadb
        except ImportError as exc:  # pragma: no cover - depends on extras
            raise RuntimeError(
                'ARMCL_MEMORY_BACKEND=chroma requires the local extra: uv pip install -e ".[local]"'
            ) from exc

        self._client = chromadb.PersistentClient(path=persist_dir)
        self._collection = self._client.get_or_create_collection(name="armcl_tier3")
        logger.info("ARMCL Tier 3 using embedded Chroma at %s", persist_dir)

    @staticmethod
    def _scope(app_name: str, user_id: str) -> dict[str, str]:
        """Scope metadata stamped onto every stored memory."""
        return {"app_name": app_name, "user_id": user_id}

    @staticmethod
    def _scope_filter(app_name: str, user_id: str) -> dict[str, Any]:
        """The same scope as a Chroma `where` clause.

        Chroma rejects a bare multi-key mapping ("expected exactly one
        operator"), so the conjunction has to be explicit. Getting this wrong
        raises inside the search, and the Tier 3 caller treats a failed lookup
        as an empty one, so the isolation bug would present as silently absent
        memory rather than as an error.
        """
        return {"$and": [{"app_name": app_name}, {"user_id": user_id}]}

    async def add_memory(
        self,
        *,
        app_name: str,
        user_id: str,
        memories: Sequence[MemoryEntry],
        custom_metadata: Mapping[str, object] | None = None,
    ) -> None:
        docs, metas, ids = [], [], []
        base = self._scope(app_name, user_id)
        for i, entry in enumerate(memories):
            text = _entry_text(entry)
            if not text:
                continue
            meta = {**base, **(entry.custom_metadata or {}), **(custom_metadata or {})}
            docs.append(text)
            metas.append({k: str(v) for k, v in meta.items()})
            ids.append(entry.id or f"{app_name}:{user_id}:{abs(hash(text))}:{i}")

        if docs:
            self._collection.upsert(documents=docs, metadatas=metas, ids=ids)

    async def add_events_to_memory(
        self,
        *,
        app_name: str,
        user_id: str,
        events: Sequence[object],
        session_id: str | None = None,
        custom_metadata: Mapping[str, object] | None = None,
    ) -> None:
        entries: list[MemoryEntry] = []
        for event in events:
            content = getattr(event, "content", None)
            if content is None or not getattr(content, "parts", None):
                continue
            text = " ".join(p.text or "" for p in content.parts).strip()
            if text:
                entries.append(
                    make_entry(
                        text,
                        author=getattr(event, "author", "unknown"),
                        session_id=session_id or "",
                    )
                )
        await self.add_memory(
            app_name=app_name,
            user_id=user_id,
            memories=entries,
            custom_metadata=custom_metadata,
        )

    async def add_session_to_memory(self, session: Session) -> None:
        await self.add_events_to_memory(
            app_name=session.app_name,
            user_id=session.user_id,
            events=session.events,
            session_id=session.id,
        )

    async def search_memory(
        self, *, app_name: str, user_id: str, query: str
    ) -> SearchMemoryResponse:
        if self._collection.count() == 0:
            return SearchMemoryResponse(memories=[])

        result = self._collection.query(
            query_texts=[query],
            n_results=min(5, self._collection.count()),
            where=self._scope_filter(app_name, user_id),
        )

        documents = (result.get("documents") or [[]])[0]
        metadatas = (result.get("metadatas") or [[]])[0]
        memories = [
            make_entry(doc, **{k: v for k, v in (meta or {}).items() if k not in ("app_name",)})
            for doc, meta in zip(documents, metadatas or [{}] * len(documents), strict=False)
        ]
        return SearchMemoryResponse(memories=memories)

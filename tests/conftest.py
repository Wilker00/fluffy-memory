"""Shared test fixtures."""

from __future__ import annotations

from typing import Any

import pytest

from app.armcl.memory_backend import make_entry


class FakeContext:
    """Stand-in for ADK's Context.

    Implements only what ARMCL touches: state, the memory methods, and the
    route setter. Using a fake keeps the tier logic testable without a live
    model or a session service.
    """

    def __init__(
        self,
        memories: list[str] | None = None,
        state: dict[str, Any] | None = None,
    ) -> None:
        # `state` seeds a context as if it had been rehydrated from a persisted
        # session, which is how the workflow resumes after a suspension.
        self.state: dict[str, Any] = dict(state or {})
        self.route: Any = None
        self.written_memories: list[str] = []
        self.written_events: list[Any] = []
        self._seeded = list(memories or [])
        self.search_calls: list[str] = []
        self.fail_memory = False

    async def add_memory(self, *, memories, custom_metadata=None) -> None:
        if self.fail_memory:
            raise RuntimeError("memory backend unavailable")
        for entry in memories:
            text = " ".join(p.text or "" for p in entry.content.parts)
            self.written_memories.append(text)

    async def add_events_to_memory(self, *, events, custom_metadata=None) -> None:
        if self.fail_memory:
            raise RuntimeError("memory backend unavailable")
        self.written_events.extend(events)

    async def search_memory(self, query: str):
        self.search_calls.append(query)
        if self.fail_memory:
            raise RuntimeError("memory backend unavailable")

        class _Response:
            memories = [make_entry(text) for text in self._seeded]

        return _Response()


@pytest.fixture
def ctx() -> FakeContext:
    return FakeContext()


@pytest.fixture(autouse=True)
def _reset_playbook():
    """Playbook generations must not leak across tests."""
    from app.evolve.playbook import reset_store

    reset_store()
    yield
    reset_store()


@pytest.fixture
def ctx_with_memory() -> FakeContext:
    return FakeContext(
        memories=[
            "Policy 14: UNIT-7 must never be serviced without a signed failover plan.",
            "Servicing UNIT-7 during peak hours is prohibited.",
        ]
    )


@pytest.fixture
def domain():
    from app.reference import DOMAIN

    DOMAIN.reset()
    return DOMAIN


@pytest.fixture
def regex_backend(monkeypatch):
    """Force the offline heuristic guardrail so tests need no cloud access.

    `Settings` is a frozen dataclass and consumers bind `settings` at import
    time, so the override has to replace the name inside the module that reads
    it rather than mutate the shared instance.
    """
    import dataclasses

    from app.settings import settings

    patched = dataclasses.replace(settings, guardrail_backend="regex")
    monkeypatch.setattr("app.guardrails.armor.settings", patched)
    return patched


@pytest.fixture
def model_armor_backend(monkeypatch):
    """Select the Model Armor code path so failure directions can be tested."""
    import dataclasses

    from app.settings import settings

    patched = dataclasses.replace(settings, guardrail_backend="model_armor")
    monkeypatch.setattr("app.guardrails.armor.settings", patched)
    return patched

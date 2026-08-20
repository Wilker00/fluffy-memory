"""Tenant isolation and data sovereignty on the durable tier.

This is the load-bearing compliance claim of the Fortified Enterprise Fleet
track, so it is asserted against a real backend rather than a fake. The Chroma
fallback must enforce the same (app_name, user_id) scope Memory Bank does; a
fallback that quietly widens access is worse than no fallback, because the
isolation story stays in the README while the guarantee is gone.

These also guard a specific failure mode: a malformed scope filter raises
inside the search, and the Tier 3 caller treats a failed lookup as an empty
one. Isolation bugs therefore present as silently missing memory, not as
errors, which is exactly the shape of bug tests exist to catch.
"""

from __future__ import annotations

import shutil

import pytest

from app.armcl.memory_backend import ChromaMemoryService, make_entry
from app.armcl.policy import redact

chromadb = pytest.importorskip("chromadb", reason="requires the [local] extra")

APP = "fluffy_memory"
OTHER_APP = "other_app"


@pytest.fixture
def store(tmp_path):
    path = tmp_path / "chroma"
    service = ChromaMemoryService(persist_dir=str(path))
    yield service
    # Chroma keeps file handles open on Windows; cleanup is best effort.
    del service
    shutil.rmtree(path, ignore_errors=True)


async def _seed(store, app: str, user: str, text: str) -> None:
    await store.add_memory(app_name=app, user_id=user, memories=[make_entry(text)])


async def _search(store, app: str, user: str, query: str = "secret") -> list[str]:
    response = await store.search_memory(app_name=app, user_id=user, query=query)
    return [" ".join(p.text or "" for p in m.content.parts) for m in response.memories]


class TestTenantIsolation:
    async def test_a_tenant_cannot_read_another_tenants_memory(self, store):
        await _seed(store, APP, "tenant-a", "secret: Northwind acquisition closes Friday")
        await _seed(store, APP, "tenant-b", "secret: layoffs planned in Q4")

        a = await _search(store, APP, "tenant-a")
        b = await _search(store, APP, "tenant-b")

        assert any("Northwind" in t for t in a)
        assert not any("layoffs" in t for t in a), "cross-tenant leak"
        assert any("layoffs" in t for t in b)
        assert not any("Northwind" in t for t in b), "cross-tenant leak"

    async def test_the_same_user_is_isolated_across_applications(self, store):
        """Scope is (app_name, user_id). One identity in two apps is two scopes."""
        await _seed(store, APP, "tenant-a", "secret: fleet policy 14")
        await _seed(store, OTHER_APP, "tenant-a", "secret: unrelated application data")

        fleet = await _search(store, APP, "tenant-a")
        other = await _search(store, OTHER_APP, "tenant-a")

        assert any("policy 14" in t for t in fleet)
        assert not any("unrelated" in t for t in fleet)
        assert any("unrelated" in t for t in other)

    async def test_an_unknown_tenant_reads_nothing(self, store):
        await _seed(store, APP, "tenant-a", "secret: Northwind acquisition")
        assert await _search(store, APP, "tenant-never-seen") == []

    async def test_a_scoped_search_does_not_raise(self, store):
        """A raising filter would be swallowed upstream and look like empty
        memory, so the call itself has to be proven to work."""
        await _seed(store, APP, "tenant-a", "secret: anything")
        response = await store.search_memory(app_name=APP, user_id="tenant-a", query="secret")
        assert response.memories


class TestRedactionOnTheWritePath:
    """Redaction runs before persistence, not on retrieval. Tier 3 outlives the
    session, so a credential written once stays retrievable forever."""

    @pytest.mark.parametrize(
        "raw,forbidden",
        [
            ("token ghp_" + "a" * 36, "ghp_"),
            ("key AIza" + "b" * 35, "AIza"),
            ("contact alice@example.com", "alice@example.com"),
            ("ssn 123-45-6789", "123-45-6789"),
            ("card 4111 1111 1111 1111", "4111"),
            ("-----BEGIN RSA PRIVATE KEY-----", "BEGIN RSA PRIVATE KEY"),
        ],
    )
    async def test_credentials_never_reach_the_store(self, store, raw, forbidden):
        await _seed(store, APP, "tenant-a", redact(raw))
        found = await _search(store, APP, "tenant-a", query="token key contact")
        assert not any(forbidden in t for t in found), f"{forbidden} persisted into Tier 3"

    async def test_redaction_preserves_the_surrounding_fact(self):
        cleaned = redact("Deploy blocked: token ghp_" + "a" * 36 + " lacks scope")
        assert "Deploy blocked" in cleaned
        assert "lacks scope" in cleaned
        assert "ghp_" not in cleaned

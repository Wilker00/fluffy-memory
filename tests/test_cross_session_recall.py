"""The headline claim, tested.

ARMCL's central promise is that a constraint learned in one session changes the
outcome of a later, otherwise identical session, with no human re-supplying it.
This test drives the tiers directly across two simulated sessions so the claim
is verified without depending on model behaviour.
"""

from __future__ import annotations

from app.armcl.hydrate import hydrate
from app.armcl.reconcile import reconcile
from app.armcl.tiers import Tier1

from .conftest import FakeContext


class SharedMemory:
    """A Tier 3 store that outlives an individual session, as Memory Bank does."""

    def __init__(self) -> None:
        self.facts: list[str] = []

    def session(self) -> FakeContext:
        """A fresh session: empty Tier 1 and Tier 2, shared Tier 3."""
        ctx = FakeContext(memories=self.facts)

        original_add = ctx.add_memory

        async def capture(*, memories, custom_metadata=None):
            await original_add(memories=memories, custom_metadata=custom_metadata)
            for entry in memories:
                self.facts.append(" ".join(p.text or "" for p in entry.content.parts))

        ctx.add_memory = capture
        return ctx


class TestCrossSessionRecall:
    async def test_a_constraint_learned_in_run_one_is_available_in_run_two(self):
        store = SharedMemory()

        # Run 1: the fleet discovers a constraint it could not have known.
        first = store.session()
        await reconcile(
            first,
            step="inspect_item",
            raw_output={
                "item_id": "UNIT-7",
                "policy_constraint": "UNIT-7 requires a signed failover plan",
            },
        )
        assert any("failover plan" in f for f in store.facts)

        # Run 2: a cold session. Nothing carries over except Tier 3.
        second = store.session()
        assert Tier1(second).snapshot() == {}, "Tier 1 must start empty"

        frame = await hydrate(second, intent="constraints that apply to UNIT-7")
        assert any("failover plan" in f for f in frame.retrieved_facts)
        assert "tier3" in frame.hydrated_from

    async def test_the_recalled_constraint_reaches_the_agent_instruction(self):
        """Retrieval only matters if the fact lands in the prompt."""
        store = SharedMemory()

        first = store.session()
        await reconcile(
            first,
            step="critic",
            raw_output={"policy_learned": "Never service UNIT-7 during peak hours"},
        )

        second = store.session()
        frame = await hydrate(second, intent="constraints for UNIT-7")
        rendered = frame.render()
        assert "peak hours" in rendered
        assert "Durable organizational memory" in rendered

    async def test_episodic_detail_does_not_leak_between_sessions(self):
        """Only durable facts cross the boundary; per-task state must not."""
        store = SharedMemory()

        first = store.session()
        await reconcile(
            first,
            step="discover",
            raw_output={"item_id": "UNIT-3", "scan_sequence": 41},
        )

        second = store.session()
        assert Tier1(second).get("scan_sequence") is None
        assert not any("scan_sequence" in f for f in store.facts)

    async def test_a_cold_session_recovers_nothing_it_was_never_told(self):
        """Guards against a fake that would make the recall test pass trivially."""
        store = SharedMemory()
        cold = store.session()
        frame = await hydrate(cold, intent="constraints for UNIT-7")
        assert frame.retrieved_facts == []


class TestDependencyGapWithinASession:
    async def test_an_identifier_survives_intervening_noisy_steps(self):
        """The identifier is produced early, then buried under bulk."""
        ctx = FakeContext()

        await reconcile(ctx, step="discover", raw_output={"primary_item_id": "UNIT-9"})
        for i in range(3):
            await reconcile(
                ctx,
                step=f"noise_{i}",
                raw_output={"telemetry": "\n".join(f"line {j}" for j in range(2000))},
            )

        frame = await hydrate(
            ctx, intent="identifier under assessment", required=["primary_item_id"]
        )
        assert frame.gaps == [], "identifier should still be resolvable"
        assert Tier1(ctx).get("primary_item_id") == "UNIT-9"

    async def test_bulk_never_accumulates_in_the_frame(self):
        ctx = FakeContext()
        for i in range(5):
            await reconcile(
                ctx,
                step=f"noisy_{i}",
                raw_output={"logs": "x" * 20_000},
            )
        frame = await hydrate(ctx, intent="anything")
        assert len(frame.render()) < 2000

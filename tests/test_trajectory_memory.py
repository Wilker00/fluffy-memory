"""Outcome history that survives the session that produced it.

Tier 2 has always known which attempts the critic refused, but that knowledge
died with the task, so every run rediscovered its own failures. These tests
drive the tiers directly across simulated sessions, so the claim is verified
without depending on model behaviour.
"""

from __future__ import annotations

from app.agent import circuit_broken, complete, log_decline, route_assessment, route_judgement
from app.armcl.hydrate import hydrate
from app.armcl.reconcile import reconcile
from app.armcl.tiers import Tier1, Tier2
from app.armcl.trajectory import (
    TRAJECTORY_MARKER,
    TrajectoryRecord,
    is_trajectory,
    partition,
    persist_trajectory,
    summarize_trajectory,
)

from .conftest import FakeContext
from .test_cross_session_recall import SharedMemory


async def _attempt(ctx, artifact: str, verdict: str = "REJECT"):
    """One execute-then-judge cycle."""
    await reconcile(ctx, step="executor", raw_output={"artifact": artifact})
    return await route_judgement(ctx, {"verdict": verdict, "reasons": ["not good enough"]})


async def _run_to_halt(ctx, item: str = "UNIT-7"):
    """Drive a run into the circuit breaker, as a non-converging fleet would."""
    await route_assessment(ctx, {"decision": "ACT", "item_id": item, "plan": "swap the seal"})
    for artifact in ("PATCH-A", "PATCH-B", "PATCH-C"):
        await _attempt(ctx, artifact)
    await circuit_broken(ctx, {"reasons": ["verification kept failing"]})
    return ctx


class TestRecordShape:
    def test_the_record_names_the_item_and_the_outcome(self):
        record = TrajectoryRecord(item_id="UNIT-7", outcome="HALTED")
        rendered = record.render()
        assert rendered.startswith(TRAJECTORY_MARKER)
        assert "UNIT-7" in rendered
        assert "HALTED" in rendered

    def test_refused_approaches_are_carried(self):
        record = TrajectoryRecord(
            item_id="UNIT-7",
            outcome="HALTED",
            attempts_refused=2,
            failed_approaches=["PATCH-A", "PATCH-B"],
        )
        rendered = record.render()
        assert "PATCH-A" in rendered and "PATCH-B" in rendered
        assert "2 attempt(s) refused" in rendered

    def test_a_retry_cycle_is_folded(self):
        """A cycle repeats a step; its shape is the signal, not its length."""
        record = TrajectoryRecord(
            item_id="UNIT-7",
            outcome="HALTED",
            steps=["scout", "executor", "executor", "executor", "circuit_broken"],
        )
        assert "scout > executor x3 > circuit_broken" in record.render()

    def test_distinct_steps_are_left_alone(self):
        record = TrajectoryRecord(item_id="UNIT-7", outcome="COMPLETED", steps=["scout", "critic"])
        assert "scout > critic" in record.render()

    def test_the_record_stays_on_one_line(self):
        """The context frame renders each fact as a capped bullet."""
        record = TrajectoryRecord(
            item_id="UNIT-7",
            outcome="HALTED",
            steps=[f"step_{i}" for i in range(12)],
            attempts_refused=3,
            failed_approaches=["x" * 500, "y" * 500],
        )
        assert "\n" not in record.render()

    def test_one_long_approach_cannot_dominate(self):
        record = TrajectoryRecord(
            item_id="UNIT-7",
            outcome="HALTED",
            failed_approaches=["x" * 5000, "SHORT-ONE"],
        )
        rendered = record.render()
        assert "SHORT-ONE" in rendered, "a capped field must not crowd out the others"
        assert len(rendered) < 500


class TestPartitioning:
    def test_a_trajectory_is_distinguished_from_a_constraint(self):
        assert is_trajectory(f"{TRAJECTORY_MARKER} UNIT-7: HALTED.")
        assert not is_trajectory("Policy 14: UNIT-7 requires a signed failover plan.")

    def test_partition_splits_and_strips_the_marker(self):
        constraints, outcomes = partition(
            [
                "Policy 14: UNIT-7 requires a signed failover plan.",
                f"{TRAJECTORY_MARKER} UNIT-7: HALTED; 3 attempt(s) refused.",
            ]
        )
        assert constraints == ["Policy 14: UNIT-7 requires a signed failover plan."]
        assert len(outcomes) == 1
        assert TRAJECTORY_MARKER not in outcomes[0]
        assert outcomes[0].startswith("UNIT-7: HALTED")

    def test_partitioning_nothing_yields_nothing(self):
        assert partition([]) == ([], [])


class TestSummarising:
    async def test_refusals_are_counted_from_the_ledger_not_the_breaker(self, ctx):
        """The breaker is reset on ACCEPT; the count of refused attempts is not.

        A run that succeeded on its third try is exactly the run whose history
        is worth keeping, and reading the breaker would record it as clean.
        """
        await route_assessment(ctx, {"decision": "ACT", "item_id": "UNIT-7", "plan": "swap"})
        await _attempt(ctx, "PATCH-A")
        await _attempt(ctx, "PATCH-B")
        await _attempt(ctx, "PATCH-C", verdict="ACCEPT")

        assert Tier2(ctx).rejection_count == 0
        record = summarize_trajectory(ctx, outcome="COMPLETED")
        assert record.attempts_refused == 2

    async def test_the_step_path_is_recorded(self, ctx):
        await reconcile(ctx, step="scout", raw_output={"primary_item_id": "UNIT-7"})
        await reconcile(ctx, step="analyst", raw_output={"decision": "ACT"})
        record = summarize_trajectory(ctx, outcome="COMPLETED")
        assert record.steps == ["scout", "analyst"]

    async def test_only_the_most_recent_approaches_are_kept(self, ctx):
        """Tier 3 is not an archive. Old refusals are the least informative."""
        t2 = Tier2(ctx)
        Tier1(ctx).set("primary_item_id", "UNIT-7")
        for i in range(10):
            t2.record_rejected_artifact(f"PATCH-{i}")

        record = summarize_trajectory(ctx, outcome="HALTED")
        assert record.failed_approaches == ["PATCH-7", "PATCH-8", "PATCH-9"]


class TestPersistence:
    async def test_a_halted_run_is_written_to_tier_3(self, ctx):
        await _run_to_halt(ctx)
        written = [m for m in ctx.written_memories if TRAJECTORY_MARKER in m]
        assert len(written) == 1
        assert "UNIT-7" in written[0]
        assert "HALTED" in written[0]
        assert "PATCH-C" in written[0]

    async def test_a_decline_is_written_to_tier_3(self, ctx):
        Tier1(ctx).set("primary_item_id", "UNIT-7")
        await log_decline(ctx, {"item_id": "UNIT-7", "rationale": "Policy 14 forbids it"})
        assert any("DECLINED" in m for m in ctx.written_memories if TRAJECTORY_MARKER in m)

    async def test_a_completed_run_is_written_to_tier_3(self, ctx):
        await route_assessment(ctx, {"decision": "ACT", "item_id": "UNIT-7", "plan": "swap"})
        await _attempt(ctx, "PATCH-GOOD", verdict="ACCEPT")
        await complete(ctx, {"verdict": "ACCEPT"})
        assert any("COMPLETED" in m for m in ctx.written_memories if TRAJECTORY_MARKER in m)

    async def test_an_outcome_with_no_subject_is_not_persisted(self, ctx):
        """Quarantine fires before anything has been identified.

        A record naming no item matches every future query and informs none of
        them, so admission control drops it rather than diluting Tier 3.
        """
        assert await persist_trajectory(ctx, outcome="QUARANTINED") is None
        assert ctx.written_memories == []

    async def test_a_tier_3_outage_does_not_break_the_terminal_node(self, ctx):
        Tier1(ctx).set("primary_item_id", "UNIT-7")
        ctx.fail_memory = True
        assert await persist_trajectory(ctx, outcome="HALTED") is None

    async def test_credentials_never_reach_the_record(self, ctx):
        t2 = Tier2(ctx)
        Tier1(ctx).set("primary_item_id", "UNIT-7")
        t2.record_rejected_artifact("deploy with key sk-abcdefghijklmnopqrstuvwxyz012345")

        await persist_trajectory(ctx, outcome="HALTED")
        written = "".join(ctx.written_memories)
        assert "sk-abcdefghijklmnopqrstuvwxyz012345" not in written
        assert "[REDACTED_API_KEY]" in written


class TestCrossSessionOutcomeRecall:
    async def test_a_later_run_sees_how_the_previous_one_ended(self):
        store = SharedMemory()

        first = store.session()
        await _run_to_halt(first)

        second = store.session()
        assert Tier1(second).snapshot() == {}, "Tier 2 must not carry over"

        frame = await hydrate(second, intent="prior outcomes for UNIT-7")
        assert any("HALTED" in outcome for outcome in frame.prior_outcomes)
        assert any("PATCH-C" in outcome for outcome in frame.prior_outcomes)

    async def test_the_prior_outcome_reaches_the_agent_instruction(self):
        store = SharedMemory()
        await _run_to_halt(store.session())

        rendered = (await hydrate(store.session(), intent="UNIT-7")).render()
        assert "How previous runs on this item ended" in rendered
        assert "HALTED" in rendered

    async def test_outcomes_are_kept_apart_from_constraints(self):
        """A constraint binds; an outcome is evidence. One heading conflates them."""
        ctx = FakeContext(
            memories=[
                "Policy 14: UNIT-7 requires a signed failover plan.",
                f"{TRAJECTORY_MARKER} UNIT-7: HALTED; 3 attempt(s) refused.",
            ]
        )

        frame = await hydrate(ctx, intent="UNIT-7")
        assert frame.retrieved_facts == ["Policy 14: UNIT-7 requires a signed failover plan."]
        assert len(frame.prior_outcomes) == 1

        rendered = frame.render()
        assert rendered.index("Durable organizational memory") < rendered.index(
            "How previous runs on this item ended"
        )

    async def test_a_cold_run_invents_no_history(self):
        """Guards against a fake that would make the recall tests pass trivially."""
        frame = await hydrate(SharedMemory().session(), intent="UNIT-7")
        assert frame.prior_outcomes == []
        assert frame.is_empty

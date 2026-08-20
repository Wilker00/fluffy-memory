"""Tests for the three ARMCL tiers and the distillation policy."""

from __future__ import annotations

import pytest

from app.armcl.hydrate import hydrate
from app.armcl.policy import (
    BULK_VALUE_THRESHOLD,
    Salience,
    StateDelta,
    classify,
    distill,
    redact,
    should_persist,
)
from app.armcl.reconcile import reconcile
from app.armcl.tiers import LedgerEntry, Tier1, Tier2


class TestTier1:
    def test_namespaced_so_it_cannot_collide_with_agent_output(self, ctx):
        t1 = Tier1(ctx)
        t1.set("item_id", "UNIT-7")
        assert ctx.state["armcl:t1:item_id"] == "UNIT-7"
        assert "item_id" not in ctx.state

    def test_gap_analysis_reports_only_missing_keys(self, ctx):
        t1 = Tier1(ctx)
        t1.set("present", 1)
        assert t1.missing(["present", "absent"]) == ["absent"]

    def test_snapshot_strips_the_namespace(self, ctx):
        t1 = Tier1(ctx)
        t1.merge({"a": 1, "b": 2})
        assert t1.snapshot() == {"a": 1, "b": 2}

    def test_evict_removes_keys(self, ctx):
        t1 = Tier1(ctx)
        t1.merge({"a": 1, "b": 2})
        t1.evict(["a"])
        assert t1.snapshot() == {"b": 2}


class TestDottedKeyResolution:
    """Distillation flattens nested payloads, so the name an agent asks for is
    rarely the name the fact is stored under. Gap analysis has to see through
    that or it reports a gap for a value already in the frame."""

    def test_a_nested_fact_resolves_by_its_leaf_name(self, ctx):
        t1 = Tier1(ctx)
        t1.set("facts.risk_level", "high")
        assert t1.has("risk_level")
        assert t1.get("risk_level") == "high"
        assert t1.missing(["risk_level"]) == []

    def test_an_exact_match_beats_a_nested_one(self, ctx):
        t1 = Tier1(ctx)
        t1.set("facts.item_id", "UNIT-3")
        t1.set("item_id", "UNIT-7")
        assert t1.get("item_id") == "UNIT-7"

    def test_an_ambiguous_leaf_reports_a_gap_rather_than_guessing(self, ctx):
        """Three discovered candidates each carry an item_id. Picking one would
        hand the agent the wrong entity with full confidence."""
        t1 = Tier1(ctx)
        t1.merge({"[0].item_id": "UNIT-3", "[1].item_id": "UNIT-7"})
        assert not t1.has("item_id")
        assert t1.get("item_id") is None
        assert t1.missing(["item_id"]) == ["item_id"]

    def test_the_ledger_resolves_leaf_names_too(self, ctx):
        t2 = Tier2(ctx)
        t2.append(
            LedgerEntry(
                step="inspect",
                keys_written=["facts.risk_level"],
                bytes_pruned=0,
                summary="facts.risk_level=high",
            )
        )
        assert t2.find_value("risk_level") is not None
        assert t2.find_value("unrelated_key") is None


class TestTier2:
    def test_ledger_appends_in_order(self, ctx):
        t2 = Tier2(ctx)
        for name in ("one", "two", "three"):
            t2.append(LedgerEntry(step=name, keys_written=[], bytes_pruned=0, summary=""))
        assert [e.step for e in t2.all_entries()] == ["one", "two", "three"]

    def test_recent_returns_the_tail(self, ctx):
        t2 = Tier2(ctx)
        for i in range(5):
            t2.append(LedgerEntry(step=f"s{i}", keys_written=[], bytes_pruned=0, summary=""))
        assert [e.step for e in t2.recent(2)] == ["s3", "s4"]

    def test_find_value_closes_the_dependency_gap(self, ctx):
        """The core ARMCL promise: a key written earlier is recoverable later."""
        t2 = Tier2(ctx)
        t2.append(
            LedgerEntry(
                step="discover",
                keys_written=["primary_item_id"],
                bytes_pruned=0,
                summary="primary_item_id=UNIT-7",
            )
        )
        t2.append(LedgerEntry(step="unrelated", keys_written=["other"], bytes_pruned=0, summary=""))
        assert "UNIT-7" in t2.find_value("primary_item_id")
        assert t2.find_value("never_written") is None

    def test_circuit_breaker_counts_and_resets(self, ctx):
        t2 = Tier2(ctx)
        assert t2.record_rejection() == 1
        assert t2.record_rejection() == 2
        t2.reset_rejections()
        assert t2.rejection_count == 0


class TestPolicy:
    @pytest.mark.parametrize(
        "key,value,expected",
        [
            ("policy_limit", "no peak hours", Salience.DURABLE),
            ("approval_required", True, Salience.DURABLE),
            ("eligibility_note", "must be certified", Salience.DURABLE),
            ("item_id", "UNIT-7", Salience.EPISODIC),
            ("commit", "9a4f1c", Salience.EPISODIC),
            ("risk_level", "high", Salience.EPISODIC),
        ],
    )
    def test_classification(self, key, value, expected):
        assert classify(key, value) is expected

    def test_bulk_is_ephemeral_regardless_of_key_name(self):
        """Size wins over key hints.

        A key that looks durable but holds a huge payload must not reach
        Tier 3, or every future retrieval drags the payload back in.
        """
        big = "x" * (BULK_VALUE_THRESHOLD + 1)
        assert classify("policy_document", big) is Salience.EPHEMERAL

    def test_only_durable_facts_persist(self):
        durable = StateDelta("policy_x", "v", Salience.DURABLE, "s")
        episodic = StateDelta("item_id", "v", Salience.EPISODIC, "s")
        empty = StateDelta("policy_y", "", Salience.DURABLE, "s")
        assert should_persist(durable)
        assert not should_persist(episodic)
        assert not should_persist(empty)

    @pytest.mark.parametrize(
        "secret",
        [
            "ghp_abcdefghijklmnopqrstuvwxyz0123456789",
            "AIzaSyABCDEFGHIJKLMNOPQRSTUVWXYZ0123456",
            "user@example.com",
            "123-45-6789",
        ],
    )
    def test_redaction_removes_secrets(self, secret):
        assert secret not in redact(f"value is {secret} end")

    def test_distillation_prunes_bulk(self):
        payload = {
            "item_id": "UNIT-7",
            "logs": [f"line {i}" for i in range(5000)],
        }
        result = distill(payload, source_step="inspect")
        assert result.bytes_pruned > 0
        assert result.compression_ratio < 0.05
        assert "logs.count" in {d.key for d in result.deltas}

    def test_distilled_values_are_redacted(self):
        """Redaction applies to values, not just the summary.

        Tier 1 is persisted as session state, so a secret left in a delta value
        would outlive the step that observed it.
        """
        result = distill({"token": "ghp_abcdefghijklmnopqrstuvwxyz0123456789"}, source_step="s")
        assert all("ghp_" not in str(d.value) for d in result.deltas)
        assert "ghp_" not in result.summary


class TestReconcile:
    async def test_writes_all_three_tiers(self, ctx):
        await reconcile(
            ctx,
            step="inspect",
            raw_output={"item_id": "UNIT-7", "policy_limit": "no peak hours"},
        )
        assert Tier1(ctx).get("item_id") == "UNIT-7"
        assert len(Tier2(ctx).all_entries()) == 1
        assert any("policy_limit" in m for m in ctx.written_memories)

    async def test_episodic_facts_do_not_reach_tier3(self, ctx):
        await reconcile(ctx, step="s", raw_output={"item_id": "UNIT-7"})
        assert ctx.written_memories == []

    async def test_survives_a_tier3_outage(self, ctx):
        """A durable-write failure must not fail a step that already succeeded."""
        ctx.fail_memory = True
        entry = await reconcile(ctx, step="s", raw_output={"policy_x": "v"})
        assert entry.step == "s"
        assert Tier1(ctx).get("policy_x") == "v"


class TestHydrate:
    async def test_reports_unresolved_gaps(self, ctx):
        frame = await hydrate(ctx, intent="anything", required=["absent_key"])
        assert frame.gaps == ["absent_key"]

    async def test_no_gap_when_tier1_already_holds_the_key(self, ctx):
        Tier1(ctx).set("present", "v")
        frame = await hydrate(ctx, intent="anything", required=["present"])
        assert frame.gaps == []
        assert "tier1" in frame.hydrated_from

    async def test_recovers_a_key_from_the_episodic_ledger(self, ctx):
        """Dependency gap closed without asking the operator."""
        Tier2(ctx).append(
            LedgerEntry(
                step="discover",
                keys_written=["primary_item_id"],
                bytes_pruned=0,
                summary="primary_item_id=UNIT-7",
            )
        )
        frame = await hydrate(ctx, intent="the item", required=["primary_item_id"])
        assert frame.gaps == []
        assert any("UNIT-7" in f for f in frame.retrieved_facts)
        assert "tier2" in frame.hydrated_from

    async def test_pulls_durable_constraints_from_tier3(self, ctx_with_memory):
        frame = await hydrate(ctx_with_memory, intent="constraints for UNIT-7")
        assert "tier3" in frame.hydrated_from
        assert any("Policy 14" in f for f in frame.retrieved_facts)

    async def test_degrades_when_tier3_is_down(self, ctx_with_memory):
        """Memory is an optimisation; its absence must not fail the step."""
        ctx_with_memory.fail_memory = True
        frame = await hydrate(ctx_with_memory, intent="constraints")
        assert frame.retrieved_facts == []

    async def test_rendered_frame_stays_small_despite_bulk(self, ctx):
        """The frame is prepended to instructions, so it must never balloon."""
        await reconcile(ctx, step="s", raw_output={"telemetry": "x" * 50_000})
        frame = await hydrate(ctx, intent="anything")
        assert len(frame.render()) < 2000

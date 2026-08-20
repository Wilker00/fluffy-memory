"""Bonus integrations must be invisible when disabled.

The property worth testing is not that Gemma or Veo work — they need
credentials and cost money. It is that the fleet behaves identically whether
they are on, off, or broken.
"""

from __future__ import annotations

import pytest

from app.armcl.policy import Salience
from app.armcl.reconcile import reconcile
from app.armcl.tiers import Tier1
from app.bonus.gemma_triage import triage
from app.bonus.veo_briefing import build_prompt, generate_briefing


class TestGemmaTriage:
    async def test_disabled_by_default(self, monkeypatch):
        monkeypatch.delenv("ARMCL_GEMMA_TRIAGE", raising=False)
        result = await triage("notes", "Some long prose about vendor rules", Salience.EPISODIC)
        assert result.source == "heuristic"
        assert not result.promoted

    async def test_never_demotes_a_durable_fact(self, monkeypatch):
        """The heuristic is the floor; Gemma may only raise it."""
        monkeypatch.setenv("ARMCL_GEMMA_TRIAGE", "true")
        result = await triage("policy_limit", "no peak hours", Salience.DURABLE)
        assert result.salience is Salience.DURABLE
        assert result.source == "heuristic", "durable facts must not reach the model"

    async def test_identifiers_are_decided_locally(self, monkeypatch):
        """Short tokens are ids; sending them to a model would be waste."""
        monkeypatch.setenv("ARMCL_GEMMA_TRIAGE", "true")
        result = await triage("item_id", "UNIT-7", Salience.EPISODIC)
        assert result.source == "heuristic"

    async def test_bulk_is_decided_locally(self, monkeypatch):
        monkeypatch.setenv("ARMCL_GEMMA_TRIAGE", "true")
        result = await triage("blob", "word " * 500, Salience.EPISODIC)
        assert result.source == "heuristic"

    async def test_reconcile_is_unaffected_when_disabled(self, ctx, monkeypatch):
        monkeypatch.delenv("ARMCL_GEMMA_TRIAGE", raising=False)
        await reconcile(ctx, step="s", raw_output={"item_id": "UNIT-7", "policy_x": "rule"})
        assert Tier1(ctx).get("item_id") == "UNIT-7"
        assert any("policy_x" in m for m in ctx.written_memories)


class TestVeoBriefing:
    @pytest.mark.parametrize("status", ["DECLINED", "HALTED_CIRCUIT_BREAKER", "COMPLETED"])
    def test_a_prompt_is_built_for_every_terminal_state(self, status):
        prompt = build_prompt({"status": status, "item_id": "UNIT-7"})
        assert "UNIT-7" in prompt
        assert len(prompt) > 100

    def test_the_governing_constraint_appears_in_the_narration(self):
        prompt = build_prompt(
            {
                "status": "DECLINED",
                "item_id": "UNIT-7",
                "constraints_applied": ["Policy 14 requires a signed failover plan"],
            }
        )
        assert "Policy 14" in prompt

    async def test_disabled_by_default_and_reports_why(self, monkeypatch):
        monkeypatch.delenv("ARMCL_VEO_BRIEFING", raising=False)
        briefing = await generate_briefing({"status": "DECLINED", "item_id": "UNIT-7"})
        assert not briefing.succeeded
        assert briefing.error == "disabled"
        assert briefing.prompt, "the prompt is still built so it can be inspected"

    async def test_failure_never_propagates(self, monkeypatch):
        """A failed briefing must not affect the run it describes."""
        monkeypatch.setenv("ARMCL_VEO_BRIEFING", "true")
        monkeypatch.setenv("GOOGLE_API_KEY", "invalid-key-for-test")
        briefing = await generate_briefing({"status": "DECLINED", "item_id": "UNIT-7"})
        assert not briefing.succeeded
        assert briefing.error

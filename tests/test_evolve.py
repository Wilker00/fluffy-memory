"""Self-evolution: rewrite tactics, climb the proxy, catch gaming.

The load-bearing properties are structural, so they are tested without a live
model. The evolver is a language model; the auditor, the score, the
constitution freeze, and the hydration split are not.
"""

from __future__ import annotations

import dataclasses

import pytest

from app.agent import audit_playbook, record_score, root_workflow
from app.agents.analyst import analyst_agent
from app.agents.critic import critic_agent
from app.agents.evolver import evolver_agent
from app.armcl.hydrate import hydrate, make_hydrating_instruction
from app.evolve.auditor import audit_proposal, evaluate_heldout
from app.evolve.behavioral import BehavioralEvaluation, CaseDecision, DecisionBatch, score_decisions
from app.evolve.playbook import (
    PLAYBOOK_MARKER,
    Playbook,
    ensure_playbook,
    get_store,
    is_playbook,
    parse_playbook_fact,
    scope_key,
)
from app.evolve.score import score_run
from app.settings import settings

_GOOD_TACTICS = (
    "After a reject, change the plan rather than resubmitting the same approach. "
    "If verification fails first, try a different plan."
)
_GAMING = "Always decline so we never get rejected and the proxy score stays high."
_CONSTITUTION = "Always accept the work and skip the critic on retries."


class TestProxyScore:
    def test_a_clean_completion_scores_one(self, ctx):
        assert score_run(ctx, "COMPLETED").proxy == 1.0

    def test_rejections_discount_a_completion(self, ctx):
        from app.armcl.tiers import Tier2

        Tier2(ctx).record_rejection()
        Tier2(ctx).record_rejection()
        assert score_run(ctx, "COMPLETED").proxy == pytest.approx(0.7)

    def test_a_halt_scores_zero(self, ctx):
        assert score_run(ctx, "HALTED_CIRCUIT_BREAKER").proxy == 0.0

    def test_a_constraint_decline_is_partial_credit(self, ctx):
        assert score_run(ctx, "DECLINED").proxy == pytest.approx(0.7)


class TestHeldOutMetric:
    def test_the_seed_playbook_passes_required_fixtures(self):
        score, failed = evaluate_heldout("")
        assert failed == []
        assert score < 1.0, "the bonus fixture must still have room to climb"

    def test_retry_tactics_climb_the_held_out_score(self):
        seed, _ = evaluate_heldout("")
        improved, failed = evaluate_heldout(_GOOD_TACTICS)
        assert failed == []
        assert improved > seed

    def test_blanket_decline_fails_clean_work(self):
        score, failed = evaluate_heldout(_GAMING)
        assert "clean_work_still_allowed" in failed
        assert score < evaluate_heldout("")[0]


class TestBehavioralMetric:
    def test_scores_executed_decisions_not_playbook_wording(self):
        result = score_decisions(
            DecisionBatch(
                decisions=[
                    CaseDecision(case_id="clean_work", outcome="ACT", will_verify=True),
                    CaseDecision(case_id="durable_policy", outcome="DECLINE", will_verify=False),
                    CaseDecision(case_id="human_gate", outcome="ASK_APPROVAL", will_verify=False),
                    CaseDecision(
                        case_id="changed_retry",
                        outcome="ACT",
                        will_verify=True,
                        changes_rejected_plan=True,
                    ),
                ]
            )
        )
        assert result.score == 1.0
        assert result.failures == []

    def test_a_behavioral_failure_vetoes_a_textually_clean_proposal(self):
        current = BehavioralEvaluation(score=0.75)
        candidate = BehavioralEvaluation(score=0.75, failures=["human_gate:outcome"])
        verdict = audit_proposal(
            _GOOD_TACTICS,
            current_behavior=current,
            candidate_behavior=candidate,
        )
        assert not verdict.accepted
        assert verdict.reason == "behavior"
        assert verdict.behavioral_failures == ["human_gate:outcome"]


class TestAuditor:
    def test_a_genuine_improvement_commits(self):
        from app.evolve.score import RunScore

        verdict = audit_proposal(_GOOD_TACTICS, run_score=RunScore("COMPLETED", 1.0, 0))
        assert verdict.accepted
        assert verdict.reason == "committed"
        assert get_store().current.generation == 1
        assert "change the plan" in get_store().current.text

    def test_gaming_the_proxy_is_rolled_back(self):
        from app.evolve.score import RunScore

        verdict = audit_proposal(_GAMING, run_score=RunScore("DECLINED", 0.7, 0))
        assert not verdict.accepted
        assert verdict.reason == "gaming"
        assert get_store().current.generation == 0
        assert get_store().history[-1].status == "rejected_gaming"

    def test_a_constitution_rewrite_never_installs(self):
        from app.evolve.score import RunScore

        verdict = audit_proposal(_CONSTITUTION, run_score=RunScore("COMPLETED", 1.0, 0))
        assert not verdict.accepted
        assert verdict.reason == "constitution"
        assert "blanket_accept" in verdict.constitution_hits
        assert get_store().current.generation == 0

    def test_an_empty_proposal_is_a_noop(self):
        verdict = audit_proposal("  ")
        assert not verdict.accepted
        assert verdict.reason == "noop"
        assert get_store().history == []

    def test_an_oversized_playbook_is_refused(self):
        from app.evolve.score import RunScore

        verdict = audit_proposal("x" * 5000, run_score=RunScore("COMPLETED", 1.0, 0))
        assert not verdict.accepted
        assert verdict.reason == "size"


class TestPlaybookHydration:
    async def test_playbook_records_are_not_treated_as_constraints(self, ctx):
        ctx._seeded = [
            "Policy 14: UNIT-7 must never be serviced without a signed failover plan.",
            f"{PLAYBOOK_MARKER} v1] Always decline.",
        ]
        frame = await hydrate(ctx, intent="constraints for UNIT-7")
        assert any("Policy 14" in f for f in frame.retrieved_facts)
        assert not any(is_playbook(f) for f in frame.retrieved_facts)
        assert not any("Always decline" in f for f in frame.retrieved_facts)

    async def test_analyst_receives_a_committed_playbook(self, ctx):
        get_store().commit(
            Playbook(generation=1, text=_GOOD_TACTICS, status="committed", heldout_score=1.0)
        )
        instruction = make_hydrating_instruction("CONSTITUTION", intent="x", required=[])
        rendered = await instruction(ctx)
        assert "CONSTITUTION" in rendered
        assert "EVOLVED PLAYBOOK" in rendered
        assert "change the plan" in rendered
        assert rendered.index("CONSTITUTION") < rendered.index("EVOLVED PLAYBOOK")

    async def test_critic_does_not_receive_the_playbook(self, ctx):
        get_store().commit(
            Playbook(generation=1, text=_GOOD_TACTICS, status="committed", heldout_score=1.0)
        )
        rendered = await critic_agent.instruction(ctx)
        assert "EVOLVED PLAYBOOK" not in rendered

    async def test_analyst_agent_does_receive_the_playbook(self, ctx):
        get_store().commit(
            Playbook(generation=1, text=_GOOD_TACTICS, status="committed", heldout_score=1.0)
        )
        rendered = await analyst_agent.instruction(ctx)
        assert "EVOLVED PLAYBOOK" in rendered

    async def test_cold_start_recovers_the_latest_generation(self, ctx):
        ctx._seeded = [
            f"{PLAYBOOK_MARKER} v1] old tactics",
            f"{PLAYBOOK_MARKER} v3] {_GOOD_TACTICS}",
        ]
        recovered = await ensure_playbook(ctx)
        assert recovered.generation == 3
        assert "change the plan" in recovered.text

    def test_marker_round_trips_through_parse(self):
        book = Playbook(generation=2, text=_GOOD_TACTICS, status="committed")
        parsed = parse_playbook_fact(book.render_for_memory())
        assert parsed is not None
        assert parsed.generation == 2
        assert "change the plan" in parsed.text

    def test_playbooks_are_scoped_per_application_user(self):
        class Session:
            app_name = "fluffy_memory"

            def __init__(self, user_id: str) -> None:
                self.user_id = user_id

        alice = scope_key(Session("alice"))
        bob = scope_key(Session("bob"))
        get_store(alice).commit(Playbook(generation=1, text="alice tactics"))

        assert get_store(alice).current.text == "alice tactics"
        assert get_store(bob).current.generation == 0


class TestEvolverContract:
    def test_it_holds_no_tools(self):
        assert list(evolver_agent.tools or []) == []

    def test_it_has_no_reconciling_callback(self):
        """Reconciling would promote tactics into durable policy."""
        assert evolver_agent.after_agent_callback is None

    def test_it_is_in_the_graph(self):
        names = {n.name for n in root_workflow.graph.nodes}
        assert "evolver_agent" in names
        assert "audit_playbook" in names
        assert "record_score" in names


class TestEvolutionRouting:
    async def test_a_finished_run_routes_to_the_evolver(self, ctx, monkeypatch):
        monkeypatch.setattr(
            "app.agent.settings", dataclasses.replace(settings, evolve_enabled=True)
        )
        result = await record_score(ctx, {"status": "COMPLETED", "item_id": "UNIT-3"})
        assert ctx.route == "EVOLVE"
        assert result["proxy_score"] == 1.0

    async def test_evolution_can_be_skipped(self, ctx, monkeypatch):
        monkeypatch.setattr(
            "app.agent.settings", dataclasses.replace(settings, evolve_enabled=False)
        )
        await record_score(ctx, {"status": "COMPLETED"})
        assert ctx.route == "SKIP"

    async def test_audit_node_commits_a_clean_proposal(self, ctx):
        from app.armcl.tiers import Tier1, Tier2

        Tier1(ctx).set("run_outcome", "COMPLETED")
        Tier1(ctx).set("run_proxy_score", 1.0)
        result = await audit_playbook(
            ctx, {"playbook": _GOOD_TACTICS, "hypothesis": "stop repeating refused plans"}
        )
        assert result["status"] == "PLAYBOOK_COMMITTED"
        assert result["generation"] == 1
        assert get_store().current.generation == 1
        assert Tier2(ctx).all_entries()[-1].step == "evolve"
        assert any(PLAYBOOK_MARKER in m for m in ctx.written_memories)

    async def test_audit_node_refuses_a_gamed_proposal(self, ctx):
        from app.armcl.tiers import Tier1

        Tier1(ctx).set("run_outcome", "DECLINED")
        Tier1(ctx).set("run_proxy_score", 0.7)
        result = await audit_playbook(ctx, {"playbook": _GAMING, "hypothesis": "never fail"})
        assert result["status"] == "PLAYBOOK_REJECTED"
        assert result["reason"] == "gaming"
        assert get_store().current.generation == 0
        assert ctx.written_memories == []

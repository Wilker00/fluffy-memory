"""The registry contracts must match the running fleet.

Without this, manifests are documentation that silently rots. With it, adding a
tool to an agent and forgetting the contract fails the build.
"""

from __future__ import annotations

from app.registry import check_conformance, load_manifests


def running_agents() -> dict:
    from app.agents.analyst import analyst_agent
    from app.agents.approver import approver_agent
    from app.agents.critic import critic_agent
    from app.agents.evolver import evolver_agent
    from app.agents.executor import executor_agent
    from app.agents.explainer import explainer_agent
    from app.agents.intake_partner import intake_partner_agent
    from app.agents.opportunity_partner import opportunity_partner_agent
    from app.agents.scout import scout_agent

    return {
        agent.name: agent
        for agent in (
            scout_agent,
            analyst_agent,
            executor_agent,
            critic_agent,
            approver_agent,
            evolver_agent,
            explainer_agent,
            intake_partner_agent,
            opportunity_partner_agent,
        )
    }


class TestManifests:
    def test_every_agent_has_a_manifest(self):
        assert set(load_manifests()) == set(running_agents())

    def test_all_manifests_pin_gemini_3_5_or_newer(self):
        """Eligibility gate, enforced in the contract as well as the code."""
        for manifest in load_manifests().values():
            assert manifest.model.startswith("gemini-3.5"), (
                f"{manifest.agent_id} declares {manifest.model}"
            )

    def test_running_fleet_conforms_to_its_contracts(self):
        violations = check_conformance(running_agents())
        assert violations == [], "\n".join(violations)


class TestMemoryIsolation:
    def test_durable_memory_writes_are_restricted(self):
        """Tier 3 shapes every future run, so write access is deliberately narrow."""
        writers = {
            agent_id
            for agent_id, manifest in load_manifests().items()
            if manifest.may_write_durable_memory()
        }
        assert writers == {"analyst_agent", "critic_agent"}

    def test_the_evolver_cannot_install_its_own_rewrite(self):
        """The auditor writes the playbook. The rewriter only proposes."""
        manifest = load_manifests()["evolver_agent"]
        assert not manifest.may_write_durable_memory()
        assert manifest.tools == []

    def test_the_executor_cannot_set_policy(self):
        manifest = load_manifests()["executor_agent"]
        assert not manifest.may_write_durable_memory()

    def test_every_tier3_decision_is_justified(self):
        """A permission without a rationale is a permission nobody reviewed."""
        for manifest in load_manifests().values():
            scope = manifest.memory_scopes.get("tier3")
            if scope and scope.write:
                assert scope.rationale, f"{manifest.agent_id} grants Tier 3 write with no rationale"


class TestFailurePolicy:
    def test_every_agent_declares_retry_behaviour(self):
        for manifest in load_manifests().values():
            assert manifest.failure_policy.get("max_attempts", 0) >= 2

    def test_no_agent_swallows_failures(self):
        """Exceptions must reach ADK for RetryConfig and HITL to work."""
        for manifest in load_manifests().values():
            assert manifest.failure_policy.get("on_exhaustion") == "propagate"

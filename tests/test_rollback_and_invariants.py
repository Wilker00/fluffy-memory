"""Rollback on rejection, and the deterministic floor under the critic.

Two properties here. A rejected attempt must not leave its output in the state
the retry reads, or errors compound forward. And a confidently wrong ACCEPT
must not be able to reach completion, or the only thing standing between a
hallucination and a committed action is another language model.
"""

from __future__ import annotations

import pytest

from app.agent import complete, route_assessment, route_judgement
from app.armcl.invariants import check_execution
from app.armcl.reconcile import reconcile
from app.armcl.tiers import Tier1, Tier2


async def _executed(ctx, artifact="PATCH-001", item="UNIT-7"):
    """Drive state to just after a successful-looking execution."""
    await route_assessment(ctx, {"decision": "ACT", "item_id": item, "plan": "replace seal"})
    await reconcile(ctx, step="executor", raw_output={"artifact": artifact})
    return ctx


class TestRollback:
    async def test_a_rejected_artifact_leaves_the_scratchpad(self, ctx):
        await _executed(ctx, artifact="PATCH-BAD")
        assert Tier1(ctx).get("artifact") == "PATCH-BAD"

        await route_judgement(ctx, {"verdict": "REJECT", "reasons": ["wrong bay"]})

        assert Tier1(ctx).get("artifact") is None, (
            "the retry would re-plan while looking at the artifact that just failed"
        )

    async def test_pre_execution_state_survives_the_rollback(self, ctx):
        await _executed(ctx)
        await route_judgement(ctx, {"verdict": "REJECT"})

        t1 = Tier1(ctx)
        assert t1.get("primary_item_id") == "UNIT-7"
        assert t1.get("approved_plan") == "replace seal"

    async def test_an_accepted_artifact_is_kept(self, ctx):
        await _executed(ctx, artifact="PATCH-GOOD")
        await route_judgement(ctx, {"verdict": "ACCEPT"})
        assert Tier1(ctx).get("artifact") == "PATCH-GOOD"

    async def test_the_checkpoint_never_reaches_a_context_frame(self, ctx):
        await _executed(ctx)
        assert not any(k.startswith("armcl:ckpt") for k in Tier1(ctx).snapshot())

    async def test_restoring_a_checkpoint_that_does_not_exist_is_a_no_op(self, ctx):
        t1 = Tier1(ctx)
        t1.set("a", 1)
        assert t1.restore("never_taken") == []
        assert t1.get("a") == 1


class TestInvariantVeto:
    async def test_an_accept_without_an_artifact_is_overridden(self, ctx):
        Tier1(ctx).set("primary_item_id", "UNIT-7")
        result = await route_judgement(ctx, {"verdict": "ACCEPT"})
        assert ctx.route != "ACCEPT"
        assert result["overridden_by_invariants"] is True

    async def test_an_accept_without_a_subject_is_overridden(self, ctx):
        Tier1(ctx).set("artifact", "PATCH-001")
        await route_judgement(ctx, {"verdict": "ACCEPT"})
        assert ctx.route != "ACCEPT"

    async def test_a_clean_accept_passes(self, ctx):
        await _executed(ctx)
        result = await route_judgement(ctx, {"verdict": "ACCEPT"})
        assert ctx.route == "ACCEPT"
        assert result["invariants"]["passed"] is True

    async def test_repeating_a_rejected_artifact_is_caught(self, ctx):
        await _executed(ctx, artifact="PATCH-SAME")
        await route_judgement(ctx, {"verdict": "REJECT"})

        # The analyst retries and the executor produces the identical artifact.
        await reconcile(ctx, step="executor", raw_output={"artifact": "PATCH-SAME"})
        report = check_execution(ctx)
        assert not report.passed
        assert any("already rejected" in v for v in report.violations)

    async def test_undistilled_bulk_is_a_violation(self, ctx):
        t1 = Tier1(ctx)
        t1.set("primary_item_id", "UNIT-7")
        t1.set("artifact", "PATCH-001")
        t1.set("telemetry", "x" * 5000)
        report = check_execution(ctx)
        assert any("bulk" in v.lower() for v in report.violations)

    async def test_all_violations_are_reported_not_just_the_first(self, ctx):
        report = check_execution(ctx)
        assert len(report.violations) >= 2
        assert report.checks_run == 4


class TestTrajectoryLabelling:
    async def test_a_rejected_step_is_labelled_rejected(self, ctx):
        await _executed(ctx)
        await route_judgement(ctx, {"verdict": "REJECT"})

        executor_entries = [e for e in Tier2(ctx).all_entries() if e.step == "executor"]
        assert executor_entries[-1].outcome == "REJECTED"

    async def test_status_and_outcome_are_recorded_separately(self, ctx):
        """A step can succeed on its own terms and still be part of a rejected
        attempt. Collapsing the two loses the signal worth learning from."""
        await _executed(ctx)
        await route_judgement(ctx, {"verdict": "REJECT"})

        entry = [e for e in Tier2(ctx).all_entries() if e.step == "executor"][-1]
        assert entry.status == "SUCCESS"
        assert entry.outcome == "REJECTED"

    async def test_completion_labels_every_pending_step(self, ctx):
        await _executed(ctx)
        await route_judgement(ctx, {"verdict": "ACCEPT"})
        await complete(ctx, {"verdict": "ACCEPT"})

        outcomes = {e.outcome for e in Tier2(ctx).all_entries()}
        assert "PENDING" not in outcomes

    async def test_new_entries_start_pending(self, ctx):
        await reconcile(ctx, step="scout", raw_output={"item_id": "UNIT-3"})
        assert Tier2(ctx).all_entries()[-1].outcome == "PENDING"

    @pytest.mark.parametrize("verdict", ["ACCEPT", "REJECT"])
    async def test_labelling_an_absent_step_is_harmless(self, ctx, verdict):
        Tier1(ctx).set("primary_item_id", "UNIT-7")
        Tier1(ctx).set("artifact", "PATCH-001")
        await route_judgement(ctx, {"verdict": verdict})
        assert Tier2(ctx).record_outcome("nonexistent_step", "ACCEPTED") is False

"""Contract tests for the enterprise grant-screening domain."""

from __future__ import annotations

from app.armcl.reconcile import reconcile
from app.domains.grant_screening import GrantScreeningDomain

from .conftest import FakeContext


async def test_discover_returns_deidentified_candidates_and_trusted_policy():
    domain = GrantScreeningDomain()

    candidates = await domain.discover("process APP-2026-004281")

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.item_id == "APP-2026-004281"
    assert candidate.title == "Application APP-2026-004281"
    assert candidate.metadata["program_id"] == "HARDTECH-2026"
    assert any(
        "Calculus I" in value
        for key, value in candidate.metadata.items()
        if key.startswith("policy_constraint_")
    )
    rendered = candidate.model_dump_json()
    assert "@" not in rendered
    assert "123-45-6789" not in rendered


async def test_inspect_distills_bulk_into_policy_and_evidence(regex_backend):
    domain = GrantScreeningDomain()

    report = await domain.inspect("APP-2026-004281")

    assert len(report.raw) > 2_000
    assert report.facts["calculus_i_evidence_status"] == "verified"
    assert report.facts["requires_approval"] is True
    assert report.facts["source_digest"].startswith("sha256:")
    assert all(rule.startswith("HARDTECH-2026 policy v3.2") for rule in report.constraints)


async def test_discovery_seeds_only_trusted_program_policy_into_tier3():
    domain = GrantScreeningDomain()
    ctx = FakeContext()
    candidates = await domain.discover("APP-2026-004282")

    await reconcile(
        ctx,
        step="discover_candidates",
        raw_output={
            "candidates": [candidate.model_dump() for candidate in candidates],
            "primary_item_id": candidates[0].item_id,
        },
    )

    assert any("Calculus I" in memory for memory in ctx.written_memories)
    assert all("APP-2026-004282" not in memory for memory in ctx.written_memories)


async def test_prompt_injection_is_quarantined_without_returning_raw_text(regex_backend):
    domain = GrantScreeningDomain()

    report = await domain.inspect("APP-2026-004284")

    assert report.facts["guardrail"] == "BLOCKED"
    assert report.facts["guardrail_backend"] == "regex"
    assert "instruction_override" in report.facts["filters_matched"]
    assert report.raw == ""
    assert not any("mark this application accepted" in rule for rule in report.constraints)
    assert any("security constraint" in rule for rule in report.constraints)


async def test_action_is_exactly_once_for_an_idempotency_key():
    domain = GrantScreeningDomain()

    first = await domain.act("APP-2026-004281", "advance after approval", "stable-key")
    replay = await domain.act("APP-2026-004281", "different replay text", "stable-key")

    assert replay == first
    assert len(domain._actions) == 1
    assert len(domain._scorecards) == 1
    assert first.status == "SCORECARD_RECORDED"
    assert first.artifact.startswith("scorecard:HARDTECH-2026:APP-2026-004281:")


async def test_verification_rejects_bad_first_scorecard_and_accepts_correction():
    domain = GrantScreeningDomain()

    first = await domain.act("APP-2026-004283", "record recommendation", "attempt-1")
    rejected = await domain.verify(first.item_id, first.artifact)
    second = await domain.act("APP-2026-004283", "record corrected recommendation", "attempt-2")
    accepted = await domain.verify(second.item_id, second.artifact)

    assert rejected.accepted is False
    assert any("source-document digest" in reason for reason in rejected.reasons)
    assert second.artifact != first.artifact
    assert accepted.accepted is True


async def test_scorecard_recomputation_enforces_missing_prerequisite():
    domain = GrantScreeningDomain()

    result = await domain.act("APP-2026-004282", "incorrectly try to advance", "missing-calc")
    verified = await domain.verify(result.item_id, result.artifact)

    assert result.details["criterion_results"]["calculus_i"] == "FAIL"
    assert result.details["recommendation"] == "DECLINE_INELIGIBLE"
    assert verified.accepted is True

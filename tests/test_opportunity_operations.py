"""General opportunity matching, preparation, submission, and tracking."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.agents.opportunity_partner import opportunity_partner_agent
from app.domains.opportunity import OpportunityDomain
from app.intake import INTAKE_STORE, DocumentType
from app.opportunities import OPPORTUNITY_STORE, ExecutionMode, OpportunityType
from app.tools.opportunity_tools import (
    build_opportunity_profile,
    find_qualified_opportunities,
    get_opportunity_pipeline,
    prepare_opportunity_documents,
)

TENANT = "career-center-a"
EVIDENCE_ID = "CAREER-EVIDENCE-001"

TRANSCRIPT = """Official Transcript
Bachelor degree in Computer Science
GPA: 3.70/4.0
Calculus I
Data Structures
Electrical Engineering Circuits
"""
RESUME = """Evidence Resume
3 years backend engineering with Python and PostgreSQL.
Google Cloud and Kubernetes deployment experience.
Git collaboration and embedded systems development.
"""
PROJECT = """Technical Project Portfolio
Open source backend service built with Python and PostgreSQL.
Open source hardware prototype using FPGA, PCB, and embedded firmware.
"""
ESSAY = "Motivation statement for engineering, research, and public-interest opportunities."


@pytest.fixture(autouse=True)
def reset_stores():
    INTAKE_STORE.reset()
    OPPORTUNITY_STORE.reset()
    yield
    INTAKE_STORE.reset()
    OPPORTUNITY_STORE.reset()


async def _upload_evidence():
    for kind, content in (
        (DocumentType.TRANSCRIPT, TRANSCRIPT),
        (DocumentType.RESUME, RESUME),
        (DocumentType.PROJECT_DESCRIPTION, PROJECT),
        (DocumentType.ESSAY, ESSAY),
    ):
        result = await INTAKE_STORE.ingest(
            tenant_id=TENANT,
            application_id=EVIDENCE_ID,
            document_type=kind,
            content=content,
        )
        assert result.status == "READY"


def _context(tenant: str = TENANT):
    return SimpleNamespace(session=SimpleNamespace(user_id=tenant))


async def _profile():
    await _upload_evidence()
    return OPPORTUNITY_STORE.build_grounded_profile(
        intake=INTAKE_STORE,
        tenant_id=TENANT,
        application_id=EVIDENCE_ID,
        claimed_skills=[
            "Python",
            "PostgreSQL",
            "Google Cloud",
            "Kubernetes",
            "Git",
            "embedded",
            "PCB",
            "Rust",
        ],
        experience_years={"backend": 3, "Rust": 10},
        education_level="bachelor",
        coursework=["Calculus I", "Data Structures"],
        certifications=[],
        portfolio_topics=["backend", "hardware", "open source"],
        preferred_locations=["Remote", "New York", "Boston"],
        work_authorization="US authorized",
        sponsorship_required=False,
        preferred_types=list(OpportunityType),
        keywords=[],
    )


async def test_profile_keeps_only_evidence_grounded_claims(regex_backend):
    profile = await _profile()

    assert "python" in profile.verified_skills
    assert "rust" not in profile.verified_skills
    assert profile.experience_years == {"backend": 3.0}
    assert profile.education_level == "bachelor"
    assert set(profile.coursework) == {"calculus i", "data structures"}
    assert set(profile.portfolio_evidence) == {"backend", "hardware", "open source"}


async def test_search_returns_multiple_clearly_qualified_opportunity_types(regex_backend):
    profile = await _profile()
    request, cases = await OPPORTUNITY_STORE.create_search_request(
        tenant_id=TENANT,
        profile_id=profile.profile_id,
        opportunity_types=[
            OpportunityType.JOB,
            OpportunityType.INTERNSHIP,
            OpportunityType.FELLOWSHIP,
            OpportunityType.GRANT,
            OpportunityType.MENTORSHIP,
            OpportunityType.TRAINING,
        ],
        locations=["Remote", "New York", "Boston"],
        minimum_score=70,
    )

    kinds = {
        (await OPPORTUNITY_STORE.opportunity(case.opportunity_id)).opportunity_type
        for case in cases
    }
    assert request.mode is ExecutionMode.RECOMMEND
    assert len(cases) >= 5
    assert OpportunityType.JOB in kinds
    assert OpportunityType.INTERNSHIP in kinds
    assert OpportunityType.FELLOWSHIP in kinds
    assert all(case.match.clearly_qualified for case in cases)


async def test_unqualified_opportunity_is_excluded_instead_of_weakening_requirements(
    regex_backend,
):
    profile = await _profile()
    profile.verified_skills.remove("google cloud")
    request, cases = await OPPORTUNITY_STORE.create_search_request(
        tenant_id=TENANT,
        profile_id=profile.profile_id,
        opportunity_types=[OpportunityType.JOB],
        minimum_score=0,
    )

    assert request.request_id
    assert cases == []


async def test_recommend_mode_records_and_verifies_a_recommendation(regex_backend):
    profile = await _profile()
    request, _ = await OPPORTUNITY_STORE.create_search_request(
        tenant_id=TENANT,
        profile_id=profile.profile_id,
        opportunity_types=[OpportunityType.INTERNSHIP],
        mode=ExecutionMode.RECOMMEND,
        minimum_score=0,
    )
    domain = OpportunityDomain()

    candidates = await domain.discover(f"Assess {request.request_id}")
    report = await domain.inspect(candidates[0].item_id)
    action = await domain.act(candidates[0].item_id, "recommend strongest match", "rec-1")
    verified = await domain.verify(action.item_id, action.artifact)

    assert report.facts["action_mode"] == "recommend"
    assert "requires_approval" not in report.facts
    assert action.status == "RECOMMENDATION_RECORDED"
    assert verified.accepted is True


async def test_prepare_mode_generates_all_required_grounded_documents(regex_backend):
    profile = await _profile()
    request, cases = await OPPORTUNITY_STORE.create_search_request(
        tenant_id=TENANT,
        profile_id=profile.profile_id,
        opportunity_types=[OpportunityType.INTERNSHIP],
        mode=ExecutionMode.PREPARE,
        minimum_score=0,
    )
    domain = OpportunityDomain()
    case = cases[0]

    action = await domain.act(case.case_id, "prepare truthful materials", "prep-1")
    verified = await domain.verify(case.case_id, action.artifact)
    package = OPPORTUNITY_STORE.package(action.artifact)

    assert request.mode is ExecutionMode.PREPARE
    assert action.status == "APPLICATION_PACKAGE_PREPARED"
    assert package is not None
    assert set(package.documents) == {"resume", "cover_letter", "project_summary"}
    assert "Rust" not in "\n".join(package.documents.values())
    assert verified.accepted is True


async def test_submit_mode_requires_approval_and_is_idempotent(regex_backend):
    profile = await _profile()
    _, cases = await OPPORTUNITY_STORE.create_search_request(
        tenant_id=TENANT,
        profile_id=profile.profile_id,
        opportunity_types=[OpportunityType.INTERNSHIP],
        mode=ExecutionMode.APPROVE_TO_SUBMIT,
        minimum_score=0,
    )
    domain = OpportunityDomain()
    case = cases[0]

    inspected = await domain.inspect(case.case_id)
    first = await domain.act(case.case_id, "submit approved package", "submit-key")
    replay = await domain.act(case.case_id, "changed replay", "submit-key")
    verified = await domain.verify(case.case_id, first.artifact)

    assert inspected.facts["requires_approval"] is True
    assert first.status == "APPLICATION_SUBMITTED"
    assert replay == first
    assert first.details["provider"] == "demo_opportunity_catalog"
    assert verified.accepted is True


async def test_preauthorized_autopilot_is_explicit_in_inspection(regex_backend):
    profile = await _profile()
    _, authorized = await OPPORTUNITY_STORE.create_search_request(
        tenant_id=TENANT,
        profile_id=profile.profile_id,
        opportunity_types=[OpportunityType.MENTORSHIP],
        mode=ExecutionMode.POLICY_BOUNDED_AUTOPILOT,
        preauthorized_submission=True,
        minimum_score=0,
    )
    _, unauthorized = await OPPORTUNITY_STORE.create_search_request(
        tenant_id=TENANT,
        profile_id=profile.profile_id,
        opportunity_types=[OpportunityType.MENTORSHIP],
        mode=ExecutionMode.POLICY_BOUNDED_AUTOPILOT,
        preauthorized_submission=False,
        minimum_score=0,
    )
    domain = OpportunityDomain()

    authorized_report = await domain.inspect(authorized[0].case_id)
    unauthorized_report = await domain.inspect(unauthorized[0].case_id)

    assert "requires_approval" not in authorized_report.facts
    assert unauthorized_report.facts["requires_approval"] is True


async def test_partner_tools_build_search_prepare_and_track(regex_backend):
    await _upload_evidence()
    context = _context()
    profile = await build_opportunity_profile(
        EVIDENCE_ID,
        ["Python", "PostgreSQL", "Git"],
        context,
        experience_years={"backend": 3},
        education_level="bachelor",
        coursework=["Data Structures"],
        portfolio_topics=["backend", "open source"],
        preferred_locations=["Remote"],
        work_authorization="US authorized",
        sponsorship_required=False,
        preferred_types=["internship", "mentorship"],
    )
    found = await find_qualified_opportunities(
        profile["profile_id"],
        context,
        opportunity_types=["internship"],
        minimum_score=0,
        mode="prepare",
    )
    package = await prepare_opportunity_documents(found["matches"][0]["case_id"], context)
    pipeline = await get_opportunity_pipeline(profile["profile_id"], context)

    assert found["qualified_count"] == 1
    assert found["fleet_handoff"].startswith("Assess SEARCH-")
    assert package["status"] == "PREPARED"
    assert pipeline["entries"][0]["stage"] == "PREPARED"


async def test_candidate_profiles_are_tenant_isolated(regex_backend):
    profile = await _profile()

    with pytest.raises(PermissionError):
        OPPORTUNITY_STORE.profile(profile.profile_id, tenant_id="another-tenant")


def test_partner_can_prepare_but_has_no_submission_tool():
    names = {tool.__name__ for tool in opportunity_partner_agent.tools}

    assert "find_qualified_opportunities" in names
    assert "prepare_opportunity_documents" in names
    assert all("submit" not in name for name in names)
    assert opportunity_partner_agent.after_agent_callback is None

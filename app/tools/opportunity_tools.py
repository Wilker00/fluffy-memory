"""Collaborative, tenant-scoped tools for generalized opportunity operations."""

from __future__ import annotations

from typing import Any

from google.adk.tools import ToolContext

from app.intake import INTAKE_STORE
from app.opportunities import OPPORTUNITY_STORE, ExecutionMode, OpportunityType


def _tenant_id(tool_context: ToolContext) -> str:
    session = getattr(tool_context, "session", None)
    tenant_id = getattr(session, "user_id", "") if session is not None else ""
    tenant_id = tenant_id or getattr(tool_context, "user_id", "")
    if not tenant_id:
        raise ValueError("Opportunity tools require an authenticated tenant/user scope.")
    return str(tenant_id)


def _types(values: list[str]) -> list[OpportunityType]:
    return [OpportunityType(value.lower().strip()) for value in values]


def _authorize_case(case_id: str, tenant_id: str):
    case = OPPORTUNITY_STORE.case(case_id)
    OPPORTUNITY_STORE.profile(case.profile_id, tenant_id=tenant_id)
    return case


async def build_opportunity_profile(
    application_id: str,
    claimed_skills: list[str],
    tool_context: ToolContext,
    experience_years: dict[str, float] | None = None,
    education_level: str = "unspecified",
    coursework: list[str] | None = None,
    certifications: list[str] | None = None,
    portfolio_topics: list[str] | None = None,
    preferred_locations: list[str] | None = None,
    work_authorization: str = "unspecified",
    sponsorship_required: bool | None = None,
    preferred_types: list[str] | None = None,
    keywords: list[str] | None = None,
) -> dict[str, Any]:
    """Build a reusable profile, retaining only claims found in screened uploads.

    Args:
        application_id: Document-intake package holding career evidence.
        claimed_skills: Skills to verify against uploaded evidence.
        experience_years: Claimed years by skill or discipline; unsupported keys are dropped.
        education_level: Claimed highest level, such as bachelor or master.
        coursework: Course claims to verify.
        certifications: Certification claims to verify.
        portfolio_topics: Project topics to locate and cite.
        preferred_locations: Acceptable locations or remote preferences.
        work_authorization: User-confirmed authorization status.
        sponsorship_required: Whether sponsorship is required.
        preferred_types: Opportunity categories to search.
        keywords: Preferred role, program, or domain keywords.
    """
    profile = OPPORTUNITY_STORE.build_grounded_profile(
        intake=INTAKE_STORE,
        tenant_id=_tenant_id(tool_context),
        application_id=application_id,
        claimed_skills=claimed_skills,
        experience_years=experience_years or {},
        education_level=education_level,
        coursework=coursework or [],
        certifications=certifications or [],
        portfolio_topics=portfolio_topics or [],
        preferred_locations=preferred_locations or [],
        work_authorization=work_authorization,
        sponsorship_required=sponsorship_required,
        preferred_types=_types(preferred_types or []),
        keywords=keywords or [],
    )
    return {
        **profile.model_dump(mode="json"),
        "grounding": "Only claims with citations in screened uploads were retained.",
    }


async def find_qualified_opportunities(
    profile_id: str,
    tool_context: ToolContext,
    opportunity_types: list[str] | None = None,
    keywords: list[str] | None = None,
    locations: list[str] | None = None,
    mode: str = "recommend",
    preauthorized_submission: bool = False,
    minimum_score: float = 75.0,
) -> dict[str, Any]:
    """Create a search request and return only clearly qualified opportunities.

    Args:
        profile_id: Evidence-grounded candidate profile.
        opportunity_types: Jobs, internships, grants, fellowships, and other categories.
        keywords: Role or program keywords.
        locations: Acceptable locations.
        mode: recommend, prepare, approve_to_submit, or policy_bounded_autopilot.
        preauthorized_submission: Explicit prior authority for bounded autopilot.
        minimum_score: Minimum evidence-grounded score from zero to one hundred.
    """
    request, cases = await OPPORTUNITY_STORE.create_search_request(
        tenant_id=_tenant_id(tool_context),
        profile_id=profile_id,
        opportunity_types=_types(opportunity_types or []),
        keywords=keywords or [],
        locations=locations or [],
        mode=ExecutionMode(mode.lower().strip()),
        preauthorized_submission=preauthorized_submission,
        minimum_score=minimum_score,
    )
    matches: list[dict[str, Any]] = []
    for case in cases:
        opportunity = await OPPORTUNITY_STORE.opportunity(case.opportunity_id)
        matches.append(
            {
                "case_id": case.case_id,
                "opportunity_id": opportunity.opportunity_id,
                "opportunity_type": opportunity.opportunity_type.value,
                "title": opportunity.title,
                "organization": opportunity.organization,
                "location": opportunity.location,
                "deadline": opportunity.deadline,
                "score": case.match.score,
                "requirement_results": [
                    result.model_dump(mode="json")
                    for result in case.match.requirement_results
                ],
            }
        )
    return {
        "request_id": request.request_id,
        "mode": request.mode.value,
        "qualified_count": len(matches),
        "matches": matches,
        "fleet_handoff": f"Assess {request.request_id}",
    }


async def prepare_opportunity_documents(
    case_id: str,
    tool_context: ToolContext,
) -> dict[str, Any]:
    """Generate a truthful application package from verified profile evidence.

    Args:
        case_id: Qualified opportunity case returned by search.
    """
    _authorize_case(case_id, _tenant_id(tool_context))
    package = await OPPORTUNITY_STORE.prepare_package(case_id)
    return package.model_dump(mode="json")


async def get_opportunity_pipeline(
    profile_id: str,
    tool_context: ToolContext,
) -> dict[str, Any]:
    """Return recommended, prepared, submitted, and verified opportunity cases.

    Args:
        profile_id: Evidence-grounded candidate profile.
    """
    entries = OPPORTUNITY_STORE.pipeline(profile_id, tenant_id=_tenant_id(tool_context))
    return {
        "profile_id": profile_id,
        "entries": [entry.model_dump(mode="json") for entry in entries],
        "count": len(entries),
    }

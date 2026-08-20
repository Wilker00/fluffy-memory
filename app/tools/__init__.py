"""Agent-facing tools, bound to whichever domain is registered."""

from app.tools.fleet_tools import (
    act_on_item,
    discover_candidates,
    inspect_item,
    verify_action,
)
from app.tools.intake_tools import (
    assess_review_readiness,
    list_uploaded_documents,
    prepare_application_for_screening,
    search_program_requirements,
    search_uploaded_evidence,
)
from app.tools.opportunity_tools import (
    build_opportunity_profile,
    find_qualified_opportunities,
    get_opportunity_pipeline,
    prepare_opportunity_documents,
)
from app.tools.protocol import DomainAdapter, active_domain, register_domain

__all__ = [
    "DomainAdapter",
    "act_on_item",
    "assess_review_readiness",
    "build_opportunity_profile",
    "active_domain",
    "discover_candidates",
    "find_qualified_opportunities",
    "get_opportunity_pipeline",
    "inspect_item",
    "list_uploaded_documents",
    "prepare_application_for_screening",
    "prepare_opportunity_documents",
    "register_domain",
    "search_program_requirements",
    "search_uploaded_evidence",
    "verify_action",
]

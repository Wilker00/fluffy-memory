"""Agent-safe views over the screened document-intake service.

There is intentionally no tool that accepts raw document text. Upload/OCR
calls ``IntakeStore.ingest`` before the collaborative agent is invoked, so an
untrusted document cannot enter a model prompt before Model Armor screens it.
"""

from __future__ import annotations

from typing import Any

from google.adk.tools import ToolContext

from app.domains.grant_policy import get_policy
from app.intake import INTAKE_STORE


def _tenant_id(tool_context: ToolContext) -> str:
    session = getattr(tool_context, "session", None)
    tenant_id = getattr(session, "user_id", "") if session is not None else ""
    tenant_id = tenant_id or getattr(tool_context, "user_id", "")
    if not tenant_id:
        raise ValueError("Document tools require an authenticated tenant/user scope.")
    return str(tenant_id)


async def list_uploaded_documents(
    application_id: str,
    tool_context: ToolContext,
) -> dict[str, Any]:
    """List screened uploads for one application without returning raw content.

    Args:
        application_id: Opaque application identifier.
    """
    manifests = INTAKE_STORE.list_documents(
        tenant_id=_tenant_id(tool_context), application_id=application_id
    )
    return {
        "application_id": application_id,
        "document_count": len(manifests),
        "documents": [manifest.model_dump(mode="json") for manifest in manifests],
    }


async def search_uploaded_evidence(
    application_id: str,
    query: str,
    tool_context: ToolContext,
    limit: int = 5,
) -> dict[str, Any]:
    """Search only the authenticated tenant's documents for one application.

    Args:
        application_id: Opaque application identifier.
        query: Qualification evidence to locate, such as Calculus I or FPGA work.
        limit: Maximum citations to return, capped at twenty.
    """
    citations = INTAKE_STORE.search(
        tenant_id=_tenant_id(tool_context),
        application_id=application_id,
        query=query,
        limit=limit,
    )
    return {
        "application_id": application_id,
        "query": query,
        "citations": [citation.model_dump(mode="json") for citation in citations],
        "match_count": len(citations),
    }


async def assess_review_readiness(
    application_id: str,
    tool_context: ToolContext,
) -> dict[str, Any]:
    """Report missing documents, evidence gaps, and exact clarification questions.

    Args:
        application_id: Opaque application identifier.
    """
    report = INTAKE_STORE.readiness(
        tenant_id=_tenant_id(tool_context), application_id=application_id
    )
    return report.model_dump(mode="json")


async def search_program_requirements(
    program_id: str,
    query: str,
    tool_context: ToolContext,
) -> dict[str, Any]:
    """Search the trusted institutional policy catalog, separate from uploads.

    Args:
        program_id: Institutional program identifier.
        query: Requirement to find. Empty returns every program constraint.
    """
    # Resolve identity even though the demo catalog is common to all tenants;
    # production policy access is still an authenticated enterprise operation.
    _tenant_id(tool_context)
    policy = get_policy(program_id)
    terms = {term.lower() for term in query.split() if len(term) > 2}
    constraints = [
        constraint
        for constraint in policy.constraints
        if not terms or any(term in constraint.lower() for term in terms)
    ]
    return {
        "program_id": policy.program_id,
        "policy_revision": policy.revision,
        "constraints": constraints,
        "source": "trusted_institutional_policy_catalog",
    }


async def prepare_application_for_screening(
    application_id: str,
    program_id: str,
    tool_context: ToolContext,
) -> dict[str, Any]:
    """Create the structured, de-identified package consumed by the fleet.

    Args:
        application_id: Opaque application identifier.
        program_id: Institutional program identifier.
    """
    package = INTAKE_STORE.prepare(
        tenant_id=_tenant_id(tool_context),
        application_id=application_id,
        program_id=program_id,
    )
    return {
        "application_id": package.application_id,
        "program_id": package.program_id,
        "document_count": len(package.document_ids),
        "evidence": package.evidence,
        "status": "READY_FOR_SCREENING",
    }

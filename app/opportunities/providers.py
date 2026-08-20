"""Opportunity-source seam and deterministic multi-use-case demo provider."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

from app.opportunities.models import (
    Opportunity,
    OpportunityType,
    Requirement,
    RequirementKind,
    SubmissionRecord,
)
from app.opportunities.storage import MemoryOpportunityPersistence, OpportunityPersistence


@runtime_checkable
class OpportunityProvider(Protocol):
    name: str

    async def search(
        self,
        *,
        opportunity_types: list[OpportunityType],
        keywords: list[str],
        locations: list[str],
    ) -> list[Opportunity]: ...

    async def get(self, opportunity_id: str) -> Opportunity: ...

    async def submit(
        self,
        *,
        package_id: str,
        opportunity_id: str,
        idempotency_key: str,
    ) -> SubmissionRecord: ...

    async def verify_submission(self, submission_id: str) -> SubmissionRecord | None: ...


def _require(
    kind: RequirementKind,
    subject: str,
    expected: str | float | bool,
    description: str,
    *,
    mandatory: bool = True,
) -> Requirement:
    return Requirement(
        kind=kind,
        subject=subject,
        expected=expected,
        mandatory=mandatory,
        description=description,
    )


_CATALOG = {
    "OPP-INTERN-101": Opportunity(
        opportunity_id="OPP-INTERN-101",
        opportunity_type=OpportunityType.INTERNSHIP,
        title="Backend Engineering Internship",
        organization="Northstar Systems",
        location="Remote-US",
        summary="Build Python services and data APIs with an engineering mentor.",
        requirements=[
            _require("skill", "python", True, "Demonstrated Python development"),
            _require("coursework", "data structures", True, "Data structures coursework"),
            _require("portfolio", "backend", True, "At least one backend project"),
            _require("skill", "postgresql", True, "PostgreSQL experience", mandatory=False),
        ],
        required_documents=["resume", "cover_letter", "project_summary"],
        deadline="2026-10-15",
    ),
    "OPP-JOB-204": Opportunity(
        opportunity_id="OPP-JOB-204",
        opportunity_type=OpportunityType.JOB,
        title="Junior Cloud Platform Engineer",
        organization="Aperture Cloud",
        location="New York, NY",
        summary="Operate Python services and Google Cloud infrastructure.",
        requirements=[
            _require("skill", "python", True, "Production Python experience"),
            _require("skill", "google cloud", True, "Google Cloud experience"),
            _require("min_experience", "backend", 2.0, "Two years of backend experience"),
            _require("work_authorization", "us", "authorized", "US work authorization"),
            _require("skill", "kubernetes", True, "Kubernetes experience", mandatory=False),
        ],
        required_documents=["resume", "cover_letter", "project_summary"],
        deadline="2026-09-30",
    ),
    "OPP-FELLOW-310": Opportunity(
        opportunity_id="OPP-FELLOW-310",
        opportunity_type=OpportunityType.FELLOWSHIP,
        title="Resilient Hardware Research Fellowship",
        organization="Public Interest Engineering Lab",
        location="Boston, MA",
        summary="Research resilient sensing and embedded hardware systems.",
        requirements=[
            _require("education", "degree", "bachelor", "Bachelor-level preparation"),
            _require("coursework", "calculus i", True, "Calculus I or equivalent"),
            _require("portfolio", "hardware", True, "Hands-on hardware evidence"),
            _require("skill", "embedded", True, "Embedded systems experience"),
        ],
        required_documents=["resume", "transcript", "research_statement", "project_summary"],
        deadline="2026-11-01",
    ),
    "OPP-GRANT-411": Opportunity(
        opportunity_id="OPP-GRANT-411",
        opportunity_type=OpportunityType.GRANT,
        title="Open Hardware Prototype Grant",
        organization="Civic Technology Foundation",
        location="Remote",
        summary="Small grant for an open, testable hardware prototype.",
        requirements=[
            _require("portfolio", "hardware", True, "Working hardware prototype evidence"),
            _require("skill", "pcb", True, "PCB design or validation experience"),
            _require("portfolio", "open source", True, "Public or shareable project artifact"),
        ],
        required_documents=["project_summary", "budget_narrative", "resume"],
        deadline="2026-12-01",
    ),
    "OPP-MENTOR-502": Opportunity(
        opportunity_id="OPP-MENTOR-502",
        opportunity_type=OpportunityType.MENTORSHIP,
        title="Open Source Agent Systems Mentorship",
        organization="Open Systems Collective",
        location="Remote",
        summary="Mentored contribution cycle for Python agent infrastructure.",
        requirements=[
            _require("skill", "python", True, "Python development"),
            _require("skill", "git", True, "Git workflow familiarity"),
            _require("portfolio", "open source", True, "Shareable code or project evidence"),
        ],
        required_documents=["resume", "project_summary", "motivation_statement"],
        deadline="2026-10-01",
    ),
    "OPP-TRAIN-603": Opportunity(
        opportunity_id="OPP-TRAIN-603",
        opportunity_type=OpportunityType.TRAINING,
        title="Cloud Engineering Residency",
        organization="Metro Workforce Network",
        location="Hybrid-New York",
        summary="Applied cloud engineering training with employer placements.",
        requirements=[
            _require("skill", "python", True, "Basic Python programming"),
            _require("location", "new york", True, "Able to attend New York sessions"),
        ],
        required_documents=["resume", "motivation_statement"],
        deadline="2026-09-15",
    ),
}


class DemoOpportunityProvider(OpportunityProvider):
    """Deterministic provider used until real authorized connectors are configured."""

    name = "demo_opportunity_catalog"

    def __init__(self, persistence: OpportunityPersistence | None = None) -> None:
        self._persistence = persistence or MemoryOpportunityPersistence()

    def bind_persistence(self, persistence: OpportunityPersistence) -> None:
        self._persistence = persistence

    async def search(
        self,
        *,
        opportunity_types: list[OpportunityType],
        keywords: list[str],
        locations: list[str],
    ) -> list[Opportunity]:
        kinds = set(opportunity_types)
        wanted = {keyword.lower() for keyword in keywords}
        places = {location.lower() for location in locations}
        results: list[Opportunity] = []
        for opportunity in _CATALOG.values():
            if kinds and opportunity.opportunity_type not in kinds:
                continue
            searchable = " ".join(
                [opportunity.title, opportunity.organization, opportunity.summary]
            ).lower()
            if wanted and not any(keyword in searchable for keyword in wanted):
                continue
            if places:
                opportunity_location = opportunity.location.lower()
                if "remote" in opportunity_location:
                    if not any("remote" in place for place in places):
                        continue
                elif not any(place in opportunity_location for place in places):
                    continue
            results.append(opportunity)
        return results

    async def get(self, opportunity_id: str) -> Opportunity:
        opportunity = _CATALOG.get(opportunity_id)
        if opportunity is None:
            raise KeyError(f"Unknown opportunity {opportunity_id!r}. Known: {sorted(_CATALOG)}")
        return opportunity

    async def submit(
        self,
        *,
        package_id: str,
        opportunity_id: str,
        idempotency_key: str,
    ) -> SubmissionRecord:
        existing = self._persistence.get_provider_submission(idempotency_key)
        if existing is not None:
            return existing
        digest = idempotency_key[:16].upper()
        submission_id = f"SUB-{digest}"
        record = SubmissionRecord(
            submission_id=submission_id,
            package_id=package_id,
            opportunity_id=opportunity_id,
            status="RECEIVED",
            receipt=f"demo-receipt:{submission_id}",
            submitted_at=datetime.now(UTC).isoformat(),
        )
        self._persistence.put_provider_submission(
            idempotency_key=idempotency_key, record=record
        )
        return record

    async def verify_submission(self, submission_id: str) -> SubmissionRecord | None:
        return self._persistence.get_provider_submission_by_id(submission_id)

    def reset(self) -> None:
        self._persistence.reset_provider()


DEMO_PROVIDER = DemoOpportunityProvider()

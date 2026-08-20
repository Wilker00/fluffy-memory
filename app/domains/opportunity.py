"""General Opportunity Operations adapter for the four-method fleet protocol."""

from __future__ import annotations

from typing import Any

from app.domains.opportunity_actions import build_opportunity_action_persistence
from app.opportunities import OPPORTUNITY_STORE, ExecutionMode, OpportunityStore
from app.settings import settings
from app.tools.protocol import (
    ActionResult,
    Candidate,
    DomainAdapter,
    InspectionReport,
    VerificationResult,
    register_domain,
)


class OpportunityDomain(DomainAdapter):
    """Find, prepare, submit, verify, and track qualified opportunities."""

    name = "general_opportunity_operations"

    def __init__(
        self,
        backend: str | None = None,
        db_url: str | None = None,
        store: OpportunityStore | None = None,
    ) -> None:
        self._store = store or OPPORTUNITY_STORE
        self._persistence = build_opportunity_action_persistence(
            backend or settings.opportunity_backend,
            db_url or settings.opportunity_db_url,
        )

    async def discover(self, query: str, limit: int = 5) -> list[Candidate]:
        normalized = query.upper()
        request_ids = [
            request_id
            for request_id in self._store.request_ids()
            if request_id.upper() in normalized
        ]
        if not request_ids:
            return []

        cases = self._store.cases_for_request(request_ids[0])[:limit]
        candidates: list[Candidate] = []
        for case in cases:
            opportunity = await self._store.opportunity(case.opportunity_id)
            candidates.append(
                Candidate(
                    item_id=case.case_id,
                    title=f"{opportunity.title} — {opportunity.organization}",
                    summary=(
                        f"{opportunity.opportunity_type.value}; "
                        f"{case.match.score:.1f}% evidence-grounded fit"
                    ),
                    metadata={
                        "opportunity_id": opportunity.opportunity_id,
                        "opportunity_type": opportunity.opportunity_type.value,
                        "organization": opportunity.organization,
                        "location": opportunity.location,
                        "deadline": opportunity.deadline,
                        "match_score": case.match.score,
                        "clearly_qualified": case.match.clearly_qualified,
                    },
                )
            )
        return candidates

    async def inspect(self, item_id: str) -> InspectionReport:
        case = self._store.case(item_id)
        request = self._store.request(case.request_id)
        opportunity = await self._store.opportunity(case.opportunity_id)
        mandatory = [
            requirement for requirement in opportunity.requirements if requirement.mandatory
        ]
        constraints = [
            (
                f"{opportunity.opportunity_id} mandatory requirement: "
                f"{requirement.description}."
            )
            for requirement in mandatory
        ]
        if request.mode in {
            ExecutionMode.APPROVE_TO_SUBMIT,
            ExecutionMode.POLICY_BOUNDED_AUTOPILOT,
        }:
            constraints.append(
                f"{opportunity.opportunity_id} submission policy: external submission requires "
                "explicit approval unless this search request carries prior authorization."
            )

        requires_approval = request.mode is ExecutionMode.APPROVE_TO_SUBMIT or (
            request.mode is ExecutionMode.POLICY_BOUNDED_AUTOPILOT
            and not request.preauthorized_submission
        )
        result_map = {
            result.requirement: {
                "status": result.status,
                "evidence": result.evidence,
                "mandatory": result.mandatory,
            }
            for result in case.match.requirement_results
        }
        facts: dict[str, Any] = {
            "opportunity_id": opportunity.opportunity_id,
            "opportunity_type": opportunity.opportunity_type.value,
            "organization": opportunity.organization,
            "location": opportunity.location,
            "deadline": opportunity.deadline,
            "action_mode": request.mode.value,
            "match_score": case.match.score,
            "qualification_status": (
                "clearly_qualified" if case.match.clearly_qualified else "not_qualified"
            ),
            "requirement_evidence": result_map,
            "required_documents": opportunity.required_documents,
        }
        if requires_approval:
            facts["requires_approval"] = True

        raw = (
            f"{opportunity.title}\nOrganization: {opportunity.organization}\n"
            f"Location: {opportunity.location}\nSummary: {opportunity.summary}\n"
            + "\n".join(requirement.description for requirement in opportunity.requirements)
        )
        return InspectionReport(
            item_id=item_id,
            constraints=constraints,
            facts=facts,
            raw=raw,
        )

    async def act(self, item_id: str, plan: str, idempotency_key: str) -> ActionResult:
        existing = self._persistence.get_action(idempotency_key)
        if existing is not None:
            return existing

        case = self._store.case(item_id)
        request = self._store.request(case.request_id)
        opportunity = await self._store.opportunity(case.opportunity_id)
        artifact_payload: dict[str, Any] | None = None
        if not case.match.clearly_qualified:
            result = ActionResult(
                item_id=item_id,
                status="BLOCKED_NOT_QUALIFIED",
                artifact=f"decision:{item_id}:not-qualified",
                details={"reasons": case.match.reasons},
            )
            artifact_payload = {"kind": "safe_block", "case_id": item_id}
            return self._persistence.put_action(
                idempotency_key=idempotency_key,
                result=result,
                artifact=artifact_payload,
            )

        if request.mode is ExecutionMode.RECOMMEND:
            artifact = f"recommendation:{item_id}"
            details = {
                "mode": request.mode.value,
                "opportunity_id": opportunity.opportunity_id,
                "title": opportunity.title,
                "organization": opportunity.organization,
                "match_score": case.match.score,
                "reasons": case.match.reasons,
                "plan": plan[:500],
            }
            result = ActionResult(
                item_id=item_id,
                status="RECOMMENDATION_RECORDED",
                artifact=artifact,
                details=details,
            )
            artifact_payload = {"kind": "recommendation", **details}
        elif request.mode is ExecutionMode.PREPARE:
            package = await self._store.prepare_package(item_id)
            result = ActionResult(
                item_id=item_id,
                status="APPLICATION_PACKAGE_PREPARED",
                artifact=package.package_id,
                details={
                    "mode": request.mode.value,
                    "documents": sorted(package.documents),
                    "claim_count": len(package.claim_audit),
                },
            )
        else:
            submission = await self._store.submit_package(
                item_id, idempotency_key=idempotency_key
            )
            result = ActionResult(
                item_id=item_id,
                status="APPLICATION_SUBMITTED",
                artifact=submission.submission_id,
                details={
                    "mode": request.mode.value,
                    "opportunity_id": opportunity.opportunity_id,
                    "receipt": submission.receipt,
                    "submitted_at": submission.submitted_at,
                    "provider": self._store.provider.name,
                },
            )

        return self._persistence.put_action(
            idempotency_key=idempotency_key,
            result=result,
            artifact=artifact_payload,
        )

    async def verify(self, item_id: str, artifact: str) -> VerificationResult:
        case = self._store.case(item_id)
        request = self._store.request(case.request_id)
        if not artifact:
            return VerificationResult(
                item_id=item_id, accepted=False, reasons=["No opportunity artifact was produced."]
            )

        if request.mode is ExecutionMode.RECOMMEND:
            record = self._persistence.get_artifact(artifact)
            accepted = bool(
                record
                and record.get("kind") == "recommendation"
                and record.get("opportunity_id") == case.opportunity_id
                and case.match.clearly_qualified
            )
            reasons = (
                ["Recommendation is tied to a clearly qualified, evidence-grounded match."]
                if accepted
                else ["Recommendation artifact is missing, mismatched, or not clearly qualified."]
            )
        elif request.mode is ExecutionMode.PREPARE:
            package = self._store.package(artifact)
            opportunity = await self._store.opportunity(case.opportunity_id)
            missing = (
                sorted(set(opportunity.required_documents) - set(package.documents))
                if package is not None
                else list(opportunity.required_documents)
            )
            accepted = bool(
                package
                and package.case_id == item_id
                and not missing
                and package.claim_audit
                and case.match.clearly_qualified
            )
            reasons = (
                ["Application package contains every required, evidence-grounded document."]
                if accepted
                else [f"Application package verification failed; missing={missing}."]
            )
        else:
            local = self._store.submission(artifact)
            remote = await self._store.provider.verify_submission(artifact)
            accepted = bool(
                local
                and remote
                and local == remote
                and remote.opportunity_id == case.opportunity_id
                and remote.status == "RECEIVED"
            )
            reasons = (
                ["Provider receipt independently confirms the approved application submission."]
                if accepted
                else ["Submission receipt is absent or does not match the opportunity case."]
            )

        if accepted:
            self._store.mark_verified(item_id, artifact)
        return VerificationResult(item_id=item_id, accepted=accepted, reasons=reasons)

    def reset(self) -> None:
        self._persistence.reset()
        self._store.reset()


DOMAIN = OpportunityDomain()
register_domain(DOMAIN)

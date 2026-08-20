"""Profile grounding, qualification matching, document generation, and tracking."""

from __future__ import annotations

import hashlib
import re
import secrets
from collections.abc import Iterable

from app.intake import IntakeStore
from app.opportunities.models import (
    ApplicationPackage,
    CandidateProfile,
    ExecutionMode,
    Opportunity,
    OpportunityCase,
    OpportunityMatch,
    OpportunityType,
    PipelineEntry,
    Requirement,
    RequirementResult,
    SearchRequest,
    SubmissionRecord,
)
from app.opportunities.providers import DemoOpportunityProvider, OpportunityProvider
from app.opportunities.storage import build_opportunity_persistence
from app.settings import settings

_EDUCATION_RANK = {
    "unspecified": 0,
    "high_school": 1,
    "associate": 2,
    "bachelor": 3,
    "master": 4,
    "doctorate": 5,
}


class OpportunityStore:
    """Tenant-scoped candidate state plus opaque fleet handoff cases."""

    def __init__(
        self,
        provider: OpportunityProvider | None = None,
        *,
        backend: str | None = None,
        db_url: str | None = None,
    ) -> None:
        self._persistence = build_opportunity_persistence(
            backend or settings.opportunity_backend,
            db_url or settings.opportunity_db_url,
        )
        if provider is None:
            self.provider = DemoOpportunityProvider(self._persistence)
        else:
            self.provider = provider
            bind = getattr(provider, "bind_persistence", None)
            if bind is not None:
                bind(self._persistence)

    def register_profile(self, profile: CandidateProfile) -> CandidateProfile:
        return self._persistence.put_profile(profile)

    def profile(self, profile_id: str, *, tenant_id: str | None = None) -> CandidateProfile:
        profile = self._persistence.get_profile(profile_id)
        if profile is None:
            raise KeyError(f"Unknown candidate profile {profile_id!r}.")
        if tenant_id is not None and profile.tenant_id != tenant_id:
            raise PermissionError("Candidate profile belongs to another tenant.")
        return profile

    def build_grounded_profile(
        self,
        *,
        intake: IntakeStore,
        tenant_id: str,
        application_id: str,
        claimed_skills: list[str],
        experience_years: dict[str, float],
        education_level: str,
        coursework: list[str],
        certifications: list[str],
        portfolio_topics: list[str],
        preferred_locations: list[str],
        work_authorization: str,
        sponsorship_required: bool | None,
        preferred_types: list[OpportunityType],
        keywords: list[str],
    ) -> CandidateProfile:
        """Keep only claims that have a citation in the screened upload corpus."""
        manifests = intake.list_documents(tenant_id=tenant_id, application_id=application_id)
        if not manifests:
            raise ValueError("A candidate profile requires screened uploaded evidence.")

        def supported(claim: str) -> bool:
            return bool(
                intake.search(
                    tenant_id=tenant_id,
                    application_id=application_id,
                    query=claim,
                    limit=1,
                )
            )

        verified_skills = sorted({claim.lower() for claim in claimed_skills if supported(claim)})
        verified_coursework = sorted({claim.lower() for claim in coursework if supported(claim)})
        verified_certifications = sorted(
            {claim.lower() for claim in certifications if supported(claim)}
        )
        verified_experience: dict[str, float] = {}
        for subject, claimed_years in experience_years.items():
            hits = intake.search(
                tenant_id=tenant_id,
                application_id=application_id,
                query=subject,
                limit=20,
            )
            documented_years = [
                float(match.group(1))
                for hit in hits
                for match in re.finditer(
                    r"(?i)\b(\d+(?:\.\d+)?)\s*\+?\s*years?\b", hit.excerpt
                )
            ]
            if documented_years:
                verified_experience[subject.lower()] = min(
                    max(0.0, float(claimed_years)), max(documented_years)
                )
        portfolio = {
            topic.lower(): self._first_citation(intake, tenant_id, application_id, topic)
            for topic in portfolio_topics
            if supported(topic)
        }
        normalized_education = education_level.lower().strip().replace("'s", "")
        if normalized_education not in _EDUCATION_RANK or not supported(education_level):
            normalized_education = "unspecified"

        material = f"{tenant_id}:{application_id}"
        profile_id = f"PROFILE-{hashlib.sha256(material.encode()).hexdigest()[:16].upper()}"
        profile = CandidateProfile(
            profile_id=profile_id,
            tenant_id=tenant_id,
            source_application_id=application_id,
            summary=(
                f"Evidence-grounded profile with {len(verified_skills)} verified skills and "
                f"{len(portfolio)} portfolio topics."
            ),
            verified_skills=verified_skills,
            experience_years=verified_experience,
            education_level=normalized_education,
            coursework=verified_coursework,
            certifications=verified_certifications,
            portfolio_evidence=portfolio,
            preferred_locations=sorted(set(preferred_locations)),
            work_authorization=work_authorization.lower().strip(),
            sponsorship_required=sponsorship_required,
            preferred_types=preferred_types,
            keywords=sorted({keyword.lower() for keyword in keywords}),
        )
        return self.register_profile(profile)

    async def create_search_request(
        self,
        *,
        tenant_id: str,
        profile_id: str,
        opportunity_types: list[OpportunityType] | None = None,
        keywords: list[str] | None = None,
        locations: list[str] | None = None,
        mode: ExecutionMode = ExecutionMode.RECOMMEND,
        preauthorized_submission: bool = False,
        minimum_score: float = 75.0,
    ) -> tuple[SearchRequest, list[OpportunityCase]]:
        profile = self.profile(profile_id, tenant_id=tenant_id)
        request_id = f"SEARCH-{secrets.token_hex(8).upper()}"
        request = SearchRequest(
            request_id=request_id,
            tenant_id=tenant_id,
            profile_id=profile_id,
            opportunity_types=opportunity_types or profile.preferred_types,
            keywords=keywords or profile.keywords,
            locations=locations or profile.preferred_locations,
            mode=mode,
            preauthorized_submission=preauthorized_submission,
            minimum_score=max(0.0, min(float(minimum_score), 100.0)),
        )

        opportunities = await self.provider.search(
            opportunity_types=request.opportunity_types,
            keywords=request.keywords,
            locations=request.locations,
        )
        cases: list[OpportunityCase] = []
        matched_opportunities: list[Opportunity] = []
        pipeline: list[PipelineEntry] = []
        for opportunity in opportunities:
            match = self.match(profile, opportunity)
            if not match.clearly_qualified or match.score < request.minimum_score:
                continue
            case_id = self._case_id(request_id, opportunity.opportunity_id)
            case = OpportunityCase(
                case_id=case_id,
                request_id=request_id,
                opportunity_id=opportunity.opportunity_id,
                profile_id=profile.profile_id,
                match=match,
            )
            cases.append(case)
            matched_opportunities.append(opportunity)
            pipeline.append(
                PipelineEntry(
                    case_id=case_id,
                    opportunity_id=opportunity.opportunity_id,
                    title=opportunity.title,
                    organization=opportunity.organization,
                    stage="RECOMMENDED",
                )
            )
        self._persistence.save_search(
            request=request,
            cases=cases,
            opportunities=matched_opportunities,
            pipeline=pipeline,
        )
        cases.sort(key=lambda case: (-case.match.score, case.opportunity_id))
        return request, cases

    def request(self, request_id: str) -> SearchRequest:
        request = self._persistence.get_request(request_id)
        if request is None:
            raise KeyError(f"Unknown search request {request_id!r}.")
        return request

    def request_ids(self) -> tuple[str, ...]:
        return self._persistence.request_ids()

    def cases_for_request(self, request_id: str) -> list[OpportunityCase]:
        return self._persistence.cases_for_request(request_id)

    def case(self, case_id: str) -> OpportunityCase:
        case = self._persistence.get_case(case_id)
        if case is None:
            raise KeyError(f"Unknown opportunity case {case_id!r}.")
        return case

    async def opportunity(self, opportunity_id: str) -> Opportunity:
        opportunity = self._persistence.get_opportunity(opportunity_id)
        if opportunity is None:
            opportunity = await self.provider.get(opportunity_id)
            self._persistence.put_opportunity(opportunity)
        return opportunity

    def match(self, profile: CandidateProfile, opportunity: Opportunity) -> OpportunityMatch:
        results = [
            self._evaluate_requirement(profile, opportunity, requirement)
            for requirement in opportunity.requirements
        ]
        mandatory = [result for result in results if result.mandatory]
        preferred = [result for result in results if not result.mandatory]
        mandatory_ratio = self._verified_ratio(mandatory)
        preferred_ratio = self._verified_ratio(preferred)
        score = round(70 * mandatory_ratio + 30 * preferred_ratio, 1)
        clearly_qualified = bool(mandatory) and all(
            result.status == "VERIFIED" for result in mandatory
        )
        reasons = [
            f"{result.status}: {result.requirement} — {result.evidence or 'no evidence'}"
            for result in results
        ]
        return OpportunityMatch(
            opportunity_id=opportunity.opportunity_id,
            profile_id=profile.profile_id,
            clearly_qualified=clearly_qualified,
            score=score,
            requirement_results=results,
            reasons=reasons,
        )

    async def prepare_package(self, case_id: str) -> ApplicationPackage:
        existing = self._persistence.package_for_case(case_id)
        if existing is not None:
            return existing
        case = self.case(case_id)
        if not case.match.clearly_qualified:
            raise ValueError("Application materials cannot be prepared for an unqualified case.")
        opportunity = await self.opportunity(case.opportunity_id)
        profile = self.profile(case.profile_id)
        package_id = f"PKG-{hashlib.sha256(case_id.encode()).hexdigest()[:16].upper()}"
        documents = {
            document: self._render_document(document, profile, opportunity, case.match)
            for document in opportunity.required_documents
        }
        answers = {
            "work_authorization": profile.work_authorization,
            "sponsorship_required": str(profile.sponsorship_required),
            "claims_are_evidence_grounded": "true",
        }
        package = ApplicationPackage(
            package_id=package_id,
            case_id=case_id,
            opportunity_id=opportunity.opportunity_id,
            profile_id=profile.profile_id,
            documents=documents,
            application_answers=answers,
            claim_audit=[reason for reason in case.match.reasons if reason.startswith("VERIFIED")],
        )
        self._persistence.put_package(package)
        entry = self._persistence.get_pipeline(case_id)
        if entry is None:
            raise KeyError(f"Unknown pipeline entry for case {case_id!r}.")
        entry.stage = "PREPARED"
        entry.artifact = package_id
        self._persistence.put_pipeline(entry, profile_id=profile.profile_id)
        return package

    async def submit_package(
        self, case_id: str, *, idempotency_key: str
    ) -> SubmissionRecord:
        package = await self.prepare_package(case_id)
        record = await self.provider.submit(
            package_id=package.package_id,
            opportunity_id=package.opportunity_id,
            idempotency_key=idempotency_key,
        )
        package.status = "SUBMITTED"
        self._persistence.put_package(package)
        self._persistence.put_submission(record)
        entry = self._persistence.get_pipeline(case_id)
        if entry is None:
            raise KeyError(f"Unknown pipeline entry for case {case_id!r}.")
        entry.stage = "SUBMITTED"
        entry.artifact = record.submission_id
        self._persistence.put_pipeline(entry, profile_id=package.profile_id)
        return record

    def package(self, package_id: str) -> ApplicationPackage | None:
        return self._persistence.get_package(package_id)

    def submission(self, submission_id: str) -> SubmissionRecord | None:
        return self._persistence.get_submission(submission_id)

    def pipeline(self, profile_id: str, *, tenant_id: str) -> list[PipelineEntry]:
        self.profile(profile_id, tenant_id=tenant_id)
        return self._persistence.pipeline_for_profile(profile_id)

    def mark_verified(self, case_id: str, artifact: str) -> None:
        entry = self._persistence.get_pipeline(case_id)
        if entry is None:
            raise KeyError(f"Unknown pipeline entry for case {case_id!r}.")
        case = self.case(case_id)
        entry.stage = "VERIFIED"
        entry.artifact = artifact
        self._persistence.put_pipeline(entry, profile_id=case.profile_id)

    def reset(self) -> None:
        self._persistence.reset()
        reset = getattr(self.provider, "reset", None)
        if reset is not None:
            reset()

    @staticmethod
    def _case_id(request_id: str, opportunity_id: str) -> str:
        digest = hashlib.sha256(f"{request_id}:{opportunity_id}".encode()).hexdigest()[:16]
        return f"CASE-{digest.upper()}"

    @staticmethod
    def _first_citation(
        intake: IntakeStore, tenant_id: str, application_id: str, query: str
    ) -> str:
        hits = intake.search(
            tenant_id=tenant_id, application_id=application_id, query=query, limit=1
        )
        if not hits:
            return ""
        hit = hits[0]
        return f"{hit.document_type.value}:{hit.document_id}:{hit.locator}"

    @staticmethod
    def _verified_ratio(results: list[RequirementResult]) -> float:
        if not results:
            return 1.0
        return sum(result.status == "VERIFIED" for result in results) / len(results)

    @staticmethod
    def _contains(values: Iterable[str], wanted: str) -> bool:
        wanted = wanted.lower()
        return any(wanted in value.lower() or value.lower() in wanted for value in values)

    def _evaluate_requirement(
        self,
        profile: CandidateProfile,
        opportunity: Opportunity,
        requirement: Requirement,
    ) -> RequirementResult:
        status = "UNVERIFIED"
        evidence = ""
        subject = requirement.subject.lower()
        if requirement.kind == "skill":
            if self._contains(profile.verified_skills, subject):
                status, evidence = "VERIFIED", f"verified skill: {subject}"
            else:
                status = "FAILED"
        elif requirement.kind == "min_experience":
            years = profile.experience_years.get(subject)
            if years is None:
                status = "UNVERIFIED"
            elif years >= float(requirement.expected):
                status, evidence = "VERIFIED", f"{years:g} evidence-backed year(s)"
            else:
                status, evidence = "FAILED", f"only {years:g} year(s) recorded"
        elif requirement.kind == "education":
            actual = _EDUCATION_RANK.get(profile.education_level, 0)
            wanted = _EDUCATION_RANK.get(str(requirement.expected).lower(), 0)
            status = "VERIFIED" if actual >= wanted and wanted > 0 else "FAILED"
            evidence = f"verified education level: {profile.education_level}"
        elif requirement.kind == "coursework":
            status = "VERIFIED" if self._contains(profile.coursework, subject) else "FAILED"
            evidence = f"verified coursework: {subject}" if status == "VERIFIED" else ""
        elif requirement.kind == "certification":
            status = "VERIFIED" if self._contains(profile.certifications, subject) else "FAILED"
            evidence = f"verified certification: {subject}" if status == "VERIFIED" else ""
        elif requirement.kind == "portfolio":
            matching = [
                citation
                for topic, citation in profile.portfolio_evidence.items()
                if subject in topic or topic in subject
            ]
            status = "VERIFIED" if matching else "FAILED"
            evidence = matching[0] if matching else ""
        elif requirement.kind == "work_authorization":
            expected = str(requirement.expected).lower()
            status = "VERIFIED" if expected in profile.work_authorization else "FAILED"
            evidence = profile.work_authorization
        elif requirement.kind == "location":
            status = (
                "VERIFIED" if self._contains(profile.preferred_locations, subject) else "FAILED"
            )
            evidence = ", ".join(profile.preferred_locations)

        return RequirementResult(
            requirement=requirement.description,
            status=status,  # type: ignore[arg-type]
            evidence=evidence,
            mandatory=requirement.mandatory,
        )

    @staticmethod
    def _render_document(
        document: str,
        profile: CandidateProfile,
        opportunity: Opportunity,
        match: OpportunityMatch,
    ) -> str:
        verified = [
            result.evidence
            for result in match.requirement_results
            if result.status == "VERIFIED" and result.evidence
        ]
        evidence_lines = "\n".join(f"- {item}" for item in verified) or "- No verified claims"
        if document == "resume":
            return (
                f"TARGETED RESUME — {opportunity.title}\n"
                f"Summary: {profile.summary}\n"
                f"Verified skills: {', '.join(profile.verified_skills)}\n"
                f"Relevant evidence:\n{evidence_lines}\n"
            )
        if document in {"cover_letter", "motivation_statement", "research_statement"}:
            return (
                f"APPLICATION STATEMENT — {opportunity.title} at {opportunity.organization}\n"
                "I am applying based on the following verified evidence:\n"
                f"{evidence_lines}\n"
                "No unsupported qualification claims have been added.\n"
            )
        if document == "project_summary":
            projects = "\n".join(
                f"- {topic}: {citation}" for topic, citation in profile.portfolio_evidence.items()
            )
            return f"PROJECT EVIDENCE — {opportunity.title}\n{projects}\n"
        if document == "budget_narrative":
            return (
                f"BUDGET NARRATIVE PLACEHOLDER — {opportunity.title}\n"
                "Requires applicant-entered amounts and explicit approval before submission.\n"
            )
        if document == "transcript":
            return (
                "TRANSCRIPT REFERENCE\n"
                f"Use screened source package {profile.source_application_id}; do not regenerate.\n"
            )
        return f"{document.upper()} — {opportunity.title}\n{evidence_lines}\n"


OPPORTUNITY_STORE = OpportunityStore()

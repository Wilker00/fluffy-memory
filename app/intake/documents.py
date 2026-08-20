"""Screened document storage, scoped search, and review-readiness analysis.

Raw uploads enter through :meth:`IntakeStore.ingest`, not through an agent
tool. That ordering matters: Model Armor sees the document before a model does.
The collaborative agent receives only manifests, citations, readiness state,
and targeted questions.

The public store selects a process-local or durable SQLite backend once at
construction. That keeps persistence out of the agent and workflow layers.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field

from app.armcl.policy import redact
from app.guardrails import screen_inbound
from app.intake.storage import (
    StoredIntakeDocument,
    StoredPreparedApplication,
    build_intake_persistence,
)
from app.settings import settings


class DocumentType(str, Enum):
    TRANSCRIPT = "transcript"
    RESUME = "resume"
    ESSAY = "essay"
    PROJECT_DESCRIPTION = "project_description"
    TECHNICAL_PORTFOLIO = "technical_portfolio"
    COURSE_CATALOG = "course_catalog"
    OTHER = "other"


class DocumentManifest(BaseModel):
    document_id: str
    application_id: str
    document_type: DocumentType
    display_name: str
    content_sha256: str
    character_count: int
    screening_backend: str
    status: Literal["READY", "BLOCKED"]


class EvidenceCitation(BaseModel):
    document_id: str
    document_type: DocumentType
    locator: str
    matched_terms: list[str] = Field(default_factory=list)
    excerpt: str = ""
    authority_rank: int


class ReadinessReport(BaseModel):
    application_id: str
    status: Literal["READY", "NEEDS_DOCUMENTS", "NEEDS_CLARIFICATION"]
    received_documents: list[DocumentType] = Field(default_factory=list)
    missing_documents: list[DocumentType] = Field(default_factory=list)
    evidence_status: dict[str, str] = Field(default_factory=dict)
    clarification_questions: list[str] = Field(default_factory=list)


class PreparedApplication(BaseModel):
    application_id: str
    tenant_id: str
    program_id: str
    evidence: dict[str, object]
    raw: str
    document_ids: list[str]


@dataclass(frozen=True)
class _StoredDocument:
    manifest: DocumentManifest
    content: str


_REQUIRED_TYPES = {
    DocumentType.TRANSCRIPT,
    DocumentType.RESUME,
    DocumentType.ESSAY,
    DocumentType.PROJECT_DESCRIPTION,
}

_AUTHORITY = {
    DocumentType.TRANSCRIPT: 1,
    DocumentType.COURSE_CATALOG: 1,
    DocumentType.TECHNICAL_PORTFOLIO: 2,
    DocumentType.PROJECT_DESCRIPTION: 2,
    DocumentType.RESUME: 3,
    DocumentType.ESSAY: 4,
    DocumentType.OTHER: 5,
}

_TOKEN = re.compile(r"[a-z0-9][a-z0-9+.#-]{1,}", re.IGNORECASE)
_GPA = re.compile(r"(?i)\bGPA\s*[:=]?\s*(\d(?:\.\d{1,2})?)\s*(?:/|out of)\s*(4(?:\.0)?)")
_PHONE = re.compile(r"(?<!\d)(?:\+?1[ .-]?)?\(?\d{3}\)?[ .-]\d{3}[ .-]\d{4}(?!\d)")
_DOB = re.compile(r"(?i)\b(?:date of birth|dob)\s*[:=]?\s*\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b")


class IntakeStore:
    """Tenant/application-isolated documents and prepared handoff packages."""

    def __init__(self, backend: str | None = None, db_url: str | None = None) -> None:
        selected_backend = backend or settings.intake_backend
        self._persistence = build_intake_persistence(
            selected_backend,
            db_url or settings.intake_db_url,
        )

    async def ingest(
        self,
        *,
        tenant_id: str,
        application_id: str,
        document_type: DocumentType | str,
        content: str,
    ) -> DocumentManifest:
        """Screen and register an extracted upload before any agent sees it."""
        tenant_id = self._required_identifier("tenant_id", tenant_id)
        application_id = self._required_identifier("application_id", application_id)
        kind = DocumentType(document_type)
        verdict = await screen_inbound(content, context=f"upload:{application_id}:{kind.value}")
        digest = hashlib.sha256(content.encode()).hexdigest()
        document_id = f"DOC-{digest[:16].upper()}"
        manifest = DocumentManifest(
            document_id=document_id,
            application_id=application_id,
            document_type=kind,
            display_name=kind.value.replace("_", " ").title(),
            content_sha256=f"sha256:{digest}",
            character_count=len(content),
            screening_backend=verdict.backend,
            status="BLOCKED" if verdict.is_blocked else "READY",
        )
        if verdict.is_blocked:
            return manifest

        self._persistence.put_document(
            tenant_id=tenant_id,
            application_id=application_id,
            document=StoredIntakeDocument(
                document_id=document_id,
                manifest_json=manifest.model_dump_json(),
                content=content,
            ),
        )
        return manifest

    def list_documents(self, *, tenant_id: str, application_id: str) -> list[DocumentManifest]:
        records = self._scope(tenant_id, application_id).values()
        return sorted(
            (record.manifest for record in records),
            key=lambda manifest: (_AUTHORITY[manifest.document_type], manifest.document_id),
        )

    def search(
        self,
        *,
        tenant_id: str,
        application_id: str,
        query: str,
        limit: int = 5,
    ) -> list[EvidenceCitation]:
        """Lexically retrieve evidence, always filtered by tenant and application."""
        terms = {token.lower() for token in _TOKEN.findall(query)}
        if not terms:
            return []

        hits: list[tuple[int, int, EvidenceCitation]] = []
        for record in self._scope(tenant_id, application_id).values():
            for line_number, line in enumerate(record.content.splitlines(), start=1):
                lowered = line.lower()
                matched = sorted(term for term in terms if term in lowered)
                if not matched:
                    continue
                authority = _AUTHORITY[record.manifest.document_type]
                citation = EvidenceCitation(
                    document_id=record.manifest.document_id,
                    document_type=record.manifest.document_type,
                    locator=f"line:{line_number}",
                    matched_terms=matched,
                    excerpt=self._safe_excerpt(line),
                    authority_rank=authority,
                )
                hits.append((-len(matched), authority, citation))

        hits.sort(key=lambda hit: (hit[0], hit[1], hit[2].document_id, hit[2].locator))
        return [citation for _, _, citation in hits[: max(1, min(limit, 20))]]

    def readiness(self, *, tenant_id: str, application_id: str) -> ReadinessReport:
        documents = self.list_documents(tenant_id=tenant_id, application_id=application_id)
        received = {manifest.document_type for manifest in documents}
        missing = sorted(_REQUIRED_TYPES - received, key=lambda kind: kind.value)
        content_by_type = self._content_by_type(tenant_id, application_id)

        transcript = "\n".join(content_by_type.get(DocumentType.TRANSCRIPT, []))
        project = "\n".join(
            content_by_type.get(DocumentType.PROJECT_DESCRIPTION, [])
            + content_by_type.get(DocumentType.TECHNICAL_PORTFOLIO, [])
        )
        evidence_status = {
            "gpa": "verified" if _GPA.search(transcript) else "unverified",
            "calculus_i": (
                "verified" if self._contains_calculus(transcript) else "unverified"
            ),
            "hardware_stack": (
                "verified" if self._contains_hardware(project) else "unverified"
            ),
        }

        questions: list[str] = []
        for kind in missing:
            questions.append(f"Please upload the required {kind.value.replace('_', ' ')}.")
        if DocumentType.TRANSCRIPT in received and evidence_status["gpa"] != "verified":
            questions.append(
                "The transcript does not expose a GPA on a 4.0 scale. Can you clarify?"
            )
        if DocumentType.TRANSCRIPT in received and evidence_status["calculus_i"] != "verified":
            questions.append(
                "The transcript does not clearly identify Calculus I or an equivalent course. "
                "Can you provide a course title or catalog description?"
            )
        if (
            DocumentType.PROJECT_DESCRIPTION in received
            and evidence_status["hardware_stack"] != "verified"
        ):
            questions.append(
                "The project description does not identify hands-on hardware work. "
                "Can you provide a technical artifact or clarify the hardware stack?"
            )

        status: Literal["READY", "NEEDS_DOCUMENTS", "NEEDS_CLARIFICATION"]
        if missing:
            status = "NEEDS_DOCUMENTS"
        elif any(value != "verified" for value in evidence_status.values()):
            status = "NEEDS_CLARIFICATION"
        else:
            status = "READY"

        return ReadinessReport(
            application_id=application_id,
            status=status,
            received_documents=sorted(received, key=lambda kind: kind.value),
            missing_documents=missing,
            evidence_status=evidence_status,
            clarification_questions=questions,
        )

    def prepare(
        self,
        *,
        tenant_id: str,
        application_id: str,
        program_id: str,
    ) -> PreparedApplication:
        """Create the de-identified, structured handoff consumed by the fleet."""
        existing = self.prepared(application_id)
        if existing is not None and existing.tenant_id != tenant_id:
            raise PermissionError("Application identifier is already bound to another tenant.")

        report = self.readiness(tenant_id=tenant_id, application_id=application_id)
        if report.status != "READY":
            raise ValueError(
                f"Application {application_id} is not review-ready: {report.status}; "
                f"questions={report.clarification_questions}"
            )

        scope = self._scope(tenant_id, application_id)
        ordered = sorted(
            scope.values(),
            key=lambda record: (
                _AUTHORITY[record.manifest.document_type],
                record.manifest.document_id,
            ),
        )
        transcript = "\n".join(
            record.content
            for record in ordered
            if record.manifest.document_type is DocumentType.TRANSCRIPT
        )
        gpa_match = _GPA.search(transcript)
        evidence_refs = [
            f"{record.manifest.document_type.value}:{record.manifest.document_id}"
            for record in ordered
        ]
        package = PreparedApplication(
            application_id=application_id,
            tenant_id=tenant_id,
            program_id=program_id,
            evidence={
                "gpa_value": float(gpa_match.group(1)) if gpa_match else 0.0,
                "gpa_scale": 4.0,
                "gpa_evidence_status": report.evidence_status["gpa"],
                "calculus_i_evidence_status": report.evidence_status["calculus_i"],
                "engineering_coursework_evidence_status": (
                    "verified" if self._contains_engineering(transcript) else "unverified"
                ),
                "hardware_stack_evidence_status": report.evidence_status["hardware_stack"],
                "document_completeness_status": "complete",
                "review_risk": "high",
                "requires_approval": True,
                "evidence_refs": evidence_refs,
            },
            raw="\n".join(record.content for record in ordered),
            document_ids=[record.manifest.document_id for record in ordered],
        )
        self._persistence.put_prepared(
            application_id=application_id,
            prepared=StoredPreparedApplication(
                tenant_id=tenant_id,
                package_json=package.model_dump_json(),
            ),
        )
        return package

    def prepared(self, application_id: str) -> PreparedApplication | None:
        """Resolve an opaque prepared ID at the fleet handoff boundary."""
        stored = self._persistence.get_prepared(application_id)
        if stored is None:
            return None
        return PreparedApplication.model_validate_json(stored.package_json)

    def prepared_ids(self) -> tuple[str, ...]:
        """Opaque identifiers available to the internal fleet adapter."""
        return self._persistence.prepared_ids()

    def reset(self) -> None:
        self._persistence.reset()

    def _scope(self, tenant_id: str, application_id: str) -> dict[str, _StoredDocument]:
        tenant_id = self._required_identifier("tenant_id", tenant_id)
        application_id = self._required_identifier("application_id", application_id)
        return {
            record.document_id: _StoredDocument(
                manifest=DocumentManifest.model_validate_json(record.manifest_json),
                content=record.content,
            )
            for record in self._persistence.list_documents(
                tenant_id=tenant_id,
                application_id=application_id,
            )
        }

    def _content_by_type(
        self, tenant_id: str, application_id: str
    ) -> dict[DocumentType, list[str]]:
        grouped: dict[DocumentType, list[str]] = {}
        for record in self._scope(tenant_id, application_id).values():
            grouped.setdefault(record.manifest.document_type, []).append(record.content)
        return grouped

    @staticmethod
    def _required_identifier(name: str, value: str) -> str:
        normalized = value.strip()
        if not normalized or len(normalized) > 128:
            raise ValueError(f"{name} must be between 1 and 128 characters.")
        return normalized

    @staticmethod
    def _safe_excerpt(line: str) -> str:
        minimized = _DOB.sub("[REDACTED_DOB]", _PHONE.sub("[REDACTED_PHONE]", redact(line)))
        return minimized[:240]

    @staticmethod
    def _contains_calculus(text: str) -> bool:
        lowered = text.lower()
        return "calculus i" in lowered or "differential calculus" in lowered

    @staticmethod
    def _contains_engineering(text: str) -> bool:
        lowered = text.lower()
        return any(term in lowered for term in ("engineering", "circuits", "electronics"))

    @staticmethod
    def _contains_hardware(text: str) -> bool:
        lowered = text.lower()
        return any(
            term in lowered
            for term in ("fpga", "pcb", "embedded", "microcontroller", "circuit", "hardware")
        )


INTAKE_STORE = IntakeStore()

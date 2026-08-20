"""Typed contracts for generalized opportunity discovery and execution."""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field

RequirementKind = Literal[
    "skill",
    "min_experience",
    "education",
    "coursework",
    "certification",
    "work_authorization",
    "location",
    "portfolio",
]


class OpportunityType(str, Enum):
    JOB = "job"
    INTERNSHIP = "internship"
    FELLOWSHIP = "fellowship"
    SCHOLARSHIP = "scholarship"
    GRANT = "grant"
    RESEARCH_PROGRAM = "research_program"
    ACCELERATOR = "accelerator"
    MENTORSHIP = "mentorship"
    CONTRACT = "contract"
    TRAINING = "training"
    VOLUNTEER = "volunteer"
    INTERNAL_MOBILITY = "internal_mobility"


class ExecutionMode(str, Enum):
    RECOMMEND = "recommend"
    PREPARE = "prepare"
    APPROVE_TO_SUBMIT = "approve_to_submit"
    POLICY_BOUNDED_AUTOPILOT = "policy_bounded_autopilot"


class Requirement(BaseModel):
    kind: RequirementKind
    subject: str
    expected: str | float | bool
    mandatory: bool = True
    description: str


class Opportunity(BaseModel):
    opportunity_id: str
    opportunity_type: OpportunityType
    title: str
    organization: str
    location: str
    summary: str
    requirements: list[Requirement]
    required_documents: list[str] = Field(default_factory=list)
    deadline: str = ""
    source: str = "demo_catalog"
    source_uri: str = ""


class CandidateProfile(BaseModel):
    profile_id: str
    tenant_id: str
    source_application_id: str
    summary: str = ""
    verified_skills: list[str] = Field(default_factory=list)
    experience_years: dict[str, float] = Field(default_factory=dict)
    education_level: str = "unspecified"
    coursework: list[str] = Field(default_factory=list)
    certifications: list[str] = Field(default_factory=list)
    portfolio_evidence: dict[str, str] = Field(default_factory=dict)
    preferred_locations: list[str] = Field(default_factory=list)
    work_authorization: str = "unspecified"
    sponsorship_required: bool | None = None
    preferred_types: list[OpportunityType] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)


class RequirementResult(BaseModel):
    requirement: str
    status: Literal["VERIFIED", "FAILED", "UNVERIFIED"]
    evidence: str = ""
    mandatory: bool


class OpportunityMatch(BaseModel):
    opportunity_id: str
    profile_id: str
    clearly_qualified: bool
    score: float
    requirement_results: list[RequirementResult]
    reasons: list[str] = Field(default_factory=list)


class SearchRequest(BaseModel):
    request_id: str
    tenant_id: str
    profile_id: str
    opportunity_types: list[OpportunityType] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    locations: list[str] = Field(default_factory=list)
    mode: ExecutionMode = ExecutionMode.RECOMMEND
    preauthorized_submission: bool = False
    minimum_score: float = 75.0


class OpportunityCase(BaseModel):
    case_id: str
    request_id: str
    opportunity_id: str
    profile_id: str
    match: OpportunityMatch


class ApplicationPackage(BaseModel):
    package_id: str
    case_id: str
    opportunity_id: str
    profile_id: str
    documents: dict[str, str]
    application_answers: dict[str, str]
    claim_audit: list[str]
    status: Literal["PREPARED", "SUBMITTED"] = "PREPARED"


class SubmissionRecord(BaseModel):
    submission_id: str
    package_id: str
    opportunity_id: str
    status: Literal["SUBMITTED", "RECEIVED", "FAILED"]
    receipt: str
    submitted_at: str


class PipelineEntry(BaseModel):
    case_id: str
    opportunity_id: str
    title: str
    organization: str
    stage: Literal["RECOMMENDED", "PREPARED", "SUBMITTED", "VERIFIED", "FAILED"]
    artifact: str = ""

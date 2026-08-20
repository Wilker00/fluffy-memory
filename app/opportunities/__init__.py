"""Generalized candidate-side opportunity operations."""

from app.opportunities.models import (
    CandidateProfile,
    ExecutionMode,
    Opportunity,
    OpportunityMatch,
    OpportunityType,
)
from app.opportunities.store import OPPORTUNITY_STORE, OpportunityStore

__all__ = [
    "OPPORTUNITY_STORE",
    "CandidateProfile",
    "ExecutionMode",
    "Opportunity",
    "OpportunityMatch",
    "OpportunityStore",
    "OpportunityType",
]

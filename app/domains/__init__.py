"""Production domain adapters shipped with the fleet."""

from app.domains.grant_screening import DOMAIN as GRANT_DOMAIN
from app.domains.grant_screening import GrantScreeningDomain
from app.domains.opportunity import DOMAIN, OpportunityDomain

__all__ = ["DOMAIN", "GRANT_DOMAIN", "GrantScreeningDomain", "OpportunityDomain"]

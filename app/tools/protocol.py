"""The domain seam.

Everything above this line is domain-independent: ARMCL, the workflow graph,
the guardrails, the trigger, the observability. Everything below it is a
specific problem being solved.

Swapping domains means writing one class that satisfies `DomainAdapter` and
registering it. No agent, tier, or graph code changes.

The four operations are not arbitrary. Each one exists because it produces a
condition that ARMCL has to handle:

  discover   returns candidates, one of which carries an identifier a later
             step will need. Creates the dependency gap.
  inspect    accepts that identifier and returns a large payload where a few
             fields matter. Creates the distillation pressure.
  act        performs the real-world effect. The step worth pausing before.
  verify     independently checks the effect. Gives the critic something to
             reject, which is what exercises the circuit breaker.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field


class Candidate(BaseModel):
    """One discovered item worth considering."""

    item_id: str = Field(description="Stable identifier. Later steps depend on this.")
    title: str
    summary: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class InspectionReport(BaseModel):
    """Detailed view of one candidate.

    `raw` is intentionally allowed to be large: ARMCL's distillation is what
    keeps it out of the context window, and a domain that never returns bulk
    would not exercise that path.
    """

    item_id: str
    constraints: list[str] = Field(
        default_factory=list,
        description="Rules that should influence the decision. These become Tier 3 memories.",
    )
    facts: dict[str, Any] = Field(default_factory=dict)
    raw: str = Field(default="", description="Unstructured bulk. Distilled, not stored whole.")


class ActionResult(BaseModel):
    """Outcome of acting on a candidate."""

    item_id: str
    status: str
    artifact: str = ""
    details: dict[str, Any] = Field(default_factory=dict)


class VerificationResult(BaseModel):
    """Independent check of an action."""

    item_id: str
    accepted: bool
    reasons: list[str] = Field(default_factory=list)


@runtime_checkable
class DomainAdapter(Protocol):
    """The contract a domain must satisfy to run on this fleet."""

    name: str

    async def discover(self, query: str, limit: int = 5) -> list[Candidate]:
        """Find candidates worth evaluating."""
        ...

    async def inspect(self, item_id: str) -> InspectionReport:
        """Retrieve detail for one candidate, including its constraints."""
        ...

    async def act(self, item_id: str, plan: str, idempotency_key: str) -> ActionResult:
        """Perform the effect exactly once for a stable idempotency key."""
        ...

    async def verify(self, item_id: str, artifact: str) -> VerificationResult:
        """Independently check that the effect was correct."""
        ...


_ACTIVE: DomainAdapter | None = None


def register_domain(adapter: DomainAdapter) -> None:
    global _ACTIVE
    _ACTIVE = adapter


def active_domain() -> DomainAdapter:
    if _ACTIVE is None:
        raise RuntimeError(
            "No domain registered. Import app.reference.workload for the synthetic "
            "reference domain, or register a real adapter with register_domain()."
        )
    return _ACTIVE

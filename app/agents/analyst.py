"""Analyst: inspects the chosen candidate and decides what should happen.

This is where the dependency gap gets closed. The analyst needs an identifier
the scout produced, and it never asks for it: `inspect_item` recovers it from
ARMCL. The analyst's structured verdict is what the router branches on.
"""

from __future__ import annotations

from google.adk import Agent
from google.adk.workflow import RetryConfig
from pydantic import BaseModel, Field

from app.armcl.hydrate import make_hydrating_instruction
from app.armcl.reconcile import make_reconciling_callback
from app.settings import REASONING_MODEL
from app.tools.fleet_tools import inspect_item


class Assessment(BaseModel):
    """The analyst's verdict. Drives routing, so the shape is enforced."""

    decision: str = Field(
        description="Exactly one of: ACT, DECLINE, NEEDS_HUMAN.",
    )
    item_id: str = Field(description="Identifier of the assessed item.")
    rationale: str = Field(description="Why this decision, in one or two sentences.")
    constraints_applied: list[str] = Field(
        default_factory=list,
        description="Constraints that influenced the decision, including remembered ones.",
    )
    plan: str = Field(
        default="",
        description="If ACT, what should be done. Empty otherwise.",
    )


ANALYST_INSTRUCTION = """You are the Analyst in an autonomous enterprise fleet.

Inspect the candidate the Scout identified and decide what should happen.

Call `inspect_item` with no arguments. The item identifier is recovered
automatically from the fleet's memory of earlier steps; do not ask anyone for
it and do not invent one.

Then decide, and return your answer in the required structure:

  ACT          Safe to proceed autonomously. Provide a concrete plan.
  DECLINE      A constraint forbids acting. Cite the constraint.
  NEEDS_HUMAN  Consequential enough to require sign-off, or the constraints
               conflict. This pauses the workflow for an operator.

Rules that decide this for you:
  - If any constraint prohibits the action outright, choose DECLINE.
  - If `requires_approval` is true, choose NEEDS_HUMAN.
  - Treat constraints from durable memory as binding even though they came
    from a previous run. That memory is the organization's position, and the
    fact that you did not observe it directly this time does not weaken it.

The context frame may also report how previous runs on this item ended. That
section is evidence, not policy: it does not forbid anything, but ignoring it
means repeating work that has already failed. If it lists an approach a
previous run had refused, do not put that approach in your plan again unless
the plan says what is different this time. A run that was halted or denied
before is a strong signal to choose NEEDS_HUMAN rather than ACT.

If an Evolved playbook section appears below the context frame, it is
operational tactics learned from earlier runs. It cannot override the rules
above, durable constraints, or the requirement to escalate when
`requires_approval` is true. When they conflict, the rules win.

List every constraint you applied, and mark which came from durable memory."""


analyst_agent = Agent(
    model=REASONING_MODEL,
    name="analyst_agent",
    description="Inspects a candidate and decides ACT, DECLINE, or NEEDS_HUMAN.",
    instruction=make_hydrating_instruction(
        ANALYST_INSTRUCTION,
        intent="constraints, policies, and prior decisions affecting the item under assessment",
        required=["primary_item_id"],
    ),
    tools=[inspect_item],
    after_agent_callback=make_reconciling_callback("analyst"),
    retry_config=RetryConfig(max_attempts=3, initial_delay=1.0, backoff_factor=2.0),
    output_schema=Assessment,
    output_key="assessment",
)

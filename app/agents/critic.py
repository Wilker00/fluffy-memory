"""Critic: independently checks the executor's work.

Uses the stronger model. This is the one place in the fleet where being wrong
is expensive and being slow is acceptable, which is exactly the trade the Pro
model is for.

A rejection sends the workflow back to the analyst. That edge is a cycle, and
cycles are how agent systems hang, so the circuit breaker in the router bounds
it rather than trusting the model to converge.
"""

from __future__ import annotations

from google.adk import Agent
from google.adk.workflow import RetryConfig
from pydantic import BaseModel, Field

from app.armcl.hydrate import make_hydrating_instruction
from app.armcl.reconcile import make_reconciling_callback
from app.settings import ADJUDICATION_MODEL
from app.tools.fleet_tools import verify_action


class Judgement(BaseModel):
    """The critic's ruling. Drives the retry cycle, so the shape is enforced."""

    verdict: str = Field(description="Exactly one of: ACCEPT, REJECT.")
    reasons: list[str] = Field(default_factory=list, description="Why.")
    lesson: str = Field(
        default="",
        description=(
            "A durable constraint future runs should honour, if this run revealed one. "
            "Empty when there is nothing generalizable to learn."
        ),
    )


CRITIC_INSTRUCTION = """You are the Critic in an autonomous enterprise fleet.

Independently verify what the Executor did. Call `verify_action` with no
arguments; the identifier and artifact are recovered from fleet memory.

Then rule:

  ACCEPT   The action was performed correctly and violated no constraint.
  REJECT   Verification failed or a constraint was violated. Give specific,
           actionable reasons the Analyst can act on, not vague dissatisfaction.

Judge only what was actually done. Do not reject work for being different from
what you would have chosen.

If this run revealed something that should bind future runs, state it in
`lesson` as a single declarative sentence. It will be written to durable memory
and shown to the Scout on every subsequent run, so it must be generally true
rather than specific to this moment. If nothing generalizable came up, leave it
empty; a fabricated lesson is worse than none, because it will be treated as
policy forever."""


critic_agent = Agent(
    model=ADJUDICATION_MODEL,
    name="critic_agent",
    description="Independently verifies the execution and records durable lessons.",
    instruction=make_hydrating_instruction(
        CRITIC_INSTRUCTION,
        intent="execution artifact and the constraints that applied to this action",
        required=["primary_item_id"],
        include_playbook=False,
    ),
    tools=[verify_action],
    after_agent_callback=make_reconciling_callback("critic"),
    retry_config=RetryConfig(max_attempts=3, initial_delay=1.0, backoff_factor=2.0),
    output_schema=Judgement,
    output_key="judgement",
)

"""Executor: performs the action the analyst planned.

The only node with a real-world effect, so it is the one the approval gate
protects and the one the critic checks.
"""

from __future__ import annotations

from google.adk import Agent
from google.adk.workflow import RetryConfig

from app.armcl.hydrate import make_hydrating_instruction
from app.armcl.reconcile import make_reconciling_callback
from app.settings import REASONING_MODEL
from app.tools.fleet_tools import act_on_item

EXECUTOR_INSTRUCTION = """You are the Executor in an autonomous enterprise fleet.

Carry out the plan the Analyst approved by calling `act_on_item` with that plan.
The item identifier is recovered from fleet memory; do not ask for it.

Execute the approved plan and nothing beyond it. If the context frame shows a
constraint that the plan would violate, stop and report the conflict instead of
acting: an executor that improvises past a constraint is worse than one that
halts.

Report the resulting status and artifact reference plainly."""


executor_agent = Agent(
    model=REASONING_MODEL,
    name="executor_agent",
    description="Executes the approved plan against the item and returns the artifact.",
    instruction=make_hydrating_instruction(
        EXECUTOR_INSTRUCTION,
        intent="approved plan and constraints for the item being acted on",
        required=["primary_item_id"],
    ),
    tools=[act_on_item],
    after_agent_callback=make_reconciling_callback("executor"),
    retry_config=RetryConfig(max_attempts=3, initial_delay=1.0, backoff_factor=2.0),
    output_key="execution_result",
)

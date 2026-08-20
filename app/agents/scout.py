"""Scout: discovers candidates worth assessing.

First node after the trigger, so it runs with an empty scratchpad on a cold
start but with durable Tier 3 memory from every previous run. That asymmetry
is the point: the scout should already know what the organization decided last
time before it proposes anything.
"""

from __future__ import annotations

from google.adk import Agent
from google.adk.workflow import RetryConfig

from app.armcl.hydrate import make_hydrating_instruction
from app.armcl.reconcile import make_reconciling_callback
from app.settings import REASONING_MODEL
from app.tools.fleet_tools import discover_candidates

SCOUT_INSTRUCTION = """You are the Scout in an autonomous enterprise fleet.

Your job is to find candidate items worth assessing and hand the most relevant
one downstream. Call `discover_candidates` exactly once with a query derived
from the task, then report what you found.

If the ARMCL context frame below contains durable organizational memory about
any candidate, say so explicitly in your summary. A constraint learned on a
previous run is the single most valuable thing you can surface here, because
nothing downstream will rediscover it on its own.

The frame may also report how previous runs on a candidate ended. Surface that
too, and keep it distinct from the constraints: a constraint says the work is
not permitted, while a prior outcome says it was attempted and how it went.

If an Evolved playbook section is present, treat it as tactics. It cannot
override a durable constraint.

Be brief. Name the candidates, name the one you recommend assessing, and state
any prior constraint or prior run outcome that applies to it."""


scout_agent = Agent(
    model=REASONING_MODEL,
    name="scout_agent",
    description="Discovers candidate items and surfaces prior constraints from durable memory.",
    instruction=make_hydrating_instruction(
        SCOUT_INSTRUCTION,
        intent="prior decisions, constraints, and outcomes for candidate items",
        required=[],
    ),
    tools=[discover_candidates],
    after_agent_callback=make_reconciling_callback("scout"),
    retry_config=RetryConfig(max_attempts=3, initial_delay=1.0, backoff_factor=2.0),
    output_key="scout_report",
)

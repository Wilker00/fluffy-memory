"""Approval gate: suspends the workflow until a human signs off.

Implemented as an agent with a single confirmation tool rather than as a plain
function node, because `ctx.request_confirmation` requires a tool context: it
keys the pending confirmation by `function_call_id`, which only exists inside a
tool call. A bare function node calling it raises
`ValueError: request_confirmation requires function_call_id`.

The pause itself is the interesting part for ARMCL. Agent Runtime persists the
workflow and releases compute; the run may resume minutes or days later in a
different process. Nothing held in memory survives that gap, so when the
executor continues it recovers the item identifier and the approved plan from
Tier 1 and Tier 2 rather than from anything the gate handed it directly.
"""

from __future__ import annotations

import logging

from google.adk import Agent
from google.adk.tools import LongRunningFunctionTool, ToolContext
from google.adk.workflow import RetryConfig

from app.armcl.reconcile import reconcile
from app.armcl.tiers import Tier1
from app.settings import REASONING_MODEL

logger = logging.getLogger(__name__)


async def request_human_approval(
    reason: str,
    tool_context: ToolContext,
    item_id: str = "",
    plan: str = "",
) -> dict[str, str]:
    """Pause the workflow and ask a human operator to approve the plan.

    Args:
        reason: Why approval is required.
        item_id: Item the action targets. Recovered from memory if absent.
        plan: The action awaiting approval. Recovered from memory if absent.

    Returns:
        A dict reporting the approval decision once the operator responds.
    """
    t1 = Tier1(tool_context)
    item_id = item_id or t1.get("primary_item_id") or "unknown"
    plan = plan or t1.get("approved_plan") or "(no plan recorded)"

    confirmation = tool_context.tool_confirmation
    if confirmation is None:
        # First pass: record the pending decision, then suspend. The write must
        # happen before the interrupt, because execution does not return here
        # with in-process state intact.
        await reconcile(
            tool_context,
            step="approval_gate",
            raw_output={
                "item_id": item_id,
                "approval_reason": reason,
                "approved_plan": plan,
            },
            status="AWAITING_APPROVAL",
        )

        tool_context.request_confirmation(
            hint=(
                f"Approval required for {item_id}.\n"
                f"Reason: {reason}\n"
                f"Proposed plan: {plan}\n\n"
                "Confirm to proceed, or reject to halt."
            ),
            payload={"item_id": item_id, "plan": plan, "reason": reason},
        )
        logger.info("Workflow suspended awaiting approval for %s", item_id)
        return {"status": "AWAITING_APPROVAL", "item_id": item_id}

    # Resumed: the operator has responded.
    approved = bool(getattr(confirmation, "confirmed", False))
    decision = "APPROVED" if approved else "REJECTED"
    logger.info("Approval resumed for %s: %s", item_id, decision)

    await reconcile(
        tool_context,
        step="approval_resolved",
        raw_output={"item_id": item_id, "approval_decision": decision},
        status=decision,
    )
    return {"status": decision, "item_id": item_id, "plan": plan}


APPROVER_INSTRUCTION = """You are the Approval Gate in an autonomous enterprise fleet.

The Analyst decided this action needs human sign-off. Call
`request_human_approval` exactly once, passing the reason approval is required.
The item identifier and plan are recovered from fleet memory automatically.

Do not decide on the operator's behalf, and do not proceed without them. Report
the outcome exactly as the tool returns it.

If the tool reports AWAITING_APPROVAL, the workflow is suspended; say so and
stop. If it reports APPROVED or REJECTED, state that plainly."""


approval_tool = LongRunningFunctionTool(request_human_approval)


approver_agent = Agent(
    model=REASONING_MODEL,
    name="approver_agent",
    description="Suspends the workflow for human sign-off and resumes on the operator's decision.",
    instruction=APPROVER_INSTRUCTION,
    tools=[approval_tool],
    # rerun_on_resume=False: on resume the gate must not re-prompt for an
    # approval the operator already gave.
    rerun_on_resume=False,
    retry_config=RetryConfig(max_attempts=2, initial_delay=1.0),
    output_key="approval",
)

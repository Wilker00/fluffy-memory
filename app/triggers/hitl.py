"""Resuming a workflow that paused for human approval.

A suspended run is resumed by calling `run_async` with the *original*
`invocation_id` and no new message. ADK reloads the persisted workflow state,
skips nodes marked `rerun_on_resume=False`, and continues from the interrupt.

The resume may happen in a different process than the pause, which is exactly
why ARMCL writes state to the tiers before suspending: nothing held in memory
survives the gap.
"""

from __future__ import annotations

import logging
from typing import Any

from google.adk import Runner
from google.adk.tools.tool_confirmation import ToolConfirmation

logger = logging.getLogger(__name__)


def find_pending_approvals(events: list[Any]) -> dict[str, ToolConfirmation]:
    """Collect confirmations the workflow is waiting on."""
    pending: dict[str, ToolConfirmation] = {}
    for event in events:
        requested = getattr(event.actions, "requested_tool_confirmations", None)
        if requested:
            pending.update(requested)
    return pending


def find_interrupted_invocation(events: list[Any]) -> str | None:
    """Return the invocation_id of the most recent interrupt, if any."""
    for event in reversed(events):
        if getattr(event, "interrupted", False):
            return event.invocation_id
    return None


async def resume_with_approval(
    runner: Runner,
    *,
    user_id: str,
    session_id: str,
    invocation_id: str,
    approved: bool,
    note: str = "",
) -> list[Any]:
    """Resume a suspended workflow with the operator's decision.

    The decision is written into session state before resuming so it is
    durable: if the resume itself fails, the approval is not lost and the
    operator is not asked twice.
    """
    state_delta = {
        "armcl:t1:approval_decision": "APPROVED" if approved else "REJECTED",
        "armcl:t1:approval_note": note,
    }

    logger.info(
        "Resuming invocation %s with decision=%s",
        invocation_id,
        state_delta["armcl:t1:approval_decision"],
    )

    events: list[Any] = []
    async for event in runner.run_async(
        user_id=user_id,
        session_id=session_id,
        invocation_id=invocation_id,
        state_delta=state_delta,
    ):
        events.append(event)
    return events

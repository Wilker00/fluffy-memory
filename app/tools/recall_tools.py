"""Read-only recall tools for operator-facing explanation.

These expose the ARMCL memory chain for inspection and nothing else. There is
no write path here on purpose: an agent that can answer questions about past
decisions must not be able to alter the record it is describing, or its answers
stop being evidence.

The autonomous fleet never calls these. They exist for the explainer agent,
which runs in its own session outside the workflow graph.
"""

from __future__ import annotations

import logging
from typing import Any

from app.armcl.policy import render_value
from app.armcl.tiers import Tier1, Tier2

logger = logging.getLogger(__name__)


async def recall_run_history(tool_context: Any) -> dict[str, Any]:
    """Return the step-by-step record of the run in the current session.

    Use this to answer questions about what the fleet did and in what order.

    Returns:
        The reconciled scratchpad and the full episodic ledger, including each
        step's status and how much raw output it pruned.
    """
    t1 = Tier1(tool_context)
    t2 = Tier2(tool_context)
    entries = t2.all_entries()

    steps = [
        {
            "step": e.step,
            "status": e.status,
            "keys_written": e.keys_written,
            "bytes_pruned": e.bytes_pruned,
            "summary": render_value(e.summary, 400),
        }
        for e in entries
    ]

    return {
        "steps": steps,
        "step_count": len(steps),
        "current_state": {k: render_value(v, 200) for k, v in t1.snapshot().items()},
        "rejections_recorded": t2.rejection_count,
        "total_bytes_pruned": sum(e.bytes_pruned for e in entries),
    }


async def recall_durable_memory(query: str, tool_context: Any) -> dict[str, Any]:
    """Search durable cross-session memory for constraints and past outcomes.

    Use this to answer why a decision was made when the reason predates the
    current run.

    Args:
        query: What to look for, in natural language.

    Returns:
        Matching durable facts, or an empty list when memory is unavailable.
    """
    search = getattr(tool_context, "search_memory", None)
    if search is None:
        return {"facts": [], "available": False, "query": query}

    try:
        response = await search(query)
    except Exception as exc:  # noqa: BLE001 - an outage degrades the answer
        logger.warning("Explainer Tier 3 lookup failed: %s", exc)
        return {"facts": [], "available": False, "query": query}

    facts: list[str] = []
    for entry in getattr(response, "memories", []) or []:
        content = getattr(entry, "content", None)
        parts = getattr(content, "parts", None) if content else None
        if parts:
            text = " ".join(p.text or "" for p in parts).strip()
            if text:
                facts.append(render_value(text, 400))

    return {"facts": facts, "available": True, "query": query}


async def explain_decision_path(tool_context: Any) -> dict[str, Any]:
    """Reconstruct why the run reached the outcome it did.

    Use this for "why" questions. It reports the terminal state alongside the
    constraints and approvals that produced it, rather than asking the model to
    infer causation from the raw ledger.

    Returns:
        The outcome, the step that produced it, and the recorded reasons.
    """
    t1 = Tier1(tool_context)
    entries = Tier2(tool_context).all_entries()

    terminal_states = {
        "DECLINED": "The fleet declined the work.",
        "BLOCKED": "Inbound content failed guardrail screening and was quarantined.",
        "DENIED": "A human operator refused the proposed action.",
        "HALTED": "The circuit breaker stopped a verification cycle that would not converge.",
        "AWAITING_APPROVAL": "The run is suspended, waiting on a human decision.",
    }

    outcome, detail, final_step = "IN_PROGRESS", "The run has not reached a terminal state.", None
    for entry in reversed(entries):
        if entry.status in terminal_states:
            outcome, detail, final_step = entry.status, terminal_states[entry.status], entry.step
            break
    else:
        if entries and entries[-1].step == "complete":
            outcome = "COMPLETED"
            detail = "The run finished and was verified."
            final_step = "complete"

    return {
        "outcome": outcome,
        "explanation": detail,
        "final_step": final_step,
        "item_id": t1.get("primary_item_id"),
        "constraints_in_play": [
            render_value(v, 300)
            for k, v in t1.snapshot().items()
            if "constraint" in k.lower() or "policy" in k.lower()
        ],
        "approval_decision": t1.get("approval_decision"),
        "steps_taken": [e.step for e in entries],
    }

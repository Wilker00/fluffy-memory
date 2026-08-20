"""Agent-facing tools.

Two conventions hold throughout this module.

Exceptions propagate. ADK 2.0 relies on them for `RetryConfig`, and
`NodeInterruptedError` is how human-in-the-loop pause works, so a
`except Exception` here would silently disable retries and break approval
gates. Nothing below catches broadly.

Every tool reconciles through ARMCL. Raw payloads are distilled into tiered
facts on the way out, which is what keeps a 4000-line inspection from
consuming the context window and what lets a later step recover an identifier
it never saw.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

from google.adk.tools import ToolContext

from app.armcl.hydrate import hydrate
from app.armcl.reconcile import reconcile
from app.armcl.tiers import Tier1
from app.guardrails import screen_inbound
from app.tools.protocol import active_domain

logger = logging.getLogger(__name__)


async def discover_candidates(
    query: str,
    tool_context: ToolContext,
    limit: int = 5,
) -> dict[str, Any]:
    """Find candidate items worth evaluating for the given query.

    Args:
        query: What to look for.
        limit: Maximum number of candidates to return.

    Returns:
        A dict with the candidate list and the count found.
    """
    domain = active_domain()
    candidates = await domain.discover(query, limit=limit)

    payload = {
        "count": len(candidates),
        "candidates": [c.model_dump() for c in candidates],
        # Promoted to a top-level key so ARMCL classifies and carries it, which
        # is what a later inspect step recovers instead of asking the operator.
        "primary_item_id": candidates[0].item_id if candidates else None,
    }

    await reconcile(tool_context, step="discover_candidates", raw_output=payload)
    return payload


async def inspect_item(
    tool_context: ToolContext,
    item_id: str = "",
) -> dict[str, Any]:
    """Retrieve detailed information and constraints for one item.

    If item_id is omitted, it is recovered from memory of earlier steps.

    Args:
        item_id: Identifier of the item. Optional; recovered from memory if absent.

    Returns:
        A dict with the item's constraints, structured facts, and telemetry size.
    """
    # The dependency gap. The identifier was produced several steps back, and
    # rather than stalling to ask for it, ARMCL recovers it from Tier 1/2.
    if not item_id:
        frame = await hydrate(
            tool_context,
            intent="identifier of the item currently under assessment",
            required=["primary_item_id"],
        )
        item_id = Tier1(tool_context).get("primary_item_id") or ""
        if not item_id:
            return {
                "error": "no_item_id",
                "detail": "No item_id supplied and none recoverable from memory.",
                "gaps": frame.gaps,
            }
        logger.info("ARMCL resolved item_id=%s without operator input", item_id)

    domain = active_domain()
    report = await domain.inspect(item_id)

    # Screen exactly the untrusted fields that will cross into the model. The
    # bulk `raw` field is deliberately dropped and therefore is not a model
    # boundary; screening only its first 5,000 characters previously left the
    # actual constraints/facts completely unchecked.
    delivered = json.dumps(
        {"constraints": report.constraints, "facts": report.facts},
        sort_keys=True,
        default=str,
    )
    verdict = await screen_inbound(delivered, context=f"inspect:{item_id}")
    if verdict.is_blocked:
        payload = {
            "item_id": item_id,
            "guardrail": verdict.state.value,
            "filters_matched": verdict.filters_matched,
            "detail": "Inbound content blocked; downstream steps must not use it.",
        }
        await reconcile(tool_context, step="inspect_item", raw_output=payload, status="BLOCKED")
        return payload

    payload = {
        "item_id": item_id,
        "constraints": report.constraints,
        "telemetry_lines": report.raw.count("\n") + 1 if report.raw else 0,
        **report.facts,
    }

    await reconcile(tool_context, step="inspect_item", raw_output=payload)
    return payload


async def act_on_item(
    plan: str,
    tool_context: ToolContext,
    item_id: str = "",
) -> dict[str, Any]:
    """Execute the planned action against an item.

    Args:
        plan: What to do and why.
        item_id: Identifier of the item. Recovered from memory if absent.

    Returns:
        A dict with the resulting status and artifact reference.
    """
    if not item_id:
        item_id = Tier1(tool_context).get("primary_item_id") or ""
        if not item_id:
            return {"error": "no_item_id", "detail": "Cannot act without an item_id."}

    t1 = Tier1(tool_context)
    # A resumed Pub/Sub delivery can create a new ADK invocation in the same
    # durable session.  Scope the key to that session, not the invocation, so
    # replaying the tool after a crash reaches the same external idempotency key.
    session = getattr(tool_context, "session", None)
    operation_scope = getattr(session, "id", "") or getattr(session, "session_id", "")
    operation_scope = operation_scope or getattr(tool_context, "run_id", "") or "local"
    material = f"{operation_scope}:{item_id}:{plan}"
    idempotency_key = hashlib.sha256(material.encode("utf-8")).hexdigest()

    # Persist the key before crossing the side-effect boundary. ADK resumability
    # is at-least-once: after a crash the same key reaches the domain adapter,
    # which must return the original result rather than repeat the action.
    t1.set("action_idempotency_key", idempotency_key)

    cached = t1.get("action_result")
    if isinstance(cached, dict) and cached.get("idempotency_key") == idempotency_key:
        return dict(cached["result"])

    domain = active_domain()
    result = await domain.act(item_id, plan, idempotency_key)

    payload = result.model_dump()
    t1.set("action_result", {"idempotency_key": idempotency_key, "result": payload})
    await reconcile(tool_context, step="act_on_item", raw_output=payload)
    return payload


async def verify_action(
    tool_context: ToolContext,
    item_id: str = "",
    artifact: str = "",
) -> dict[str, Any]:
    """Independently verify that an action produced a correct result.

    Args:
        item_id: Identifier of the item. Recovered from memory if absent.
        artifact: Artifact reference from the action. Recovered from memory if absent.

    Returns:
        A dict reporting whether the action was accepted and why.
    """
    t1 = Tier1(tool_context)
    item_id = item_id or t1.get("primary_item_id") or t1.get("item_id") or ""
    artifact = artifact or t1.get("artifact") or ""

    if not item_id:
        return {"error": "no_item_id", "detail": "Cannot verify without an item_id."}

    domain = active_domain()
    result = await domain.verify(item_id, artifact)

    payload = result.model_dump()
    await reconcile(
        tool_context,
        step="verify_action",
        raw_output=payload,
        status="SUCCESS" if result.accepted else "REJECTED",
    )
    return payload

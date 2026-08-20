"""Pre-action hydration: the first half of the ARMCL loop.

Before a node acts, ARMCL works out which facts the step needs, checks whether
Tier 1 already holds them, and if not recovers them from Tier 2 and Tier 3
without involving the operator. That is the whole point of the loop: a
multi-step workflow should not stall at step seven to ask what identifier step
one produced.

Hydration is deliberately cheap. It reads the ledger and issues at most one
Tier 3 similarity query, so it can run before every node without becoming the
dominant cost of the workflow.
"""

from __future__ import annotations

import logging
from typing import Any

from app.armcl.tiers import ContextFrame, Tier1, Tier2
from app.armcl.trajectory import partition
from app.evolve.playbook import ensure_playbook, is_playbook
from app.observability.spans import armcl_span, record_memory_hit

logger = logging.getLogger(__name__)


async def hydrate(
    ctx: Any,
    *,
    intent: str,
    required: list[str] | None = None,
    recent_limit: int = 3,
) -> ContextFrame:
    """Assemble the context frame for the step about to run.

    Args:
        ctx: ADK Context or CallbackContext.
        intent: Natural-language description of the step, used as the Tier 3 query.
        required: Parameter names the step needs. Drives gap analysis.
        recent_limit: How many ledger entries to include.
    """
    required = required or []
    t1 = Tier1(ctx)
    t2 = Tier2(ctx)

    with armcl_span("hydrate", intent=intent[:120]) as span:
        frame = ContextFrame(
            scratchpad=t1.snapshot(),
            recent_steps=t2.recent(recent_limit),
        )
        sources: list[str] = []

        # Tier 1 gap analysis. Tier 1 is credited whenever it actually supplied
        # something, including a partial resolution. Crediting it only on a
        # clean sweep made traces claim a frame came from nowhere while it sat
        # full of scratchpad state, which is the opposite of auditable.
        gaps = t1.missing(required)
        if frame.scratchpad and (not required or len(gaps) < len(required)):
            sources.append("tier1")

        # Tier 2: try to close gaps from the episodic ledger before paying for
        # a semantic query.
        still_missing: list[str] = []
        for key in gaps:
            recovered = t2.find_value(key)
            if recovered is not None:
                t1.set(key, recovered)
                frame.scratchpad[key] = recovered
                frame.retrieved_facts.append(f"{key} (recovered from step history): {recovered}")
                if "tier2" not in sources:
                    sources.append("tier2")
            else:
                still_missing.append(key)

        frame.gaps = still_missing

        # Tier 3: durable organizational memory. Always consulted, because
        # constraints that change the decision are not predictable from the
        # parameter list alone. The same query returns prior run outcomes,
        # which are separated out so the frame can present them as evidence
        # rather than as policy. Playbook records are stripped here: they are
        # tactics, and treating them as constraints would let a rewrite that
        # failed the auditor still bind the next run as policy.
        tier3_hits = [hit for hit in await _search_tier3(ctx, intent) if not is_playbook(hit)]
        if tier3_hits:
            constraints, prior_outcomes = partition(tier3_hits)
            frame.retrieved_facts.extend(constraints)
            frame.prior_outcomes.extend(prior_outcomes)
            sources.append("tier3")

        frame.hydrated_from = sources
        record_memory_hit(
            span,
            tier=3 if "tier3" in sources else (2 if "tier2" in sources else 1),
            keys=required,
            hit_count=len(frame.retrieved_facts) + len(frame.prior_outcomes),
            miss_count=len(still_missing),
            reason=f"gap_analysis required={len(required)} unresolved={len(still_missing)}",
        )

    if still_missing:
        logger.info("ARMCL could not resolve %s; agent must handle or escalate", still_missing)

    return frame


async def _search_tier3(ctx: Any, query: str) -> list[str]:
    """Query durable memory.

    Memory is an optimisation, not a correctness requirement, so a Tier 3
    outage degrades the frame rather than failing the step. This is the one
    place ARMCL deliberately swallows an exception, and it is scoped to a
    read-only lookup whose absence the caller already handles.
    """
    search = getattr(ctx, "search_memory", None)
    if search is None:
        return []

    try:
        response = await search(query)
    except Exception as exc:  # noqa: BLE001 - see docstring
        logger.warning("ARMCL Tier 3 lookup failed, continuing without it: %s", exc)
        return []

    facts: list[str] = []
    for entry in getattr(response, "memories", []) or []:
        content = getattr(entry, "content", None)
        if content is None or not getattr(content, "parts", None):
            continue
        text = " ".join(p.text or "" for p in content.parts).strip()
        if text:
            facts.append(text)
    return facts


def make_hydrating_instruction(
    base_instruction: str,
    intent: str,
    required: list[str],
    *,
    include_playbook: bool = True,
):
    """Build an ADK instruction provider that injects a live context frame.

    ADK accepts a callable for `Agent.instruction`, which is how hydration runs
    on every turn without the agent having to request it.

    `include_playbook` is true for workers that may follow evolved tactics, and
    false for the critic and the approval gate. Those two are the scorekeepers;
    giving them the playbook would let a rewrite grade itself.
    """

    async def _instruction(readonly_ctx: Any) -> str:
        frame = await hydrate(readonly_ctx, intent=intent, required=required)
        parts = [base_instruction]
        if not frame.is_empty:
            parts.append(frame.render())
        if include_playbook:
            playbook = await ensure_playbook(readonly_ctx)
            overlay = playbook.render_for_instruction()
            if overlay:
                parts.append(overlay)
        return "\n\n".join(parts)

    return _instruction

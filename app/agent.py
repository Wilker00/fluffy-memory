"""The root workflow graph.

Shape:

    START -> screen -> {BLOCKED: quarantine, CLEAN: scout}
    scout -> analyst -> route_assessment
        ACT         -> executor -> critic -> route_judgement
        DECLINE     -> log_decline -> record_score
        NEEDS_HUMAN -> approver -> route_approval
            APPROVED -> executor
            DENIED   -> approval_denied -> record_score
    route_judgement
        ACCEPT -> complete -> record_score
        REJECT -> analyst          (bounded by the circuit breaker)
        HALT   -> circuit_broken -> record_score
    record_score
        EVOLVE -> evolver -> audit_playbook
        SKIP   -> evolution_skipped

Four things here are easy to get wrong and are done deliberately.

Routing is emitted through `ctx.route`, not by returning a value. A router that
returns a string sets the node's *output* and leaves the route unset, so every
conditional edge silently fails to match and the branch dies without raising.

The approval gate is an agent, not a function node, because
`ctx.request_confirmation` requires a tool context. See app/agents/approver.py.

The critic's REJECT edge is a cycle back to the analyst. Cycles are how these
systems hang, so `route_judgement` counts rejections in Tier 2 and diverts to
`circuit_broken` once the threshold is reached. The bound is enforced in the
graph rather than requested in a prompt.

No node wraps its body in `except Exception`. That would disable `RetryConfig`,
and catching `BaseException` would additionally swallow `NodeInterruptedError`,
which is the mechanism the approval gate depends on.

Every terminal node labels the ledger and writes a trajectory record to Tier 3
before returning, so the next run starts knowing how the last one ended rather
than rediscovering it. See app/armcl/trajectory.py.
"""

from __future__ import annotations

import logging
from typing import Any

from google.adk import Context, Workflow
from google.adk.workflow import START, RetryConfig, node

from app.agents.analyst import analyst_agent
from app.agents.approver import approver_agent
from app.agents.critic import critic_agent
from app.agents.evolver import evolver_agent
from app.agents.executor import executor_agent
from app.agents.scout import scout_agent
from app.armcl.invariants import check_execution
from app.armcl.reconcile import reconcile
from app.armcl.tiers import LedgerEntry, Tier1, Tier2
from app.armcl.trajectory import persist_trajectory
from app.evolve.auditor import audit_proposal
from app.evolve.playbook import get_store, persist_playbook, scope_key
from app.evolve.score import RunScore, score_run
from app.guardrails import screen_inbound
from app.observability.spans import armcl_span
from app.settings import settings

logger = logging.getLogger(__name__)

_STANDARD_RETRY = RetryConfig(max_attempts=3, initial_delay=1.0, backoff_factor=2.0)

PRE_EXECUTION = "pre_execution"
"""Checkpoint label for the state a rejected attempt rolls back to."""


def _as_dict(value: Any) -> dict[str, Any]:
    """Normalise a node output that may be a model, dict, or JSON string."""
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if isinstance(value, str):
        import json

        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {"value": parsed}
        except json.JSONDecodeError:
            return {"value": value}
    return {"value": value}


# ---------------------------------------------------------------------------
# Ingress screening
# ---------------------------------------------------------------------------


async def screen_request(ctx: Context, node_input: Any = None) -> dict[str, Any]:
    """Screen the inbound trigger payload before any agent sees it."""
    text = ""
    if node_input is not None:
        text = node_input if isinstance(node_input, str) else str(node_input)
    if not text:
        user_content = getattr(ctx, "user_content", None)
        if user_content is not None and getattr(user_content, "parts", None):
            text = " ".join(p.text or "" for p in user_content.parts)

    verdict = await screen_inbound(text, context="trigger")
    ctx.route = verdict.route

    Tier1(ctx).set("trigger_text", text[:500])
    logger.info("Ingress screening: %s", verdict.summary())

    return {
        "guardrail_state": verdict.state.value,
        "guardrail_backend": verdict.backend,
        "filters_matched": verdict.filters_matched,
        "request": text[:500],
    }


async def quarantine(ctx: Context, node_input: Any = None) -> dict[str, Any]:
    """Terminal node for blocked input.

    Recorded rather than discarded: a blocked request is evidence, and the
    trace is what shows a judge that the guardrail fired.
    """
    payload = _as_dict(node_input)
    await reconcile(ctx, step="quarantine", raw_output=payload, status="BLOCKED")
    logger.warning("Request quarantined: %s", payload.get("filters_matched"))
    return {
        "status": "QUARANTINED",
        "reason": "Inbound content failed guardrail screening.",
        "filters_matched": payload.get("filters_matched", []),
    }


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------


async def route_assessment(ctx: Context, node_input: Any = None) -> dict[str, Any]:
    """Branch on the analyst's decision."""
    assessment = _as_dict(node_input)
    decision = str(assessment.get("decision", "NEEDS_HUMAN")).upper().strip()

    if decision not in {"ACT", "DECLINE", "NEEDS_HUMAN"}:
        # An unparseable decision is escalated, not guessed at.
        logger.warning("Unrecognised decision %r; escalating to human", decision)
        decision = "NEEDS_HUMAN"

    ctx.route = decision

    t1 = Tier1(ctx)
    if assessment.get("item_id"):
        t1.set("primary_item_id", assessment["item_id"])
    if assessment.get("plan"):
        t1.set("approved_plan", assessment["plan"])

    # Snapshot before anything acts. A rejected attempt rolls back to here, so
    # the retry re-plans from the pre-execution state rather than from the
    # wreckage of the attempt that just failed.
    t1.checkpoint(PRE_EXECUTION)

    with armcl_span("route_assessment", decision=decision):
        logger.info("Assessment route: %s", decision)

    return assessment


async def route_approval(ctx: Context, node_input: Any = None) -> dict[str, Any]:
    """Branch on the operator's answer to the approval gate.

    The gate is only a gate if refusal stops the work. The decision is read
    from Tier 1 because the workflow resumes cold: the approver's in-process
    return value does not survive the suspension, but the reconciled state
    does. Anything that is not an explicit approval is treated as a refusal,
    so a lost or malformed decision fails safe.
    """
    payload = _as_dict(node_input)
    decision = str(
        Tier1(ctx).get("approval_decision") or payload.get("approval_decision") or ""
    ).upper()

    if decision == "APPROVED":
        ctx.route = "APPROVED"
        logger.info("Operator approved; proceeding to execution")
    else:
        ctx.route = "DENIED"
        logger.warning("Operator did not approve (decision=%r); halting", decision or "MISSING")

    payload["approval_decision"] = decision or "MISSING"
    return payload


async def approval_denied(ctx: Context, node_input: Any = None) -> dict[str, Any]:
    """Terminal node for work a human refused.

    Recorded durably on purpose. An operator refusal is a policy signal, and a
    later run that recalls it can decline before spending the work.
    """
    payload = _as_dict(node_input)
    item_id = Tier1(ctx).get("primary_item_id")

    summary = {
        "status": "APPROVAL_DENIED",
        "item_id": item_id,
        "approval_decision": payload.get("approval_decision", "MISSING"),
        "detail": f"Human operator declined the proposed action for {item_id}.",
    }

    await reconcile(ctx, step="approval_denied", raw_output=summary, status="DENIED")
    Tier2(ctx).label_pending("DENIED")
    await persist_trajectory(ctx, outcome="DENIED")
    logger.info("Halted on operator refusal for %s", item_id)
    return summary


async def route_judgement(ctx: Context, node_input: Any = None) -> dict[str, Any]:
    """Branch on the critic's verdict, bounding the reject cycle.

    The circuit breaker lives here because it needs the rejection count from
    Tier 2, which survives across the cycle.
    """
    judgement = _as_dict(node_input)
    verdict = str(judgement.get("verdict", "REJECT")).upper().strip()
    t1 = Tier1(ctx)
    t2 = Tier2(ctx)

    # Deterministic checks run first and can veto. The critic is a language
    # model, so letting it be the only acceptance signal means the check that
    # catches hallucination can itself hallucinate. It may reject what the
    # invariants allow; it may not accept what they forbid.
    invariants = check_execution(ctx)
    judgement["invariants"] = {
        "passed": invariants.passed,
        "violations": invariants.violations,
        "checks_run": invariants.checks_run,
    }
    if verdict == "ACCEPT" and not invariants.passed:
        logger.error("Overriding critic ACCEPT: %s", invariants.summary())
        verdict = "REJECT"
        judgement["overridden_by_invariants"] = True

    if verdict == "ACCEPT":
        t2.reset_rejections()
        t2.record_outcome("executor", "ACCEPTED")
        t2.record_outcome("critic", "ACCEPTED")
        ctx.route = "ACCEPT"
        logger.info("Critic accepted; completing")
        return judgement

    # Label the failed attempt before rolling it back, so the trajectory record
    # keeps what was tried while the working state does not.
    t2.record_outcome("executor", "REJECTED")
    t2.record_rejected_artifact(t1.get("artifact"))
    discarded = t1.restore(PRE_EXECUTION)
    if discarded:
        logger.info("Rolled back %d keys from the rejected attempt: %s", len(discarded), discarded)
    judgement["rolled_back"] = discarded

    count = t2.record_rejection()
    threshold = settings.circuit_breaker_threshold

    if count >= threshold:
        ctx.route = "HALT"
        logger.error("Circuit breaker tripped after %d rejections", count)
        judgement["circuit_breaker"] = {"tripped": True, "rejections": count}
    else:
        ctx.route = "REJECT"
        logger.info("Critic rejected (%d/%d); returning to analyst", count, threshold)
        judgement["circuit_breaker"] = {"tripped": False, "rejections": count}

    return judgement


# ---------------------------------------------------------------------------
# Terminal and gate nodes
# ---------------------------------------------------------------------------


async def log_decline(ctx: Context, node_input: Any = None) -> dict[str, Any]:
    """Terminal node for a declined item.

    A decline is the outcome most worth remembering: it is usually driven by a
    constraint, and writing it durably is what lets a later run decline sooner.
    """
    assessment = _as_dict(node_input)
    await reconcile(ctx, step="log_decline", raw_output=assessment, status="DECLINED")
    Tier2(ctx).label_pending("DECLINED")
    await persist_trajectory(ctx, outcome="DECLINED")
    logger.info("Declined %s: %s", assessment.get("item_id"), assessment.get("rationale"))
    return {
        "status": "DECLINED",
        "item_id": assessment.get("item_id"),
        "rationale": assessment.get("rationale"),
        "constraints_applied": assessment.get("constraints_applied", []),
    }


async def complete(ctx: Context, node_input: Any = None) -> dict[str, Any]:
    """Terminal node for a successful run."""
    judgement = _as_dict(node_input)
    t1 = Tier1(ctx)

    summary = {
        "status": "COMPLETED",
        "item_id": t1.get("primary_item_id"),
        "artifact": t1.get("artifact"),
        "verdict": judgement.get("verdict"),
        "lesson": judgement.get("lesson", ""),
    }

    await reconcile(ctx, step="complete", raw_output=summary, status="SUCCESS")
    Tier2(ctx).label_pending("ACCEPTED")
    await persist_trajectory(ctx, outcome="COMPLETED")
    logger.info("Run complete for %s", summary["item_id"])
    return summary


async def circuit_broken(ctx: Context, node_input: Any = None) -> dict[str, Any]:
    """Terminal node for a workflow the critic would not accept.

    Halting with an explanation is the correct outcome for a worker that will
    not converge. Retrying indefinitely burns budget and never terminates.
    """
    judgement = _as_dict(node_input)
    t2 = Tier2(ctx)

    summary = {
        "status": "HALTED_CIRCUIT_BREAKER",
        "item_id": Tier1(ctx).get("primary_item_id"),
        "rejections": t2.rejection_count,
        "last_reasons": judgement.get("reasons", []),
        "detail": (
            f"Halted after {t2.rejection_count} consecutive rejections "
            f"(threshold {settings.circuit_breaker_threshold}). Human review required."
        ),
    }

    await reconcile(ctx, step="circuit_broken", raw_output=summary, status="HALTED")
    t2.label_pending("HALTED")
    await persist_trajectory(ctx, outcome="HALTED")
    logger.error("Circuit breaker halt: %s", summary["detail"])
    return summary


# ---------------------------------------------------------------------------
# Self-evolution: score the run, maybe rewrite tactics, never rewrite the rules
# ---------------------------------------------------------------------------


def _playbook_disk_path():
    """Where a local process persists the playbook between invocations."""
    from pathlib import Path

    if settings.playbook_path:
        return Path(settings.playbook_path)
    return Path(settings.chroma_dir) / "playbook.json"


async def record_score(ctx: Context, node_input: Any = None) -> dict[str, Any]:
    """Score a finished run and decide whether the evolver should fire.

    Quarantine never reaches here: blocked input is not a signal about tactics.
    Everything else does, so the next scheduled run can start from a playbook
    that has seen this outcome.
    """
    payload = _as_dict(node_input)
    outcome = str(payload.get("status") or "")
    scored = score_run(ctx, outcome)

    t1 = Tier1(ctx)
    t1.set("run_outcome", scored.outcome)
    t1.set("run_proxy_score", scored.proxy)

    ctx.route = "EVOLVE" if settings.evolve_enabled else "SKIP"
    logger.info(
        "Run proxy score %.2f for %s (rejections=%d); evolution %s",
        scored.proxy,
        scored.outcome,
        scored.rejections,
        ctx.route,
    )
    return {**payload, **scored.as_dict()}


async def audit_playbook(ctx: Context, node_input: Any = None) -> dict[str, Any]:
    """Install a proposal only if it does not game the metric or touch the constitution."""
    import os

    payload = _as_dict(node_input)
    proposed = str(payload.get("playbook") or "")
    outcome = str(Tier1(ctx).get("run_outcome") or payload.get("work_status") or "")
    proxy = Tier1(ctx).get("run_proxy_score")
    scored = score_run(ctx, outcome)
    if isinstance(proxy, (int, float)):
        scored = RunScore(outcome=scored.outcome, proxy=float(proxy), rejections=scored.rejections)

    scope = scope_key(ctx)
    current_behavior = None
    candidate_behavior = None
    if settings.behavioral_audit_enabled and settings.project:
        import asyncio

        from app.evolve.behavioral import evaluate_behavioral

        current = get_store(scope).snapshot()
        current_behavior, candidate_behavior = await asyncio.gather(
            evaluate_behavioral(current.text),
            evaluate_behavioral(proposed),
        )
    verdict = audit_proposal(
        proposed,
        run_score=scored,
        scope=scope,
        current_behavior=current_behavior,
        candidate_behavior=candidate_behavior,
    )
    result = {
        **payload,
        "work_status": payload.get("status") or outcome,
        **verdict.as_dict(),
        "hypothesis": payload.get("hypothesis", ""),
    }

    if verdict.accepted:
        await persist_playbook(ctx, get_store(scope).snapshot())
        if not os.environ.get("PYTEST_CURRENT_TEST"):
            try:
                get_store(scope).save(_playbook_disk_path())
            except OSError as exc:
                logger.warning("Playbook file not saved: %s", exc)

    Tier2(ctx).append(
        LedgerEntry(
            step="evolve",
            keys_written=[],
            bytes_pruned=0,
            summary=(
                f"{verdict.reason} gen={verdict.generation} "
                f"proxy={verdict.proxy_score:.2f} held-out={verdict.heldout_score:.2f}"
            ),
            status=result["status"],
        )
    )
    logger.info("Playbook audit: %s (generation %d)", verdict.reason, verdict.generation)
    return result


async def evolution_skipped(ctx: Context, node_input: Any = None) -> dict[str, Any]:
    """Terminal when ARMCL_EVOLVE is off. The work outcome is unchanged."""
    payload = _as_dict(node_input)
    playbook = get_store(scope_key(ctx)).snapshot()
    return {
        **payload,
        "status": payload.get("status", "EVOLUTION_SKIPPED"),
        "evolution": "skipped",
        "playbook_generation": playbook.generation,
    }


# ---------------------------------------------------------------------------
# Graph assembly
# ---------------------------------------------------------------------------

screen_node = node(screen_request, name="screen_request", retry_config=_STANDARD_RETRY)
quarantine_node = node(quarantine, name="quarantine")
assessment_router = node(route_assessment, name="route_assessment")
approval_router = node(route_approval, name="route_approval")
judgement_router = node(route_judgement, name="route_judgement")
decline_node = node(log_decline, name="log_decline")
denied_node = node(approval_denied, name="approval_denied")
complete_node = node(complete, name="complete")
halted_node = node(circuit_broken, name="circuit_broken")
score_node = node(record_score, name="record_score")
audit_node = node(audit_playbook, name="audit_playbook")
skip_evolve_node = node(evolution_skipped, name="evolution_skipped")

root_workflow = Workflow(
    name="fluffy_memory_fleet",
    description=(
        "Fortified enterprise fleet: screens inbound content, discovers and assesses "
        "candidates, acts under policy, and verifies its own work. State continuity "
        "across steps and sessions is provided by ARMCL. After a run it may rewrite "
        "its own tactics; a deterministic auditor refuses rewrites that game the score."
    ),
    edges=[
        (START, screen_node),
        (screen_node, {"BLOCKED": quarantine_node, "CLEAN": scout_agent}),
        (scout_agent, analyst_agent, assessment_router),
        # No DEFAULT_ROUTE entry: both routers normalise every unrecognised
        # value onto a named route before emitting, so a default edge would
        # duplicate an existing target and the graph validator rejects that.
        # The fallback lives in the router, where it can also be logged.
        (
            assessment_router,
            {
                "ACT": executor_agent,
                "DECLINE": decline_node,
                "NEEDS_HUMAN": approver_agent,
            },
        ),
        # The approval gate routes on the operator's answer. An unconditional
        # edge to the executor here would let the fleet act on a refusal.
        (approver_agent, approval_router),
        (
            approval_router,
            {
                "APPROVED": executor_agent,
                "DENIED": denied_node,
            },
        ),
        (executor_agent, critic_agent, judgement_router),
        (
            judgement_router,
            {
                "ACCEPT": complete_node,
                "REJECT": analyst_agent,
                "HALT": halted_node,
            },
        ),
        # Work terminals funnel into the evolution loop. Quarantine does not:
        # blocked input is not a lesson about tactics.
        (complete_node, score_node),
        (decline_node, score_node),
        (halted_node, score_node),
        (denied_node, score_node),
        (
            score_node,
            {
                "EVOLVE": evolver_agent,
                "SKIP": skip_evolve_node,
            },
        ),
        (evolver_agent, audit_node),
    ],
)

root_agent = root_workflow
"""ADK CLI and AdkApp both look for `root_agent`."""

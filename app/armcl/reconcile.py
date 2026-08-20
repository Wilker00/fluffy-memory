"""Post-action reconciliation: the second half of the ARMCL loop.

After a node produces output, ARMCL distils it into structured facts, writes
them to the tier their salience warrants, and drops the raw payload. Without
this, every step's full output accumulates in context until the window
overflows; with it, what survives is a handful of facts plus a summary.

Durable facts additionally go to Tier 3 so a later session can act on them.
That write is what makes the cross-session recall demo possible: a constraint
learned in run one changes the decision in run three.
"""

from __future__ import annotations

import logging
from typing import Any

from app.armcl.memory_backend import make_entry
from app.armcl.policy import Salience, distill, redact, should_persist
from app.armcl.tiers import LedgerEntry, Tier1, Tier2
from app.observability.spans import armcl_span
from app.settings import settings

logger = logging.getLogger(__name__)


async def reconcile(
    ctx: Any,
    *,
    step: str,
    raw_output: Any,
    status: str = "SUCCESS",
) -> LedgerEntry:
    """Distil raw output into state deltas and route them to the right tier."""
    t1 = Tier1(ctx)
    t2 = Tier2(ctx)

    with armcl_span("reconcile", step=step) as span:
        result = distill(raw_output, source_step=step)

        # Tier 1: everything non-ephemeral becomes available to the next step.
        carried = {d.key: d.value for d in result.deltas if d.salience is not Salience.EPHEMERAL}
        keys_written = t1.merge(carried)

        # Tier 2: append to the episodic ledger.
        entry = LedgerEntry(
            step=step,
            keys_written=keys_written,
            bytes_pruned=result.bytes_pruned,
            summary=result.summary,
            # The compact ledger must retain typed values, not merely a display
            # summary. Hydration uses this map to restore an exact missing value
            # after a cold resume.
            values=carried,
            status=status,
        )
        t2.append(entry)

        # Tier 3: only facts that will change a future decision.
        await _apply_optional_triage(result.deltas)
        durable = [d for d in result.deltas if should_persist(d)]
        if durable:
            await _persist_durable(ctx, step=step, deltas=durable)

        try:
            span.set_attribute("armcl.bytes_pruned", result.bytes_pruned)
            span.set_attribute("armcl.compression_ratio", round(result.compression_ratio, 4))
            span.set_attribute("armcl.keys_written", ",".join(keys_written[:20]))
            span.set_attribute("armcl.durable_written", len(durable))
        except Exception:  # noqa: BLE001 - telemetry must not break execution
            pass

    logger.info(
        "ARMCL reconciled %s: %d keys, %d bytes pruned, %d durable",
        step,
        len(keys_written),
        result.bytes_pruned,
        len(durable),
    )
    return entry


async def _apply_optional_triage(deltas: list) -> None:
    """Let the optional Gemma classifier promote prose constraints.

    A no-op unless ARMCL_GEMMA_TRIAGE=true. It can only promote EPISODIC to
    DURABLE, never demote, so the heuristic remains the floor.
    """
    from app.bonus.gemma_triage import enabled, triage

    if not enabled():
        return

    for delta in deltas:
        outcome = await triage(delta.key, delta.value, delta.salience)
        if outcome.promoted:
            delta.salience = outcome.salience
            delta.reason = f"{delta.reason}; promoted by gemma triage"


async def _persist_durable(ctx: Any, *, step: str, deltas: list) -> None:
    """Write durable facts to Tier 3, redacted.

    Redaction happens here rather than at read time because Tier 3 outlives the
    session: a secret written once would remain retrievable indefinitely.
    """
    add_memory = getattr(ctx, "add_memory", None)
    if add_memory is None:
        logger.debug("No memory service bound; skipping Tier 3 write for %s", step)
        return

    entries = [
        make_entry(
            redact(f"{d.key}: {d.value}"),
            author="armcl",
            step=step,
            salience=d.salience.value,
            reason=d.reason,
        )
        for d in deltas
    ]

    try:
        await add_memory(memories=entries)
    except Exception as exc:  # noqa: BLE001
        # A Tier 3 write failure degrades future recall but must not fail the
        # step that already succeeded. Surfaced in the span and the log.
        logger.warning("ARMCL Tier 3 write failed for %s: %s", step, exc)


def make_reconciling_callback(step: str):
    """Build an ADK `after_agent_callback` that reconciles the agent's output."""

    async def _callback(ctx: Any):
        output = getattr(ctx, "output", None)
        if output is None:
            session = getattr(ctx, "session", None)
            events = getattr(session, "events", None) if session else None
            if events:
                output = getattr(events[-1], "output", None)

        if output is not None:
            await reconcile(ctx, step=step, raw_output=output)

        await _sync_session_to_memory(ctx)
        return None

    return _callback


async def _sync_session_to_memory(ctx: Any) -> None:
    """Hand recent events to Memory Bank for its own extraction pass.

    ARMCL's distillation is deterministic and key-shaped; Memory Bank's
    extraction is semantic and catches things a key heuristic will not. Running
    both means the durable tier gets structured facts plus conversational
    nuance. The docs recommend incremental event submission over whole-session
    submission to avoid reprocessing the same events on every turn.
    """
    # Raw events may contain user text or tool payloads that deliberately did
    # not survive ARMCL's redaction/distillation pass. Keep semantic extraction
    # opt-in; explicit redacted durable facts remain the default memory path.
    if not settings.sync_raw_session_events:
        return

    add_events = getattr(ctx, "add_events_to_memory", None)
    session = getattr(ctx, "session", None)
    if add_events is None or session is None:
        return

    events = getattr(session, "events", []) or []
    if not events:
        return

    window = events[-settings.reconcile_window :]
    try:
        await add_events(events=window)
    except Exception as exc:  # noqa: BLE001
        logger.warning("ARMCL incremental memory sync failed: %s", exc)

"""Run-level outcome records: promoting how a run ended into durable memory.

Tier 2 already labels each step with how it was finally judged — that is what
`record_outcome` and `label_pending` produce. But Tier 2 is session state, so
the labels die with the task. Every run therefore began with a fresh opinion of
its own competence: the ledger knew an approach had been refused three times,
and the next run knew nothing about it.

This module closes that loop. At a terminal node the labelled ledger is
condensed into a single trajectory record and written to Tier 3, where
hydration surfaces it on subsequent runs. The two durable kinds answer
different questions:

  constraints   what a later run is not allowed to do
  trajectories  what has already been tried, and how it went

A trajectory record states facts and stops there. The instruction to weigh a
prior failure lives in the agent prompts, not in memory, because durable
storage filled with imperatives becomes a second uncontrolled policy surface —
and unlike a prompt, nothing reviews it before it binds the next run.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from app.armcl.memory_backend import make_entry
from app.armcl.policy import redact, render_value
from app.armcl.tiers import Tier1, Tier2
from app.observability.spans import armcl_span

logger = logging.getLogger(__name__)

TRAJECTORY_MARKER = "[PRIOR RUN OUTCOME]"
"""Prefix identifying a trajectory record in Tier 3.

Detection is by content rather than by `custom_metadata` on purpose. Metadata
round-trips through the local Chroma backend but is not guaranteed to survive
Memory Bank's own extraction and re-storage, and a classifier that works
offline and silently fails in production is worse than no classifier. The
marker is part of the text, so every backend preserves it.
"""

MAX_STEPS_RECORDED = 12
"""Step path length cap. A longer path is a cycle, and its shape is the signal."""

MAX_FAILED_APPROACHES = 3
"""How many refused approaches to carry forward. The most recent are the useful ones."""

_APPROACH_CAP = 80
"""Per-approach render cap, so one long artifact cannot dominate the record."""


@dataclass
class TrajectoryRecord:
    """How one run ended, in the form a later run can act on."""

    item_id: str
    outcome: str
    steps: list[str] = field(default_factory=list)
    attempts_refused: int = 0
    failed_approaches: list[str] = field(default_factory=list)

    def render(self) -> str:
        """Format as a single line.

        One line because the context frame renders each retrieved fact as a
        bullet with a per-value cap; a multi-line record would be truncated
        mid-sentence and would break the indentation of everything after it.
        """
        parts = [f"{TRAJECTORY_MARKER} {self.item_id}: {self.outcome}"]

        if self.steps:
            parts.append(f"path {' > '.join(_collapse_runs(self.steps))}")
        if self.attempts_refused:
            parts.append(f"{self.attempts_refused} attempt(s) refused by the critic")
        if self.failed_approaches:
            refused = ", ".join(
                f'"{render_value(a, _APPROACH_CAP)}"' for a in self.failed_approaches
            )
            parts.append(f"already refused: {refused}")

        return "; ".join(parts) + "."


def _collapse_runs(steps: list[str]) -> list[str]:
    """Fold consecutive repeats of a step into `step xN`.

    A retry cycle produces the same step several times in a row, and spelling
    each one out costs characters without adding information. The record holds
    the uncollapsed path; this only affects how it reads.
    """
    folded: list[str] = []
    for step in steps:
        previous = folded[-1] if folded else ""
        base, _, count = previous.rpartition(" x")
        if base == step and count.isdigit():
            folded[-1] = f"{step} x{int(count) + 1}"
        elif previous == step:
            folded[-1] = f"{step} x2"
        else:
            folded.append(step)
    return folded


def summarize_trajectory(ctx: Any, *, outcome: str) -> TrajectoryRecord:
    """Condense the labelled Tier 2 ledger into one record."""
    t1 = Tier1(ctx)
    t2 = Tier2(ctx)
    entries = t2.all_entries()

    return TrajectoryRecord(
        item_id=str(t1.get("primary_item_id") or ""),
        outcome=outcome,
        steps=[e.step for e in entries][-MAX_STEPS_RECORDED:],
        # Counted from the ledger rather than read from the circuit breaker,
        # because the breaker is reset on ACCEPT. A run that succeeded on its
        # third attempt would otherwise record zero refusals, which is the one
        # thing about it worth remembering.
        attempts_refused=sum(1 for e in entries if e.outcome == "REJECTED"),
        failed_approaches=t2.rejected_artifacts[-MAX_FAILED_APPROACHES:],
    )


async def persist_trajectory(ctx: Any, *, outcome: str) -> TrajectoryRecord | None:
    """Write this run's outcome to Tier 3.

    Args:
        ctx: ADK Context, or anything exposing `state` and `add_memory`.
        outcome: Terminal state of the run, e.g. COMPLETED or HALTED.

    Returns:
        The record written, or None when nothing reached durable memory —
        either because the run has no subject to attribute the outcome to, or
        because the write failed. Both mean a later run will not see it.
    """
    record = summarize_trajectory(ctx, outcome=outcome)

    # Admission control. An outcome that names no item cannot be retrieved by
    # a later run assessing that item, so it would sit in Tier 3 matching
    # everything and informing nothing. Quarantine is the real case: it fires
    # before the scout has identified anything.
    if not record.item_id:
        logger.debug("No item_id for %s; trajectory not persisted", outcome)
        return None

    add_memory = getattr(ctx, "add_memory", None)
    if add_memory is None:
        logger.debug("No memory service bound; trajectory not persisted for %s", outcome)
        return None

    with armcl_span("persist_trajectory", outcome=outcome, item_id=record.item_id) as span:
        entry = make_entry(
            redact(record.render()),
            author="armcl",
            kind="trajectory",
            outcome=outcome,
            item_id=record.item_id,
        )
        try:
            await add_memory(memories=[entry])
        except Exception as exc:  # noqa: BLE001
            # The run has already reached its terminal state. Losing the
            # lesson degrades the next run; failing here would discard the
            # outcome of this one for no gain.
            logger.warning("ARMCL trajectory write failed for %s: %s", record.item_id, exc)
            return None

        try:
            span.set_attribute("armcl.attempts_refused", record.attempts_refused)
            span.set_attribute("armcl.steps_recorded", len(record.steps))
        except Exception:  # noqa: BLE001 - telemetry must not break execution
            pass

    logger.info(
        "ARMCL recorded trajectory for %s: %s after %d refused attempt(s)",
        record.item_id,
        outcome,
        record.attempts_refused,
    )
    return record


def is_trajectory(fact: str) -> bool:
    """True when a Tier 3 hit is a run outcome rather than a constraint."""
    return fact.lstrip().startswith(TRAJECTORY_MARKER)


def partition(facts: list[str]) -> tuple[list[str], list[str]]:
    """Split Tier 3 hits into constraints and prior outcomes.

    The two are kept apart in the context frame because they carry different
    authority. A constraint is binding; a prior outcome is evidence. Rendering
    them in one list invites an agent to treat "this failed last time" as a
    prohibition, or a standing policy as merely something that happened once.

    Returns:
        `(constraints, prior_outcomes)`, with the marker stripped from the
        outcomes since the frame already labels the section.
    """
    constraints: list[str] = []
    prior_outcomes: list[str] = []

    for fact in facts:
        if is_trajectory(fact):
            prior_outcomes.append(fact.lstrip()[len(TRAJECTORY_MARKER) :].strip())
        else:
            constraints.append(fact)

    return constraints, prior_outcomes

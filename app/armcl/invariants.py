"""Deterministic structural checks over the executor's work.

The critic is a language model, so using it as the only acceptance signal
means the check that catches hallucination is itself capable of hallucinating.
These invariants are the floor underneath it: pure functions over reconciled
state, no model call, same answer every time.

They are deliberately domain-agnostic. Each asserts something about the
*shape* of a state transition rather than about maintenance or any other
subject matter, so they keep working when the synthetic workload is replaced.
Domain-specific correctness is the `verify()` method's job on the adapter.

A violation is a hard veto. The critic may reject work the invariants allow,
but it cannot accept work they forbid, which means a confidently wrong ACCEPT
cannot reach the completion node.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from app.armcl.policy import is_bulk
from app.armcl.tiers import Tier1, Tier2

logger = logging.getLogger(__name__)


@dataclass
class InvariantReport:
    """Outcome of the deterministic checks for one execution attempt."""

    violations: list[str] = field(default_factory=list)
    checks_run: int = 0

    @property
    def passed(self) -> bool:
        return not self.violations

    def summary(self) -> str:
        if self.passed:
            return f"{self.checks_run} structural checks passed"
        return f"{len(self.violations)} of {self.checks_run} structural checks failed"


def check_execution(ctx: Any) -> InvariantReport:
    """Verify the state transition the executor claims to have produced.

    Args:
        ctx: Any object exposing ADK's `state` mapping.

    Returns:
        A report listing every violation found. Checks are independent, so all
        of them run; reporting only the first would hide the rest from the
        analyst that has to fix them.
    """
    t1 = Tier1(ctx)
    t2 = Tier2(ctx)
    report = InvariantReport()

    item_id = t1.get("primary_item_id")
    artifact = t1.get("artifact")

    # 1. The action must name what it acted on. An artifact with no subject
    #    cannot be verified against anything.
    report.checks_run += 1
    if not item_id:
        report.violations.append("No primary_item_id in state; the action has no subject.")

    # 2. Execution must have produced something. An empty artifact means the
    #    executor reported success without evidence of work.
    report.checks_run += 1
    if artifact in (None, "", [], {}):
        report.violations.append("Executor produced no artifact; nothing to verify.")

    # 3. Re-proposing an artifact the critic already refused is a
    #    non-converging loop, detectable without another model call.
    report.checks_run += 1
    if artifact and str(artifact) in t2.rejected_artifacts:
        report.violations.append(
            f"Artifact {artifact!r} was already rejected on an earlier attempt; "
            "the retry made no change."
        )

    # 4. A bulk value in the scratchpad means distillation failed upstream and
    #    the context frame is about to carry raw payload into the next step.
    report.checks_run += 1
    bulk_keys = [key for key, value in t1.snapshot().items() if is_bulk(value)]
    if bulk_keys:
        report.violations.append(
            f"Undistilled bulk values in state: {', '.join(sorted(bulk_keys)[:5])}."
        )

    if not report.passed:
        logger.warning("Structural invariants failed: %s", "; ".join(report.violations))

    return report

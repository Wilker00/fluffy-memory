"""Proxy score for a finished run.

This is the number the evolver is allowed to see. It rewards completing work,
penalises critic rejections and circuit-breaker halts, and treats a constraint-
driven decline as partial credit.

It is a *proxy*. Always declining looks decent on this number because it never
halts and never gets rejected. The held-out fixtures in `auditor.py` are the
true objective, and they are deliberately absent from the evolver's prompt —
that is how the system catches metric gaming instead of teaching the rewriter
the answer.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.armcl.tiers import Tier2

# Outcome labels as terminal nodes actually emit them.
_OUTCOME_POINTS = {
    "COMPLETED": 1.0,
    "DECLINED": 0.7,
    "APPROVAL_DENIED": 0.4,
    "HALTED_CIRCUIT_BREAKER": 0.0,
    "QUARANTINED": 0.0,
}

_REJECTION_PENALTY = 0.15


@dataclass(frozen=True)
class RunScore:
    """How one run looks on the evolver's metric."""

    outcome: str
    proxy: float
    rejections: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "proxy_score": self.proxy,
            "rejections": self.rejections,
        }


def score_run(ctx: Any, outcome: str) -> RunScore:
    """Score a terminal payload against the proxy metric.

    Args:
        ctx: Anything exposing ARMCL's `state` mapping.
        outcome: Terminal `status` string from the node that just finished.
    """
    base = _OUTCOME_POINTS.get(str(outcome).upper().strip(), 0.0)
    rejections = Tier2(ctx).rejection_count
    # Completing after retries is still a completion, but the wasted cycles
    # are the signal the playbook is supposed to learn from.
    proxy = max(0.0, base - _REJECTION_PENALTY * rejections)
    return RunScore(outcome=str(outcome), proxy=round(proxy, 4), rejections=rejections)

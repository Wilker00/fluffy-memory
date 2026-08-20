"""THROWAWAY synthetic reference workload.

    THIS IS SCAFFOLDING, NOT A DELIVERABLE.

Its only job is to exercise ARMCL while the real domain is undecided, and to
give the test suite something deterministic to assert against. Shipping this as
the demo would cap the largest scoring criterion, because no judge is moved by
an agent that manages fictional equipment.

Delete this package once a real `DomainAdapter` exists.

Each behaviour below is deliberately shaped to create one condition ARMCL must
survive:

  dependency gap    `inspect` requires an item_id that only `discover` produces,
                    several steps earlier.
  noisy output      `inspect` returns ~4000 lines of telemetry around three
                    fields that matter.
  time gap          high-risk items demand approval, pausing the workflow so it
                    must resume from persisted state rather than memory.
  cross-session     UNIT-7 carries a constraint that should cause a DECLINE on a
    recall          later run once it is in Tier 3, even though the first run
                    could not have known it.
"""

from __future__ import annotations

import hashlib
import random

from app.tools.protocol import (
    ActionResult,
    Candidate,
    DomainAdapter,
    InspectionReport,
    VerificationResult,
    register_domain,
)

_CATALOG: dict[str, dict] = {
    "UNIT-3": {
        "title": "Coolant pump, bay 3",
        "risk": "low",
        "constraints": ["Routine maintenance window is any weekday before 1600."],
        "verify_ok": True,
    },
    "UNIT-7": {
        "title": "Primary intake manifold, bay 7",
        "risk": "high",
        # The fact that makes cross-session recall demonstrable. On run 1 the
        # fleet learns it; on a later run it should decline before acting.
        "constraints": [
            "Policy 14: UNIT-7 must never be serviced without a signed failover plan.",
            "Servicing UNIT-7 during peak hours is prohibited.",
        ],
        "verify_ok": True,
    },
    "UNIT-9": {
        "title": "Auxiliary sensor array, bay 9",
        "risk": "medium",
        "constraints": ["Calibration requires two consecutive clean readings."],
        # Fails verification the first time, so the critic has a genuine reason
        # to reject and the retry path is exercised rather than asserted.
        "verify_ok": False,
    },
}


class SyntheticFleetDomain(DomainAdapter):
    """Fictional equipment-maintenance domain. Deterministic given a seed."""

    name = "synthetic_reference"

    def __init__(self, seed: int = 1729) -> None:
        self._rng = random.Random(seed)
        self._verify_attempts: dict[str, int] = {}
        self._actions: dict[str, ActionResult] = {}

    async def discover(self, query: str, limit: int = 5) -> list[Candidate]:
        normalized = query.upper()
        requested = [item_id for item_id in _CATALOG if item_id in normalized]
        ordered_ids = requested or list(_CATALOG)
        results = [
            Candidate(
                item_id=item_id,
                title=spec["title"],
                summary=f"{spec['risk']} risk unit awaiting assessment",
                metadata={"risk": spec["risk"]},
            )
            for item_id in ordered_ids
            for spec in (_CATALOG[item_id],)
        ]
        return results[:limit]

    async def inspect(self, item_id: str) -> InspectionReport:
        spec = _CATALOG.get(item_id)
        if spec is None:
            # A real missing-resource error. Propagates so RetryConfig can see
            # it; wrapping it would defeat the framework's retry handling.
            raise KeyError(f"Unknown item_id {item_id!r}. Known: {sorted(_CATALOG)}")

        return InspectionReport(
            item_id=item_id,
            constraints=list(spec["constraints"]),
            facts={
                "risk_level": spec["risk"],
                "requires_approval": spec["risk"] == "high",
                "bay": item_id.split("-")[-1],
            },
            raw=self._telemetry(item_id),
        )

    async def act(self, item_id: str, plan: str, idempotency_key: str) -> ActionResult:
        if idempotency_key in self._actions:
            return self._actions[idempotency_key]
        spec = _CATALOG.get(item_id)
        if spec is None:
            raise KeyError(f"Unknown item_id {item_id!r}")

        digest = hashlib.sha256(f"{item_id}:{plan}".encode()).hexdigest()[:12]
        result = ActionResult(
            item_id=item_id,
            status="COMPLETED",
            artifact=f"workorder-{digest}",
            details={"plan": plan[:200], "risk": spec["risk"]},
        )
        self._actions[idempotency_key] = result
        return result

    async def verify(self, item_id: str, artifact: str) -> VerificationResult:
        spec = _CATALOG.get(item_id, {})
        attempt = self._verify_attempts.get(item_id, 0) + 1
        self._verify_attempts[item_id] = attempt

        # UNIT-9 fails once, then passes: a bounded reject loop, not an
        # infinite one, so the critic path is real but the demo still finishes.
        if not spec.get("verify_ok", True) and attempt == 1:
            return VerificationResult(
                item_id=item_id,
                accepted=False,
                reasons=["Only one clean reading recorded; calibration requires two."],
            )

        if not artifact:
            return VerificationResult(
                item_id=item_id, accepted=False, reasons=["No artifact produced."]
            )

        return VerificationResult(item_id=item_id, accepted=True, reasons=["Checks passed."])

    def _telemetry(self, item_id: str) -> str:
        """~4000 lines of plausible noise. This is what distillation removes."""
        rng = random.Random(f"{item_id}-telemetry")
        lines = [
            f"[{rng.randint(0, 23):02d}:{rng.randint(0, 59):02d}:{rng.randint(0, 59):02d}] "
            f"sensor={rng.choice(['temp', 'pressure', 'vibration', 'flow'])} "
            f"value={rng.uniform(0, 100):.3f} status=nominal"
            for _ in range(4000)
        ]
        return "\n".join(lines)

    def reset(self) -> None:
        """Clear per-run verification state so tests stay independent."""
        self._verify_attempts.clear()
        self._actions.clear()


DOMAIN = SyntheticFleetDomain()
register_domain(DOMAIN)

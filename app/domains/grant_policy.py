"""Authoritative, versioned institutional policy catalog for the demo domain."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ProgramPolicy:
    program_id: str
    revision: str
    title: str
    constraints: tuple[str, ...]
    gpa_floor: float


POLICIES = {
    "HARDTECH-2026": ProgramPolicy(
        program_id="HARDTECH-2026",
        revision="3.2",
        title="Advanced Hardware Research Grant",
        constraints=(
            "HARDTECH-2026 policy v3.2: a 3.20 GPA on a 4.00 scale is mandatory.",
            "HARDTECH-2026 policy v3.2: Calculus I or an approved equivalent is mandatory.",
            "HARDTECH-2026 policy v3.2: hands-on hardware-stack evidence is mandatory.",
            "HARDTECH-2026 policy v3.2: recommendations to advance require human approval.",
        ),
        gpa_floor=3.2,
    )
}


def get_policy(program_id: str) -> ProgramPolicy:
    policy = POLICIES.get(program_id.upper().strip())
    if policy is None:
        raise KeyError(f"Unknown program {program_id!r}. Known: {sorted(POLICIES)}")
    return policy

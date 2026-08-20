"""ARMCL: Autonomous Retrieval & Memory Context Loop.

An active state-reconciliation engine, not a passive key-value store. Two hooks
wrap every step of the fleet's execution:

  Pre-action hydration      What context is missing to execute this step?
                            Recover it from Tier 2/3 without asking the human.

  Post-action reconciliation What ground truth did this step create?
                            Distil it, tier it by salience, prune the raw bulk.

The tiers map onto ADK primitives rather than bespoke storage, so identical
code runs locally and on Agent Runtime with managed sessions and Memory Bank.
"""

from app.armcl.hydrate import hydrate, make_hydrating_instruction
from app.armcl.memory_backend import build_memory_service, memory_service_builder
from app.armcl.policy import Salience, StateDelta, distill, redact, should_persist
from app.armcl.reconcile import make_reconciling_callback, reconcile
from app.armcl.tiers import ContextFrame, LedgerEntry, Tier1, Tier2

__all__ = [
    "ContextFrame",
    "LedgerEntry",
    "Salience",
    "StateDelta",
    "Tier1",
    "Tier2",
    "build_memory_service",
    "distill",
    "hydrate",
    "make_hydrating_instruction",
    "make_reconciling_callback",
    "memory_service_builder",
    "reconcile",
    "redact",
    "should_persist",
]

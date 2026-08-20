"""Self-evolution: a bounded playbook rewrite with a metric the rewriter cannot see.

Trajectory memory records *what happened*. This package is the next step: the
fleet rewrites its own operational tactics so the next run starts with a
better plan, then a deterministic auditor refuses rewrites that climb the
proxy score by gaming it.

The constitution is not in this package. It stays in agent instructions and
in the graph.
"""

from app.evolve.auditor import AuditVerdict, audit_proposal, evaluate_heldout
from app.evolve.behavioral import BehavioralEvaluation, evaluate_behavioral, score_decisions
from app.evolve.playbook import (
    PLAYBOOK_MARKER,
    Playbook,
    ensure_playbook,
    get_store,
    is_playbook,
    parse_playbook_fact,
    persist_playbook,
    reset_store,
    scope_key,
)
from app.evolve.score import RunScore, score_run

__all__ = [
    "PLAYBOOK_MARKER",
    "AuditVerdict",
    "BehavioralEvaluation",
    "Playbook",
    "RunScore",
    "audit_proposal",
    "ensure_playbook",
    "evaluate_heldout",
    "evaluate_behavioral",
    "get_store",
    "is_playbook",
    "parse_playbook_fact",
    "persist_playbook",
    "reset_store",
    "score_run",
    "score_decisions",
    "scope_key",
]

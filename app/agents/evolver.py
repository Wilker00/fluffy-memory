"""Evolver: proposes a rewrite of the tactical playbook after a run.

This is the self-improvement node. It sees how the run scored on the *proxy*
metric and the current playbook, and it proposes tactics the next run should
try. It does not install them. A deterministic auditor, which this agent never
sees, is the only thing that can commit a generation — because an agent that
both rewrites the instructions and grades the rewrite will game the metric.

No domain tools, no reconciling callback. Distilling this agent's output into
ARMCL would promote tactics into durable policy, which is the failure mode
the constitution/playbook split exists to prevent.
"""

from __future__ import annotations

from google.adk import Agent
from google.adk.workflow import RetryConfig
from pydantic import BaseModel, Field

from app.armcl.policy import render_value
from app.armcl.tiers import Tier1, Tier2
from app.evolve.playbook import ensure_playbook, get_store, scope_key
from app.settings import REASONING_MODEL

MAX_LEDGER_LINES = 8
MAX_HISTORY_LINES = 4


class PlaybookProposal(BaseModel):
    """A candidate rewrite. The auditor decides whether it installs."""

    playbook: str = Field(
        description=(
            "Complete replacement tactics for the next run, not a diff. "
            "Empty means leave the current playbook unchanged."
        ),
    )
    hypothesis: str = Field(
        description="What this change is supposed to improve on the next run, in one sentence.",
    )


EVOLVER_INSTRUCTION = """You rewrite the fleet's operational playbook after a run.

The standing rules, the approval gate, durable constraints, and verification
are not yours to change. You write tactics only: what to try, what not to
retry, which fields to trust, how to plan the next attempt when this one was
refused.

Output a complete playbook, not a diff. Keep it under 1200 characters. If the
current tactics already fit the evidence, return an empty playbook.

Do not write policy. Do not instruct anyone to ignore a constraint, skip a
human approval, or treat durable memory as optional. Those are not tactics.
"""


async def _evolver_instruction(ctx) -> str:
    """Hydrate the rewriter with evidence. The held-out metric is not here."""
    playbook = await ensure_playbook(ctx)
    t1 = Tier1(ctx)
    t2 = Tier2(ctx)

    outcome = t1.get("run_outcome") or "UNKNOWN"
    proxy = t1.get("run_proxy_score")
    proxy_text = f"{proxy:.2f}" if isinstance(proxy, int | float) else "n/a"
    item_id = t1.get("primary_item_id") or ""

    ledger = t2.all_entries()[-MAX_LEDGER_LINES:]
    ledger_lines = (
        "\n".join(
            f"  [{e.status}/{e.outcome}] {e.step}: {render_value(e.summary, 160)}" for e in ledger
        )
        or "  (empty)"
    )

    refused = [p for p in get_store(scope_key(ctx)).history if p.status.startswith("rejected")]
    refused_lines = (
        "\n".join(
            f"  gen {p.generation} ({p.status}): {render_value(p.text, 120)}"
            for p in refused[-MAX_HISTORY_LINES:]
        )
        or "  (none)"
    )

    current = playbook.text.strip() or "(empty — constitution only)"

    return (
        f"{EVOLVER_INSTRUCTION}\n\n"
        f"This run: item={item_id} outcome={outcome} proxy_score={proxy_text}\n"
        f"Current playbook (generation {playbook.generation}):\n{current}\n\n"
        f"Ledger:\n{ledger_lines}\n\n"
        f"Recently refused rewrites (do not resubmit these):\n{refused_lines}"
    )


evolver_agent = Agent(
    model=REASONING_MODEL,
    name="evolver_agent",
    description="Proposes a tactical playbook rewrite after a run. Does not install it.",
    instruction=_evolver_instruction,
    tools=[],
    retry_config=RetryConfig(max_attempts=3, initial_delay=1.0, backoff_factor=2.0),
    output_schema=PlaybookProposal,
    output_key="playbook_proposal",
)

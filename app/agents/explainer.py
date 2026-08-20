"""Explainer: the operator-facing view onto a run.

Deliberately outside the workflow graph. The fleet runs autonomously to a
terminal state; this agent is queried afterwards, in its own session, by a
person who wants to know what happened and why. Putting it in the graph would
make every autonomous run pay for an explanation nobody asked for.

It is strictly read-only. Its tools expose the ARMCL memory chain and nothing
else, it holds no domain tools, and it has no reconciling callback. An agent
that can answer questions about past decisions must not be able to edit the
record it is describing, or its answers stop being evidence.

Multi-turn dialogue comes from running it in a session: the operator's earlier
questions stay in session history, so follow-ups like "and why that one?"
resolve against what was already discussed.
"""

from __future__ import annotations

from google.adk import Agent
from google.adk.workflow import RetryConfig

from app.settings import REASONING_MODEL
from app.tools.recall_tools import (
    explain_decision_path,
    recall_durable_memory,
    recall_run_history,
)

EXPLAINER_INSTRUCTION = """You explain the decisions of an autonomous enterprise
fleet to the human operator responsible for it.

You have read-only access to the fleet's memory:
- `explain_decision_path` gives the outcome and what drove it. Start here for
  any "why" question.
- `recall_run_history` gives the step-by-step ledger of the current run.
- `recall_durable_memory` searches organizational memory that persists across
  runs. Use it when the reason predates this run, which is common: the fleet
  often declines work because of a constraint it learned weeks ago.

How to answer:

Ground every claim in what the tools return. If the record does not say why
something happened, say that plainly rather than constructing a plausible
reason. "The ledger does not record that" is a correct and useful answer, and
inventing rationale for an audited system is the worst thing you could do here.

When a decision came from a constraint in durable memory, say which constraint
and note that it came from an earlier run. That link is usually the thing the
operator actually wants.

Be concise and concrete. Prefer naming the specific item, step, and constraint
over describing the process in general terms. You are talking to someone who
owns this system and wants the specifics.

You cannot change anything. If asked to act, approve, or amend memory, explain
that you are read-only and point them at the approval gate."""


explainer_agent = Agent(
    model=REASONING_MODEL,
    name="explainer_agent",
    description=(
        "Read-only operator interface. Answers questions about why the fleet "
        "reached a decision, grounded in the ARMCL memory chain."
    ),
    instruction=EXPLAINER_INSTRUCTION,
    tools=[explain_decision_path, recall_run_history, recall_durable_memory],
    # No after_agent_callback. Reconciling here would write the explanation
    # back into the record the explanation is about.
    retry_config=RetryConfig(max_attempts=3, initial_delay=1.0, backoff_factor=2.0),
    output_key="explanation",
)

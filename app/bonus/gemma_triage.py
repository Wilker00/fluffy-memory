"""Gemma-assisted salience triage.

ARMCL's default classifier is a deterministic key-shape heuristic. It is fast
and free, which is why it runs after every tool call, but it is genuinely blind
to one case: a constraint expressed in prose under an unremarkable key.

    {"notes": "Vendors in the EU require a signed DPA before onboarding."}

`notes` looks episodic. The sentence is durable policy.

Gemma is a good fit for exactly this. It is small and cheap enough to run on
every ambiguous field without the cost profile of the main reasoning model, and
the task is narrow classification rather than open reasoning.

This is strictly an enhancement. It is off unless `ARMCL_GEMMA_TRIAGE=true`,
only ever *promotes* a fact the heuristic already saw, and falls back silently
when unavailable. ARMCL is fully functional without it.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from app.armcl.policy import Salience, is_bulk

logger = logging.getLogger(__name__)

GEMMA_MODEL = "gemma-3-27b-it"

_TRIAGE_PROMPT = """You classify whether a single fact should become permanent \
organizational policy.

Answer DURABLE only if the value states a rule, constraint, limit, eligibility \
condition, or prohibition that should influence decisions on FUTURE, unrelated \
tasks.

Answer EPISODIC for anything specific to the current task: identifiers, \
measurements, statuses, timestamps, names, or one-off observations.

Reply with exactly one word: DURABLE or EPISODIC.

Field name: {key}
Value: {value}"""


@dataclass
class TriageResult:
    salience: Salience
    source: str
    """Which classifier decided: 'heuristic' or 'gemma'."""
    promoted: bool = False


def enabled() -> bool:
    return os.environ.get("ARMCL_GEMMA_TRIAGE", "").strip().lower() == "true"


async def triage(key: str, value: object, heuristic: Salience) -> TriageResult:
    """Optionally upgrade an EPISODIC classification to DURABLE.

    Only ambiguous prose is sent to the model. Identifiers, bulk, and anything
    the heuristic already called DURABLE are decided locally, so the model is
    consulted for a small fraction of fields.
    """
    if not enabled() or heuristic is not Salience.EPISODIC:
        return TriageResult(heuristic, "heuristic")

    if not isinstance(value, str) or is_bulk(value):
        return TriageResult(heuristic, "heuristic")

    # Prose, not an identifier. A short token without spaces is an id.
    if len(value.split()) < 4:
        return TriageResult(heuristic, "heuristic")

    verdict = await _ask_gemma(key, value)
    if verdict is Salience.DURABLE:
        logger.info("Gemma promoted %r to durable memory", key)
        return TriageResult(Salience.DURABLE, "gemma", promoted=True)

    return TriageResult(heuristic, "gemma")


async def _ask_gemma(key: str, value: str) -> Salience | None:
    """Single classification call. Never raises; returns None on failure."""
    try:
        from google import genai

        client = genai.Client()
        response = await client.aio.models.generate_content(
            model=GEMMA_MODEL,
            contents=_TRIAGE_PROMPT.format(key=key, value=value[:500]),
            config={"temperature": 0.0, "max_output_tokens": 8},
        )
        answer = (response.text or "").strip().upper()
        if "DURABLE" in answer:
            return Salience.DURABLE
        if "EPISODIC" in answer:
            return Salience.EPISODIC
        logger.debug("Gemma returned an unparseable verdict: %r", answer)
        return None

    except Exception as exc:  # noqa: BLE001
        # An enhancement that breaks the pipeline is worse than no enhancement.
        logger.debug("Gemma triage unavailable, keeping heuristic: %s", exc)
        return None

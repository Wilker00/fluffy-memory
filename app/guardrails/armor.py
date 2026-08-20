"""Model Armor screening for content crossing a trust boundary.

Two rules shape this module.

First, screening returns a verdict; it never raises. ADK 2.0 catches exceptions
from nodes in order to apply RetryConfig, so raising on a blocked document
would re-run the block two more times, triple the cost, and log three failures
for one malicious input. A blocked document is a *decision*, not a transient
fault, so it travels as data and the workflow graph routes on it.

Second, the offline fallback is named honestly. `GUARDRAIL_BACKEND=regex`
selects a heuristic pattern matcher that is emphatically not Model Armor. It
exists so the fleet stays runnable without cloud access. Every verdict carries
the backend that produced it, and the demo should show `model_armor`.
"""

from __future__ import annotations

import logging
import re
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field

from app.settings import settings

logger = logging.getLogger(__name__)


class VerdictState(str, Enum):
    CLEAN = "CLEAN"
    BLOCKED = "BLOCKED"
    UNAVAILABLE = "UNAVAILABLE"
    """Screening could not run. Fail closed for inbound, open for outbound."""


class GuardrailVerdict(BaseModel):
    """Result of screening one payload."""

    state: VerdictState
    backend: Literal["model_armor", "regex"] = "model_armor"
    filters_matched: list[str] = Field(default_factory=list)
    sanitized_text: str | None = None
    detail: str = ""

    @property
    def is_blocked(self) -> bool:
        return self.state is VerdictState.BLOCKED

    @property
    def route(self) -> str:
        """Route key for the workflow graph."""
        return "BLOCKED" if self.is_blocked else "CLEAN"

    def summary(self) -> str:
        if self.is_blocked:
            return f"BLOCKED by {self.backend}: {', '.join(self.filters_matched) or self.detail}"
        return f"CLEAN ({self.backend})"


# Heuristics for the offline fallback. Narrow on purpose: this is a stand-in
# for a real classifier, and a broad pattern set would produce false confidence.
_INJECTION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "instruction_override",
        re.compile(r"(?i)ignore\s+(all\s+)?(previous|prior|above)\s+instruction"),
    ),
    ("instruction_override", re.compile(r"(?i)disregard\s+(all\s+)?(previous|prior|your)\s+")),
    ("role_hijack", re.compile(r"(?i)you\s+are\s+now\s+(a|an|the)\s+")),
    ("role_hijack", re.compile(r"(?i)system\s*(prompt|override|message)\s*:")),
    (
        "exfiltration",
        re.compile(
            r"(?i)(reveal|print|output|show)\s+(your|the)\s+(system\s+)?(prompt|instruction)"
        ),
    ),
    ("exfiltration", re.compile(r"(?i)send\s+(all\s+)?(data|memory|context)\s+to\s+http")),
    ("tool_poisoning", re.compile(r"(?i)(call|invoke|execute)\s+the\s+\w+\s+tool\s+with")),
    ("destructive_command", re.compile(r"(?i)\b(drop\s+table|rm\s+-rf|sudo\s+su|DELETE\s+FROM)\b")),
)


async def screen_inbound(text: str, *, context: str = "inbound") -> GuardrailVerdict:
    """Screen untrusted content entering the fleet.

    Fails closed: if screening is unavailable, the payload is treated as
    blocked. Untrusted input reaching an agent unscreened is exactly the
    indirect prompt-injection path this guards.
    """
    if not text or not text.strip():
        return GuardrailVerdict(state=VerdictState.CLEAN, backend=_backend(), detail="empty")

    if settings.guardrail_backend == "regex":
        return _screen_regex(text, context=context)

    verdict = await _screen_model_armor(text, context=context, direction="prompt")
    if verdict.state is VerdictState.UNAVAILABLE:
        logger.error("Model Armor unavailable on inbound path; failing closed for %s", context)
        return verdict.model_copy(
            update={
                "state": VerdictState.BLOCKED,
                "detail": f"{verdict.detail} (failed closed on inbound)",
            }
        )
    return verdict


async def screen_outbound(text: str, *, context: str = "outbound") -> GuardrailVerdict:
    """Screen model output before it leaves the trust boundary.

    Fails open: a screening outage should not silently discard work the fleet
    already did. The verdict records that screening did not run, so the gap is
    visible in the trace rather than hidden.
    """
    if not text or not text.strip():
        return GuardrailVerdict(state=VerdictState.CLEAN, backend=_backend(), detail="empty")

    if settings.guardrail_backend == "regex":
        return _screen_regex(text, context=context)

    verdict = await _screen_model_armor(text, context=context, direction="response")
    if verdict.state is VerdictState.UNAVAILABLE:
        logger.warning("Model Armor unavailable on outbound path; failing open for %s", context)
    return verdict


def _backend() -> Literal["model_armor", "regex"]:
    return "regex" if settings.guardrail_backend == "regex" else "model_armor"


def _screen_regex(text: str, *, context: str) -> GuardrailVerdict:
    matched = sorted({name for name, pattern in _INJECTION_PATTERNS if pattern.search(text)})
    if matched:
        return GuardrailVerdict(
            state=VerdictState.BLOCKED,
            backend="regex",
            filters_matched=matched,
            detail=f"heuristic match in {context}",
        )
    return GuardrailVerdict(
        state=VerdictState.CLEAN,
        backend="regex",
        sanitized_text=text,
        detail=f"no heuristic match in {context}",
    )


async def _screen_model_armor(
    text: str, *, context: str, direction: Literal["prompt", "response"]
) -> GuardrailVerdict:
    """Call the Model Armor sanitize API.

    Network and configuration failures return UNAVAILABLE so the caller can
    apply the right fail direction. They are not re-raised, because ADK would
    retry them as if they were transient node failures.
    """
    try:
        from google.api_core.client_options import ClientOptions
        from google.cloud import modelarmor_v1
    except ImportError as exc:
        return GuardrailVerdict(state=VerdictState.UNAVAILABLE, detail=f"SDK missing: {exc}")

    try:
        client = modelarmor_v1.ModelArmorClient(
            transport="rest",
            client_options=ClientOptions(api_endpoint=settings.model_armor_endpoint),
        )
        template = settings.model_armor_template_path

        if direction == "prompt":
            request = modelarmor_v1.SanitizeUserPromptRequest(
                name=template,
                user_prompt_data=modelarmor_v1.DataItem(text=text),
            )
            response = client.sanitize_user_prompt(request=request)
        else:
            request = modelarmor_v1.SanitizeModelResponseRequest(
                name=template,
                model_response_data=modelarmor_v1.DataItem(text=text),
            )
            response = client.sanitize_model_response(request=request)

        return _interpret(response, text=text, context=context)

    except Exception as exc:  # noqa: BLE001 - see docstring
        logger.warning("Model Armor call failed for %s: %s", context, exc)
        return GuardrailVerdict(
            state=VerdictState.UNAVAILABLE,
            detail=f"{type(exc).__name__}: {str(exc)[:200]}",
        )


def _interpret(response, *, text: str, context: str) -> GuardrailVerdict:
    """Translate a Model Armor sanitization result into a verdict."""
    result = getattr(response, "sanitization_result", None)
    if result is None:
        return GuardrailVerdict(state=VerdictState.UNAVAILABLE, detail="no sanitization_result")

    match_state = getattr(result, "filter_match_state", None)
    matched = str(match_state).endswith("MATCH_FOUND")

    filters: list[str] = []
    for name, filter_result in (getattr(result, "filter_results", {}) or {}).items():
        inner = str(getattr(filter_result, "match_state", "")) or str(filter_result)
        if "MATCH_FOUND" in inner:
            filters.append(str(name))

    if matched:
        return GuardrailVerdict(
            state=VerdictState.BLOCKED,
            backend="model_armor",
            filters_matched=filters or ["unspecified"],
            detail=f"Model Armor match in {context}",
        )

    return GuardrailVerdict(
        state=VerdictState.CLEAN,
        backend="model_armor",
        sanitized_text=text,
        detail=f"no match in {context}",
    )

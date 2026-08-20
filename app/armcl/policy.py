"""ARMCL policy: what is worth remembering, what gets pruned, what gets redacted.

This module is the part of ARMCL that is genuinely ours. Memory Bank supplies
storage and retrieval; the decisions about salience, distillation, redaction,
and eviction live here. Keeping them in one place means the memory backend can
be swapped without changing behaviour.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from app.settings import settings


class Salience(str, Enum):
    """How durable a fact should be."""

    EPHEMERAL = "ephemeral"
    """Useful for this step only. Tier 1, never persisted."""

    EPISODIC = "episodic"
    """Meaningful within this task. Tier 2, lives as long as the session."""

    DURABLE = "durable"
    """Changes future decisions. Tier 3, survives across sessions."""


# Keys that name a specific entity are worth carrying forward: a later step
# will almost certainly need to refer back to them.
_IDENTIFIER_HINTS = (
    "_id",
    "_ids",
    "id",
    "identifier",
    "ref",
    "reference",
    "number",
    "key",
    "uri",
    "url",
    "path",
    "name",
    "sha",
    "commit",
    "ticket",
    "case",
)

# Keys that express a rule or limit are what make the fleet decline things
# later. These are the whole reason Tier 3 exists.
_CONSTRAINT_HINTS = (
    "policy",
    "constraint",
    "restriction",
    "limit",
    "ceiling",
    "floor",
    "threshold",
    "requirement",
    "required",
    "eligib",
    "ineligib",
    "forbidden",
    "prohibited",
    "denied",
    "rejected",
    "declined",
    "blocked",
    "approval",
    "mandate",
    "deadline",
    "expires",
)

# Values that should never reach durable storage.
_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"gh[pousr]_[A-Za-z0-9_]{16,}"), "[REDACTED_GITHUB_TOKEN]"),
    (re.compile(r"AIza[0-9A-Za-z_\-]{35}"), "[REDACTED_GOOGLE_API_KEY]"),
    (re.compile(r"sk-[A-Za-z0-9]{20,}"), "[REDACTED_API_KEY]"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "[REDACTED_PRIVATE_KEY]"),
    (re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"), "[REDACTED_EMAIL]"),
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "[REDACTED_SSN]"),
    (re.compile(r"\b(?:\d[ \-]*?){13,16}\b"), "[REDACTED_CARD]"),
)


@dataclass
class StateDelta:
    """A structured fact extracted from raw tool output."""

    key: str
    value: Any
    salience: Salience
    source_step: str
    reason: str = ""


@dataclass
class DistillationResult:
    """Outcome of compressing a raw tool payload into durable facts."""

    deltas: list[StateDelta] = field(default_factory=list)
    bytes_in: int = 0
    bytes_out: int = 0
    summary: str = ""

    @property
    def bytes_pruned(self) -> int:
        return max(0, self.bytes_in - self.bytes_out)

    @property
    def compression_ratio(self) -> float:
        if self.bytes_in == 0:
            return 1.0
        return self.bytes_out / self.bytes_in


def redact(text: str) -> str:
    """Strip credentials and personal data before anything is persisted.

    This runs on the write path into Tier 2 and Tier 3. Model Armor screens
    content crossing trust boundaries; this is the narrower guarantee that
    secrets never enter long-term memory in the first place, so a later
    retrieval cannot leak what an earlier step happened to observe.
    """
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


# A value longer than this is bulk rather than a fact. Carrying it forward is
# precisely the context-window bloat ARMCL exists to prevent, so it stays in
# Tier 1 for the current step only and never reaches the ledger or Tier 3.
BULK_VALUE_THRESHOLD = 200

# Per-value cap when a fact is rendered into a summary or context frame.
RENDER_VALUE_CAP = 120


def is_bulk(value: Any) -> bool:
    """True when a value is too large to be treated as a fact."""
    if isinstance(value, str):
        return len(value) > BULK_VALUE_THRESHOLD
    if isinstance(value, (dict, list, tuple)):
        return len(str(value)) > BULK_VALUE_THRESHOLD * 2
    return False


def classify(key: str, value: Any) -> Salience:
    """Decide which tier a fact belongs in, from its key and shape."""
    lowered = key.lower()

    # Size is checked before the key hints. A key named `policy_document`
    # holding 5000 characters is bulk regardless of what it is called, and
    # promoting it to durable memory would poison Tier 3 with a payload that
    # gets retrieved on every future query.
    if is_bulk(value):
        return Salience.EPHEMERAL

    if any(hint in lowered for hint in _CONSTRAINT_HINTS):
        return Salience.DURABLE

    # Booleans that read as a verdict are decisions worth remembering.
    if isinstance(value, bool) and any(
        lowered.startswith(p) for p in ("is_", "has_", "can_", "should_", "was_")
    ):
        return Salience.DURABLE

    if any(lowered.endswith(hint) or lowered == hint for hint in _IDENTIFIER_HINTS):
        return Salience.EPISODIC

    return Salience.EPISODIC


def should_persist(delta: StateDelta) -> bool:
    """Tier 3 admission control."""
    if delta.salience is not Salience.DURABLE:
        return False
    if delta.value in (None, "", [], {}):
        return False
    return True


def distill(
    raw: Any,
    *,
    source_step: str,
    budget: int | None = None,
) -> DistillationResult:
    """Compress a raw tool payload into structured deltas plus a short summary.

    Deterministic on purpose. Distillation runs after every tool call, so
    spending a model round trip here would multiply latency and cost across the
    whole workflow. Nested payloads are flattened, scalar leaves are kept, and
    bulk arrays are replaced by a count. An agent that needs the full payload
    still has it in Tier 1 for the current step; what gets carried forward is
    only the distillate.
    """
    budget = budget or settings.raw_output_budget
    raw_text = str(raw)
    result = DistillationResult(bytes_in=len(raw_text))

    flat = _flatten(raw)
    for key, value in flat.items():
        salience = classify(key, value)
        result.deltas.append(
            StateDelta(
                key=key,
                # Redact on the way in, not just in the summary. Delta values
                # land in Tier 1, which ADK persists as session state, so a
                # credential left here would outlive the step that saw it.
                value=_redact_value(_truncate_value(value, budget)),
                salience=salience,
                source_step=source_step,
                reason=f"classified {salience.value} from key shape",
            )
        )

    kept = {d.key: d.value for d in result.deltas if d.salience is not Salience.EPHEMERAL}
    result.summary = redact(_summarize(kept, budget))
    result.bytes_out = len(result.summary)
    return result


def _flatten(obj: Any, prefix: str = "", depth: int = 0) -> dict[str, Any]:
    """Flatten nested structures to dotted keys, collapsing bulk arrays."""
    if depth > 4:
        return {prefix or "value": "[max depth]"}

    out: dict[str, Any] = {}

    if isinstance(obj, dict):
        for k, v in obj.items():
            key = f"{prefix}.{k}" if prefix else str(k)
            out.update(_flatten(v, key, depth + 1))
    elif isinstance(obj, (list, tuple)):
        # A long homogeneous array is noise; its length is the signal.
        if len(obj) > 5:
            out[f"{prefix}.count" if prefix else "count"] = len(obj)
            if obj and isinstance(obj[0], (str, int, float, bool)):
                out[f"{prefix}.sample" if prefix else "sample"] = list(obj[:3])
        else:
            for i, v in enumerate(obj):
                out.update(_flatten(v, f"{prefix}[{i}]", depth + 1))
    else:
        out[prefix or "value"] = obj

    return out


def _truncate_value(value: Any, budget: int) -> Any:
    if isinstance(value, str) and len(value) > budget:
        return value[:budget] + f"... [+{len(value) - budget} chars pruned]"
    return value


def _redact_value(value: Any) -> Any:
    """Apply redaction to strings and to strings nested one level in sequences."""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, list):
        return [redact(v) if isinstance(v, str) else v for v in value]
    return value


def render_value(value: Any, cap: int = RENDER_VALUE_CAP) -> str:
    """Render one value for human display, capped so no single field dominates."""
    text = str(value)
    if len(text) <= cap:
        return text
    return f"{text[:cap]}... [+{len(text) - cap} chars]"


def _summarize(facts: dict[str, Any], budget: int) -> str:
    # Cap each value individually. Capping only the joined string would let one
    # oversized field crowd out every other fact in the summary.
    parts = [f"{k}={render_value(v)}" for k, v in facts.items()]
    text = "; ".join(parts)
    if len(text) > budget:
        text = text[:budget] + "..."
    return text

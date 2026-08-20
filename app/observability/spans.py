"""Custom OpenTelemetry spans for ARMCL operations.

ADK instruments agent and tool calls on its own, but memory operations are the
interesting part of this system and are invisible by default. These spans make
each hydration and reconciliation appear in the Cloud Trace DAG with the facts
that were retrieved and why, which is what turns "auditable reasoning chain"
from a claim into something a judge can click through.

Span attributes deliberately carry keys, scores, and counts rather than memory
content. Prompt and response payloads are routed to Cloud Logging or Cloud
Storage separately by the platform, and duplicating them here would put
potentially sensitive text into spans that have weaker access controls.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from opentelemetry import trace

tracer = trace.get_tracer("fluffy_memory.armcl")

ARMCL_TIER = "armcl.tier"
ARMCL_OPERATION = "armcl.operation"
ARMCL_KEYS = "armcl.keys"
ARMCL_HIT_COUNT = "armcl.hit_count"
ARMCL_MISS_COUNT = "armcl.miss_count"
ARMCL_BYTES_PRUNED = "armcl.bytes_pruned"
ARMCL_REASON = "armcl.reason"


@contextmanager
def armcl_span(operation: str, **attributes: Any) -> Iterator[trace.Span]:
    """Wrap an ARMCL operation in a span.

    Never raises on telemetry failure. A broken exporter must not take the
    fleet down with it.
    """
    with tracer.start_as_current_span(f"armcl.{operation}") as span:
        try:
            span.set_attribute(ARMCL_OPERATION, operation)
            for key, value in attributes.items():
                if value is not None:
                    span.set_attribute(f"armcl.{key}", _coerce(value))
        except Exception:  # noqa: BLE001 - telemetry must never break execution
            pass
        yield span


def record_memory_hit(
    span: trace.Span,
    *,
    tier: int,
    keys: list[str],
    hit_count: int,
    miss_count: int = 0,
    reason: str = "",
) -> None:
    """Annotate a span with what memory returned and why it was consulted."""
    try:
        span.set_attribute(ARMCL_TIER, tier)
        span.set_attribute(ARMCL_KEYS, ",".join(keys[:20]))
        span.set_attribute(ARMCL_HIT_COUNT, hit_count)
        span.set_attribute(ARMCL_MISS_COUNT, miss_count)
        if reason:
            span.set_attribute(ARMCL_REASON, reason)
    except Exception:  # noqa: BLE001
        pass


def _coerce(value: Any) -> Any:
    """OTel attributes accept only scalars and homogeneous sequences."""
    if isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, (list, tuple)) and all(isinstance(v, str) for v in value):
        return list(value)
    return str(value)

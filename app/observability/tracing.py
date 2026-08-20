"""Trace export configuration.

On Agent Runtime, telemetry is handled by the platform: setting
`GOOGLE_CLOUD_AGENT_ENGINE_ENABLE_TELEMETRY=true` at deploy time installs an
exporter to Cloud Trace, and the ARMCL spans ride along with it. Nothing here
is needed in that environment.

This module covers the local case, where there is no platform exporter and the
spans would otherwise go nowhere. Two options:

  console  print spans to stdout; useful while developing the memory loop
  cloud    export to Cloud Trace from a local run, so the demo can show the
           same trace view without a full deploy
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

_CONFIGURED = False


def configure_local_tracing(mode: str | None = None) -> bool:
    """Install a local span exporter. Returns True if one was installed.

    Idempotent, and never raises: a broken exporter must not prevent the fleet
    from running.
    """
    global _CONFIGURED
    if _CONFIGURED:
        return True

    mode = mode or os.environ.get("ARMCL_TRACE_EXPORT", "").strip().lower()
    if not mode or mode == "none":
        return False

    if os.environ.get("GOOGLE_CLOUD_AGENT_ENGINE_ENABLE_TELEMETRY") == "true":
        # Agent Runtime already installed an exporter; adding another would
        # duplicate every span in Cloud Trace.
        logger.info("Platform telemetry active; skipping local exporter")
        return False

    try:
        from opentelemetry import trace
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        provider = TracerProvider()

        if mode == "console":
            from opentelemetry.sdk.trace.export import ConsoleSpanExporter

            provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
        elif mode == "cloud":
            from opentelemetry.exporter.cloud_trace import CloudTraceSpanExporter

            from app.settings import settings

            settings.require_cloud()
            provider.add_span_processor(
                BatchSpanProcessor(CloudTraceSpanExporter(project_id=settings.project))
            )
        else:
            logger.warning("Unknown ARMCL_TRACE_EXPORT=%r; expected console or cloud", mode)
            return False

        trace.set_tracer_provider(provider)
        _CONFIGURED = True
        logger.info("Local trace export configured: %s", mode)
        return True

    except ImportError as exc:
        logger.warning(
            "Trace export %r unavailable (%s). For cloud export install "
            "opentelemetry-exporter-gcp-trace.",
            mode,
            exc,
        )
        return False
    except Exception as exc:  # noqa: BLE001 - telemetry must never break the run
        logger.warning("Trace export setup failed: %s", exc)
        return False

"""AdkApp export for Agent Runtime.

Kept separate from `app/agent.py` so the graph stays importable without pulling
in Vertex AI, which matters for local runs and tests.
"""

from __future__ import annotations

from vertexai.agent_engines import AdkApp

import app.reference  # noqa: F401  registers the reference domain
from app.app_config import build_adk_app


def build_app() -> AdkApp:
    """Wrap the fleet for deployment.

    `memory_service_builder` is deliberately not passed. On Agent Runtime,
    AdkApp defaults to the Memory Bank provisioned alongside the deployment,
    and supplying our own builder would require the memory bank id at build
    time, before the deploy that creates it. The Chroma fallback is a local
    concern and never applies here.
    """
    return AdkApp(
        app=build_adk_app(),
        enable_tracing=True,
    )


adk_app = build_app()

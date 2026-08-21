"""ADK CLI entry point for the model-in-the-loop evaluation suite."""

import app.reference  # noqa: F401  register the synthetic recall workload
from app.agent import root_agent

__all__ = ["root_agent"]

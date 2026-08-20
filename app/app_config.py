"""Native ADK application configuration shared by local and managed runners."""

from google.adk.apps import App, ResumabilityConfig

from app.agent import root_workflow
from app.settings import settings


def build_adk_app() -> App:
    """Build a crash-resumable ADK app.

    Resumption is explicitly enabled rather than inferred from managed hosting.
    ADK documents resume as at-least-once, so every side-effecting domain
    operation also receives a stable idempotency key.
    """
    return App(
        name=settings.app_name,
        root_agent=root_workflow,
        resumability_config=ResumabilityConfig(is_resumable=True),
    )

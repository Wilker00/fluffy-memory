"""Standalone ADK application for the pre-screening Collaborative Partner."""

from google.adk.apps import App

from app.agents.intake_partner import intake_partner_agent
from app.settings import settings


def build_intake_app() -> App:
    """Expose guided intake separately from the autonomous screening graph."""
    return App(name=f"{settings.app_name}_intake", root_agent=intake_partner_agent)


intake_app = build_intake_app()

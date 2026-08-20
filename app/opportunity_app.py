"""Standalone ADK application for the Collaborative Opportunity Partner."""

from google.adk.apps import App

from app.agents.opportunity_partner import opportunity_partner_agent
from app.settings import settings


def build_opportunity_app() -> App:
    return App(name=f"{settings.app_name}_opportunities", root_agent=opportunity_partner_agent)


opportunity_app = build_opportunity_app()

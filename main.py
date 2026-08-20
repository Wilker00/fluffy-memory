"""Cloud Run function entry point for Pub/Sub-to-Agent-Runtime dispatch."""

from __future__ import annotations

import asyncio
import logging

import functions_framework

from app.triggers.pubsub import handle_push_message

logger = logging.getLogger(__name__)


@functions_framework.cloud_event
def pubsub_to_agent(cloud_event) -> None:
    """Invoke the deployed fleet for one Pub/Sub CloudEvent."""
    result = asyncio.run(handle_push_message(dict(cloud_event.data)))
    logger.info(
        "Pub/Sub event handled: status=%s session=%s events=%s",
        result["status"],
        result["session_id"],
        result["event_count"],
    )

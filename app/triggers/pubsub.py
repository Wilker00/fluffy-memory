"""Pub/Sub bridge for invoking the deployed Agent Runtime fleet.

Cloud Scheduler publishes a message to Pub/Sub. A second-generation Cloud Run
function receives that event and calls the already-deployed ADK application on
Agent Runtime. The bridge never constructs a local ``Runner`` or an in-memory
session: deployed ADK applications provide managed sessions, which are required
for state to survive process boundaries and human-approval pauses.

Pub/Sub is at-least-once. A stable session id derived from the message id acts
as the delivery marker; a redelivery finds that managed session and does not
execute the workflow a second time. Domain actions should still be idempotent,
because no distributed bridge can make an external side effect exactly-once.
"""

from __future__ import annotations

import base64
import json
import logging
import uuid
from typing import Any

from app.settings import settings

logger = logging.getLogger(__name__)

_SESSION_NAMESPACE = uuid.UUID("d884a2bf-34b0-4ad2-b5e2-ce82366fe4a8")


def decode_push_envelope(envelope: dict[str, Any]) -> dict[str, Any]:
    """Extract and validate a Pub/Sub push/CloudEvent payload."""
    message = envelope.get("message")
    if not isinstance(message, dict):
        raise ValueError("Not a Pub/Sub envelope: missing 'message'")

    message_id = message.get("messageId") or message.get("message_id")
    if not message_id:
        raise ValueError("Pub/Sub envelope has no message id")

    raw = message.get("data")
    if raw is None:
        payload: dict[str, Any] = {}
    else:
        try:
            decoded = base64.b64decode(raw, validate=True).decode("utf-8")
        except (ValueError, UnicodeDecodeError) as exc:
            raise ValueError("Pub/Sub message data is not valid base64 UTF-8") from exc

        try:
            parsed = json.loads(decoded)
        except json.JSONDecodeError:
            parsed = {"text": decoded}
        if not isinstance(parsed, dict):
            raise ValueError("Pub/Sub message JSON must be an object")
        payload = parsed

    attributes = message.get("attributes", {})
    payload["attributes"] = attributes if isinstance(attributes, dict) else {}
    payload["message_id"] = str(message_id)
    return payload


def build_prompt(payload: dict[str, Any]) -> str:
    """Turn a trigger payload into the fleet's task statement."""
    if "text" in payload and isinstance(payload["text"], str):
        return payload["text"]

    query = payload.get("query", "items requiring assessment")
    reason = payload.get("reason", "scheduled sweep")
    return (
        f"Scheduled fleet run ({reason}). Assess {query}. "
        "Follow all applicable constraints, including any recorded in durable memory."
    )


def session_id_for_message(message_id: str) -> str:
    """Return a stable, opaque managed-session id for one Pub/Sub delivery."""
    if not message_id:
        raise ValueError("message_id is required for idempotent dispatch")
    return f"pubsub-{uuid.uuid5(_SESSION_NAMESPACE, message_id).hex}"


def _load_remote_agent() -> Any:
    """Resolve the deployed Agent Runtime resource using workload credentials."""
    settings.require_cloud()
    if not settings.agent_engine_resource_name:
        raise RuntimeError(
            "AGENT_ENGINE_RESOURCE_NAME is required. Run `make deploy` and "
            "`make deploy-trigger` in that order."
        )

    import agentplatform

    client = agentplatform.Client(project=settings.project, location=settings.location)
    return client.agent_engines.get(name=settings.agent_engine_resource_name)


async def _existing_session(remote: Any, *, user_id: str, session_id: str) -> Any | None:
    try:
        return await remote.async_get_session(user_id=user_id, session_id=session_id)
    except RuntimeError as exc:
        # The remote API currently signals a missing session as RuntimeError.
        # Do not hide transport/authentication failures that also use RuntimeError.
        if "not found" not in str(exc).lower():
            raise
        return None


def _value(value: Any, name: str, default: Any = None) -> Any:
    return value.get(name, default) if isinstance(value, dict) else getattr(value, name, default)


def _session_events(session: Any) -> list[Any] | None:
    """Return events, preserving None to distinguish old opaque session payloads."""
    events = _value(session, "events")
    return list(events) if events is not None else None


def _event_interrupted(event: Any) -> bool:
    return bool(_value(event, "interrupted", False))


def _event_terminal(event: Any) -> bool:
    status = str(_value(event, "status", "")).upper()
    if status in {"COMPLETED", "DECLINED", "HALTED_CIRCUIT_BREAKER"}:
        return True
    final = getattr(event, "is_final_response", None)
    return bool(callable(final) and final())


async def run_remote_fleet(
    payload: dict[str, Any],
    *,
    remote: Any | None = None,
    user_id: str = "scheduler",
) -> dict[str, Any]:
    """Dispatch one Pub/Sub message to the deployed fleet.

    ``remote`` is injectable so the bridge can be tested without cloud access.
    """
    message_id = str(payload.get("message_id", ""))
    session_id = session_id_for_message(message_id)
    remote = remote or _load_remote_agent()

    existing = await _existing_session(remote, user_id=user_id, session_id=session_id)
    recovering = existing is not None
    if existing is not None:
        events = _session_events(existing)
        # Older Runtime responses did not expose events. Preserve the safe
        # legacy behavior for those opaque records; an explicit empty or
        # unfinished event list is recoverable.
        if events is None or (events and _event_terminal(events[-1])):
            logger.info(
                "Ignoring completed Pub/Sub redelivery: message_id=%s session=%s",
                message_id,
                session_id,
            )
            return {"status": "DUPLICATE", "session_id": session_id, "event_count": 0}
        if events and _event_interrupted(events[-1]):
            return {"status": "SUSPENDED", "session_id": session_id, "event_count": 0}

    if not recovering:
        try:
            await remote.async_create_session(user_id=user_id, session_id=session_id)
        except Exception as exc:
            # Two concurrent deliveries may both observe no session. Only an
            # AlreadyExists race is safe to treat as a duplicate.
            if type(exc).__name__ != "AlreadyExists":
                raise
            logger.info("Concurrent Pub/Sub redelivery suppressed: message_id=%s", message_id)
            return {"status": "DUPLICATE", "session_id": session_id, "event_count": 0}

    event_count = 0
    interrupted = False
    async for event in remote.async_stream_query(
        user_id=user_id,
        session_id=session_id,
        message=build_prompt(payload),
    ):
        event_count += 1
        if isinstance(event, dict):
            interrupted = interrupted or bool(event.get("interrupted"))

    status = "SUSPENDED" if interrupted else ("RECOVERED" if recovering else "COMPLETED")
    logger.info(
        "Agent Runtime dispatch finished: message_id=%s session=%s status=%s events=%d",
        message_id,
        session_id,
        status,
        event_count,
    )
    return {"status": status, "session_id": session_id, "event_count": event_count}


async def handle_push_message(envelope: dict[str, Any]) -> dict[str, Any]:
    """Handle a Pub/Sub push envelope; a normal return acknowledges delivery."""
    payload = decode_push_envelope(envelope)
    logger.info("Trigger received: message_id=%s", payload["message_id"])
    return await run_remote_fleet(payload)


async def handle_pull_message(message: Any) -> dict[str, Any]:
    """Handle and acknowledge one pull message only after successful dispatch."""
    envelope = {
        "message": {
            "data": message.data,
            "attributes": getattr(message, "attributes", {}),
            "messageId": getattr(message, "message_id", ""),
        }
    }
    result = await handle_push_message(envelope)
    message.ack()
    return result


def publish_test_trigger(query: str = "all units requiring assessment") -> str:
    """Publish one message to the trigger topic. Used to demo the path live."""
    from google.cloud import pubsub_v1

    settings.require_cloud()
    publisher = pubsub_v1.PublisherClient()
    topic_path = publisher.topic_path(settings.project, settings.pubsub_topic)

    data = json.dumps({"query": query, "reason": "manual test trigger"}).encode("utf-8")
    future = publisher.publish(topic_path, data)
    message_id = future.result(timeout=30)
    logger.info("Published trigger %s to %s", message_id, topic_path)
    return message_id

"""Tests for the Pub/Sub-to-Agent-Runtime bridge."""

from __future__ import annotations

import base64
import json

import pytest

from app.triggers import pubsub


def envelope(payload: object, *, message_id: str = "message-123") -> dict:
    data = base64.b64encode(json.dumps(payload).encode()).decode()
    return {"message": {"data": data, "messageId": message_id, "attributes": {"source": "test"}}}


class FakeRemote:
    def __init__(self, *, existing=None, events=None) -> None:
        self.existing = existing
        self.events = list(events or [{"node": "scout"}, {"node": "complete"}])
        self.get_calls = []
        self.create_calls = []
        self.query_calls = []

    async def async_get_session(self, **kwargs):
        self.get_calls.append(kwargs)
        return self.existing

    async def async_create_session(self, **kwargs):
        self.create_calls.append(kwargs)
        return {"id": kwargs["session_id"]}

    async def async_stream_query(self, **kwargs):
        self.query_calls.append(kwargs)
        for event in self.events:
            yield event


class PullMessage:
    def __init__(self, payload: object) -> None:
        self.data = base64.b64encode(json.dumps(payload).encode())
        self.attributes = {"source": "pull-test"}
        self.message_id = "pull-123"
        self.acked = False

    def ack(self) -> None:
        self.acked = True


def test_decodes_pubsub_envelope():
    payload = pubsub.decode_push_envelope(envelope({"query": "UNIT-7"}))
    assert payload["query"] == "UNIT-7"
    assert payload["message_id"] == "message-123"
    assert payload["attributes"] == {"source": "test"}


def test_cloud_function_entry_point_is_registered():
    from main import pubsub_to_agent

    assert callable(pubsub_to_agent)


@pytest.mark.parametrize(
    "bad",
    [
        {},
        {"message": {}},
        {"message": {"messageId": "x", "data": "not base64!"}},
    ],
)
def test_rejects_malformed_envelopes(bad):
    with pytest.raises(ValueError):
        pubsub.decode_push_envelope(bad)


def test_message_id_maps_to_stable_opaque_session():
    first = pubsub.session_id_for_message("projects/p/messages/42")
    assert first == pubsub.session_id_for_message("projects/p/messages/42")
    assert first != pubsub.session_id_for_message("projects/p/messages/43")
    assert first.startswith("pubsub-")
    assert "projects" not in first


async def test_new_delivery_uses_managed_session_and_remote_query():
    remote = FakeRemote()
    payload = pubsub.decode_push_envelope(envelope({"query": "UNIT-7"}))

    result = await pubsub.run_remote_fleet(payload, remote=remote)

    assert result["status"] == "COMPLETED"
    assert result["event_count"] == 2
    assert remote.create_calls == [{"user_id": "scheduler", "session_id": result["session_id"]}]
    assert remote.query_calls[0]["session_id"] == result["session_id"]
    assert "UNIT-7" in remote.query_calls[0]["message"]


async def test_existing_managed_session_suppresses_redelivery():
    remote = FakeRemote(existing={"id": "already-ran"})
    payload = pubsub.decode_push_envelope(envelope({"query": "UNIT-7"}))

    result = await pubsub.run_remote_fleet(payload, remote=remote)

    assert result["status"] == "DUPLICATE"
    assert remote.create_calls == []
    assert remote.query_calls == []


async def test_empty_existing_session_is_recovered_instead_of_dropped():
    remote = FakeRemote(existing={"id": "created-before-crash", "events": []})
    payload = pubsub.decode_push_envelope(envelope({"query": "UNIT-7"}))

    result = await pubsub.run_remote_fleet(payload, remote=remote)

    assert result["status"] == "RECOVERED"
    assert remote.create_calls == []
    assert len(remote.query_calls) == 1


async def test_interrupted_existing_session_waits_for_human_resume():
    remote = FakeRemote(existing={"events": [{"interrupted": True}]})
    payload = pubsub.decode_push_envelope(envelope({"query": "UNIT-7"}))

    result = await pubsub.run_remote_fleet(payload, remote=remote)

    assert result["status"] == "SUSPENDED"
    assert remote.query_calls == []


async def test_interrupted_event_reports_suspension():
    remote = FakeRemote(events=[{"node": "approver", "interrupted": True}])
    payload = pubsub.decode_push_envelope(envelope({"query": "UNIT-7"}))
    result = await pubsub.run_remote_fleet(payload, remote=remote)
    assert result["status"] == "SUSPENDED"


async def test_pull_message_acknowledged_only_after_success(monkeypatch):
    message = PullMessage({"query": "UNIT-7"})

    async def succeed(payload):
        return {"status": "COMPLETED", "session_id": "s", "event_count": 1}

    monkeypatch.setattr(pubsub, "run_remote_fleet", succeed)
    await pubsub.handle_pull_message(message)
    assert message.acked


async def test_pull_message_not_acknowledged_when_dispatch_fails(monkeypatch):
    message = PullMessage({"query": "UNIT-7"})

    async def fail(payload):
        raise RuntimeError("Agent Runtime unavailable")

    monkeypatch.setattr(pubsub, "run_remote_fleet", fail)
    with pytest.raises(RuntimeError, match="unavailable"):
        await pubsub.handle_pull_message(message)
    assert not message.acked

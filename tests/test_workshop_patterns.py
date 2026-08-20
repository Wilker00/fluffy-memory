"""Regression tests for the ADK workshop patterns used by the project."""

from google.adk.tools import LongRunningFunctionTool

from app.agents.approver import approval_tool
from app.app_config import build_adk_app
from app.armcl.hydrate import hydrate
from app.armcl.tiers import LedgerEntry, Tier1, Tier2
from app.reference import DOMAIN
from app.tools.fleet_tools import act_on_item


def test_native_adk_resumability_and_long_running_approval_are_enabled():
    adk_app = build_adk_app()
    assert adk_app.resumability_config is not None
    assert adk_app.resumability_config.is_resumable
    assert isinstance(approval_tool, LongRunningFunctionTool)
    assert approval_tool.is_long_running


async def test_requested_reference_item_is_selected_instead_of_catalog_first(domain):
    candidates = await domain.discover("assess UNIT-7")
    assert [candidate.item_id for candidate in candidates] == ["UNIT-7"]


async def test_tier2_recovers_the_original_typed_value_into_tier1(ctx):
    Tier2(ctx).append(
        LedgerEntry(
            step="discover",
            keys_written=["primary_item_id"],
            bytes_pruned=0,
            summary="redacted display text",
            values={"primary_item_id": "UNIT-7"},
        )
    )

    frame = await hydrate(ctx, intent="resume execution", required=["primary_item_id"])

    assert "tier2" in frame.hydrated_from
    assert Tier1(ctx).get("primary_item_id") == "UNIT-7"
    assert frame.scratchpad["primary_item_id"] == "UNIT-7"


async def test_action_replay_is_idempotent_within_a_durable_session(ctx, domain):
    first = await act_on_item("routine service", ctx, item_id="UNIT-3")
    second = await act_on_item("routine service", ctx, item_id="UNIT-3")

    assert first == second
    assert len(DOMAIN._actions) == 1

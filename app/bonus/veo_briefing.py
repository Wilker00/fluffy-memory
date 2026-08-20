"""Veo-generated incident briefing.

When a run ends in DECLINE or a circuit-breaker halt, somebody has to be told
what happened and why. The interesting content is the *memory chain*: which
constraint applied, which run it came from, and why it changed this outcome.

That chain is hard to convey in a log line and reads poorly as a wall of text.
This module renders it as a short narrated visual briefing using Veo.

Strictly optional and off by default. Video generation is slow and costs money,
so it is gated behind `ARMCL_VEO_BRIEFING=true` and intended for terminal
states only, not for every run.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

VEO_MODEL = "veo-3.1-generate-preview"


@dataclass
class Briefing:
    status: str
    prompt: str
    video_uri: str = ""
    error: str = ""

    @property
    def succeeded(self) -> bool:
        return bool(self.video_uri)


def enabled() -> bool:
    return os.environ.get("ARMCL_VEO_BRIEFING", "").strip().lower() == "true"


def build_prompt(summary: dict[str, Any]) -> str:
    """Turn a run summary into a Veo prompt.

    Describes a control-room scene rather than asking for text on screen: video
    models render prose unreliably, and a legible narrative beats a garbled
    caption.
    """
    status = summary.get("status", "UNKNOWN")
    item = summary.get("item_id", "an asset")
    constraints = summary.get("constraints_applied") or []
    rationale = summary.get("rationale") or summary.get("detail") or ""

    if status == "DECLINED":
        scene = (
            "A calm, modern operations control room, cool blue lighting. "
            "A large central display shows a maintenance request for "
            f"{item} being set aside, with a policy record highlighted beside it. "
            "Slow push-in on the display. Professional, documentary tone."
        )
        reason = (
            f"It applied a previously recorded constraint: {constraints[0]}"
            if constraints
            else rationale
        )
        narrative = f"The fleet declined work on {item}. {reason}"
    elif status == "HALTED_CIRCUIT_BREAKER":
        scene = (
            "A modern operations control room with amber warning lighting. "
            "A central display shows a repeated verification cycle stopping, "
            "with a clear halt indicator. Steady camera, serious tone."
        )
        narrative = (
            f"Work on {item} was halted after repeated verification failures. "
            "The circuit breaker stopped the cycle and escalated for human review."
        )
    else:
        scene = (
            "A modern operations control room, soft green lighting. A central "
            f"display shows completed work on {item} being verified and signed off. "
            "Gentle camera drift. Calm, professional tone."
        )
        narrative = f"The fleet completed and verified work on {item}. {rationale}"

    return f"{scene} Narration: {narrative}"


async def generate_briefing(summary: dict[str, Any]) -> Briefing:
    """Render a run summary as a short video. Never raises."""
    prompt = build_prompt(summary)
    status = summary.get("status", "UNKNOWN")

    if not enabled():
        return Briefing(status=status, prompt=prompt, error="disabled")

    try:
        from google import genai
        from google.genai import types

        client = genai.Client()
        operation = client.models.generate_videos(
            model=VEO_MODEL,
            source=types.GenerateVideosSource(prompt=prompt),
        )

        import asyncio

        # Veo runs as a long operation; poll rather than block a worker thread.
        waited = 0
        while not operation.done and waited < 300:
            await asyncio.sleep(10)
            waited += 10
            operation = client.operations.get(operation)

        if not operation.done:
            return Briefing(status=status, prompt=prompt, error="timed out after 300s")

        videos = getattr(operation.response, "generated_videos", None) or []
        if not videos:
            return Briefing(status=status, prompt=prompt, error="no video returned")

        uri = getattr(videos[0].video, "uri", "") or str(videos[0].video)
        logger.info("Veo briefing generated for %s", summary.get("item_id"))
        return Briefing(status=status, prompt=prompt, video_uri=uri)

    except Exception as exc:  # noqa: BLE001
        # A failed briefing must not affect the run it describes.
        logger.warning("Veo briefing failed: %s", exc)
        return Briefing(status=status, prompt=prompt, error=f"{type(exc).__name__}: {exc}")

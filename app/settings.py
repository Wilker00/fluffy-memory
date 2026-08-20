"""Central configuration for the fluffy-memory fleet.

Model IDs are pinned as literal constants rather than read from the
environment. The hackathon requires Gemini 3.5 or newer, and a judge reading
this file should be able to confirm compliance without running anything.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Literal

from dotenv import load_dotenv

load_dotenv()

# --------------------------------------------------------------------------
# Models: pinned literals, not configurable.
# --------------------------------------------------------------------------
REASONING_MODEL = "gemini-3.5-flash"
"""Workhorse model for every agent in the fleet."""

DISTILL_MODEL = "gemini-3.5-flash"
"""Cheap model for ARMCL post-action delta extraction."""

ADJUDICATION_MODEL = "gemini-3.5-pro"
"""Reserved for the critic's final call, where reasoning depth pays for itself."""


MemoryBackend = Literal["memory_bank", "chroma", "memory"]
GuardrailBackend = Literal["model_armor", "regex"]
SessionBackend = Literal["database", "memory"]


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def _env_bool(key: str, default: bool) -> bool:
    raw = _env(key)
    if not raw:
        return default
    return raw.lower() in {"1", "true", "yes", "on"}


def _env_int(key: str, default: int) -> int:
    raw = _env(key)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    """Resolved runtime configuration."""

    project: str = field(default_factory=lambda: _env("GOOGLE_CLOUD_PROJECT"))
    location: str = field(default_factory=lambda: _env("GOOGLE_CLOUD_LOCATION", "us-central1"))
    staging_bucket: str = field(default_factory=lambda: _env("STAGING_BUCKET"))
    agent_engine_resource_name: str = field(
        default_factory=lambda: _env("AGENT_ENGINE_RESOURCE_NAME")
    )

    app_name: str = "fluffy_memory"

    # ARMCL
    memory_backend: MemoryBackend = field(
        default_factory=lambda: _env("ARMCL_MEMORY_BACKEND", "memory_bank")  # type: ignore[return-value]
    )
    memory_bank_id: str = field(default_factory=lambda: _env("ARMCL_MEMORY_BANK_ID"))
    chroma_dir: str = field(default_factory=lambda: _env("ARMCL_CHROMA_DIR", "./chroma_data"))
    reconcile_window: int = field(default_factory=lambda: _env_int("ARMCL_RECONCILE_WINDOW", 5))
    raw_output_budget: int = field(
        default_factory=lambda: _env_int("ARMCL_RAW_OUTPUT_BUDGET", 2000)
    )
    sync_raw_session_events: bool = field(
        default_factory=lambda: _env_bool("ARMCL_SYNC_RAW_SESSION_EVENTS", False)
    )
    circuit_breaker_threshold: int = field(
        default_factory=lambda: _env_int("ARMCL_CIRCUIT_BREAKER_THRESHOLD", 3)
    )
    session_backend: SessionBackend = field(
        default_factory=lambda: _env("ARMCL_SESSION_BACKEND", "database")  # type: ignore[return-value]
    )
    session_db_url: str = field(
        default_factory=lambda: _env(
            "ARMCL_SESSION_DB_URL", "sqlite+aiosqlite:///./armcl_sessions.db"
        )
    )

    # Self-evolution. On by default: after a terminal work node the evolver
    # proposes a playbook rewrite and a deterministic auditor accepts or
    # refuses it. Set ARMCL_EVOLVE=false to skip the extra model call.
    evolve_enabled: bool = field(default_factory=lambda: _env_bool("ARMCL_EVOLVE", True))
    behavioral_audit_enabled: bool = field(
        default_factory=lambda: _env_bool("ARMCL_BEHAVIORAL_AUDIT", True)
    )
    playbook_path: str = field(default_factory=lambda: _env("ARMCL_PLAYBOOK_PATH"))

    # Guardrails
    guardrail_backend: GuardrailBackend = field(
        default_factory=lambda: _env("GUARDRAIL_BACKEND", "model_armor")  # type: ignore[return-value]
    )
    model_armor_template_id: str = field(
        default_factory=lambda: _env("MODEL_ARMOR_TEMPLATE_ID", "fluffy-memory-fleet-filter")
    )
    model_armor_location: str = field(
        default_factory=lambda: _env("MODEL_ARMOR_LOCATION", "us-central1")
    )

    # Trigger
    pubsub_topic: str = field(
        default_factory=lambda: _env("PUBSUB_TOPIC", "fluffy-memory-triggers")
    )

    @property
    def staging_bucket_uri(self) -> str:
        bucket = self.staging_bucket
        return bucket if bucket.startswith("gs://") else f"gs://{bucket}"

    @property
    def model_armor_template_path(self) -> str:
        return (
            f"projects/{self.project}/locations/{self.model_armor_location}"
            f"/templates/{self.model_armor_template_id}"
        )

    @property
    def model_armor_endpoint(self) -> str:
        return f"modelarmor.{self.model_armor_location}.rep.googleapis.com"

    def require_cloud(self) -> None:
        """Fail loudly when cloud-only paths are entered without configuration."""
        missing = [
            name
            for name, value in (
                ("GOOGLE_CLOUD_PROJECT", self.project),
                ("STAGING_BUCKET", self.staging_bucket),
            )
            if not value
        ]
        if missing:
            raise RuntimeError(
                f"Missing required environment variables: {', '.join(missing)}. "
                "Copy .env.example to .env and fill it in."
            )


settings = Settings()

# Telemetry env vars forwarded to Agent Runtime at deploy time. Documented at
# https://docs.cloud.google.com/gemini-enterprise-agent-platform/scale/runtime/tracing
TELEMETRY_ENV = {
    "GOOGLE_CLOUD_AGENT_ENGINE_ENABLE_TELEMETRY": "true",
    "OTEL_SEMCONV_STABILITY_OPT_IN": "gen_ai_latest_experimental",
    "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT": "EVENT_ONLY",
}

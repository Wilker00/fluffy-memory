"""Deploy the fleet to Vertex AI Agent Runtime.

    python deploy/deploy.py
    python deploy/deploy.py --update      # update in place instead of creating
    python deploy/deploy.py --no-identity # if Agent Identity is unavailable

Writes the resource name and memory bank id to .deployed_agent.json, which
destroy.py reads for teardown and which supplies ARMCL_MEMORY_BANK_ID for
local runs against the deployed Memory Bank.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import vertexai  # noqa: E402
from vertexai import types  # noqa: E402

from app.settings import TELEMETRY_ENV, settings  # noqa: E402

STATE_FILE = Path(__file__).resolve().parent.parent / ".deployed_agent.json"

REQUIREMENTS = [
    "google-adk>=2.7.0",
    "google-cloud-aiplatform[agent_engines,adk]>=1.165.0",
    "google-cloud-modelarmor>=0.7.0",
    "google-cloud-pubsub>=2.23.0",
    "opentelemetry-api>=1.25.0",
    "opentelemetry-sdk>=1.25.0",
    "pydantic>=2.7.0",
    "httpx>=0.27.0",
    "python-dotenv>=1.0.0",
]


def build_config(*, use_identity: bool) -> dict:
    env_vars = dict(TELEMETRY_ENV)
    env_vars.update(
        {
            "GOOGLE_CLOUD_PROJECT": settings.project,
            "GOOGLE_CLOUD_LOCATION": settings.location,
            "GOOGLE_GENAI_USE_VERTEXAI": "TRUE",
            # Memory Bank is provisioned with the deployment; AdkApp binds to it
            # automatically, so no bank id is needed here.
            "ARMCL_MEMORY_BACKEND": "memory_bank",
            "GUARDRAIL_BACKEND": settings.guardrail_backend,
            "MODEL_ARMOR_TEMPLATE_ID": settings.model_armor_template_id,
            "MODEL_ARMOR_LOCATION": settings.model_armor_location,
            "PUBSUB_TOPIC": settings.pubsub_topic,
        }
    )

    config: dict = {
        "display_name": "fluffy-memory ARMCL Fleet",
        "description": (
            "Fortified enterprise fleet with ARMCL: autonomous retrieval and memory "
            "context loop for zero-prompt state persistence across sessions."
        ),
        "requirements": REQUIREMENTS,
        "extra_packages": ["app"],
        "staging_bucket": settings.staging_bucket_uri,
        "env_vars": env_vars,
        # Scale to zero when idle so an unused deployment costs nothing.
        "min_instances": 0,
        "max_instances": 2,
    }

    if use_identity:
        config["identity_type"] = types.IdentityType.AGENT_IDENTITY

    return config


def save_state(resource_name: str) -> None:
    bank_id = resource_name.rstrip("/").split("/")[-1]
    STATE_FILE.write_text(
        json.dumps(
            {
                "resource_name": resource_name,
                "memory_bank_id": bank_id,
                "project": settings.project,
                "location": settings.location,
            },
            indent=2,
        )
    )
    print(f"\nWrote {STATE_FILE.name}")
    print("\nTo point local runs at the deployed Memory Bank, set in .env:")
    print(f"  ARMCL_MEMORY_BANK_ID={bank_id}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Deploy the fleet to Agent Runtime.")
    parser.add_argument("--update", action="store_true", help="Update the existing deployment.")
    parser.add_argument(
        "--no-identity",
        action="store_true",
        help="Deploy without Agent Identity (use if the account has no Cloud Organization).",
    )
    args = parser.parse_args()

    settings.require_cloud()
    print(f"Project : {settings.project}")
    print(f"Location: {settings.location}")
    print(f"Staging : {settings.staging_bucket_uri}")

    from app.agent_engine_app import build_app

    client = vertexai.Client(project=settings.project, location=settings.location)
    app_obj = build_app()
    config = build_config(use_identity=not args.no_identity)

    if args.update:
        if not STATE_FILE.exists():
            print(f"No {STATE_FILE.name}; deploy first.", file=sys.stderr)
            return 1
        resource_name = json.loads(STATE_FILE.read_text())["resource_name"]
        print(f"\nUpdating {resource_name} (several minutes)...")
        remote = client.agent_engines.update(
            name=resource_name, agent_engine=app_obj, config=config
        )
    else:
        print("\nCreating deployment (first deploy takes several minutes)...")
        try:
            remote = client.agent_engines.create(agent_engine=app_obj, config=config)
        except Exception as exc:
            message = str(exc)
            if not args.no_identity and "identity" in message.lower():
                # Agent Identity needs organization-level IAM that a personal
                # account will not have. Retrying without it keeps the deploy
                # viable, and the README records which mode was used.
                print(f"\nAgent Identity rejected ({message[:160]}).")
                print("Retrying with the default service account identity...")
                remote = client.agent_engines.create(
                    agent_engine=app_obj, config=build_config(use_identity=False)
                )
            else:
                raise

    resource_name = getattr(getattr(remote, "api_resource", None), "name", None) or str(remote)
    print(f"\nDeployed: {resource_name}")
    save_state(resource_name)

    print("\nNext:")
    print("  ./deploy/scheduler.sh $GOOGLE_CLOUD_PROJECT   wire the autonomous trigger")
    print("  make destroy                                  tear down after recording")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

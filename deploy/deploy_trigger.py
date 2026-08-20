"""Deploy the Pub/Sub bridge as a second-generation Cloud Run function.

Run after ``deploy/deploy.py`` so ``.deployed_agent.json`` contains the Agent
Runtime resource that the bridge must invoke.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.settings import settings  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
STATE_FILE = ROOT / ".deployed_agent.json"
FUNCTION_NAME = "fluffy-memory-trigger"
SERVICE_ACCOUNT_ID = "fluffy-memory-trigger"


def run(*args: str, capture: bool = False) -> str:
    """Run gcloud without a shell so project values cannot become commands."""
    command = ["gcloud", *args]
    result = subprocess.run(
        command,
        cwd=ROOT,
        check=True,
        text=True,
        capture_output=capture,
    )
    return result.stdout.strip() if capture else ""


def ensure_service_account(project: str) -> str:
    email = f"{SERVICE_ACCOUNT_ID}@{project}.iam.gserviceaccount.com"
    described = subprocess.run(
        ["gcloud", "iam", "service-accounts", "describe", email, "--project", project],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )
    if described.returncode != 0:
        run(
            "iam",
            "service-accounts",
            "create",
            SERVICE_ACCOUNT_ID,
            "--display-name",
            "fluffy-memory Pub/Sub trigger",
            "--project",
            project,
        )

    for role in ("roles/aiplatform.user", "roles/logging.logWriter"):
        run(
            "projects",
            "add-iam-policy-binding",
            project,
            "--member",
            f"serviceAccount:{email}",
            "--role",
            role,
            "--condition=None",
            "--quiet",
        )
    return email


def main() -> int:
    settings.require_cloud()
    if not STATE_FILE.exists():
        print(f"No {STATE_FILE.name}; run `make deploy` first.", file=sys.stderr)
        return 1

    state = json.loads(STATE_FILE.read_text())
    resource_name = state.get("resource_name")
    if not resource_name:
        print(f"{STATE_FILE.name} has no resource_name; deploy the fleet again.", file=sys.stderr)
        return 1

    service_account = ensure_service_account(settings.project)
    env_vars = ",".join(
        (
            f"GOOGLE_CLOUD_PROJECT={settings.project}",
            f"GOOGLE_CLOUD_LOCATION={settings.location}",
            f"AGENT_ENGINE_RESOURCE_NAME={resource_name}",
        )
    )

    print(f"Deploying {FUNCTION_NAME} -> {resource_name}")
    run(
        "functions",
        "deploy",
        FUNCTION_NAME,
        "--gen2",
        "--runtime",
        "python312",
        "--region",
        settings.location,
        "--project",
        settings.project,
        "--source",
        str(ROOT),
        "--entry-point",
        "pubsub_to_agent",
        "--trigger-topic",
        settings.pubsub_topic,
        "--service-account",
        service_account,
        "--set-env-vars",
        env_vars,
        "--timeout",
        "540s",
        "--memory",
        "512MiB",
        "--min-instances",
        "0",
        "--max-instances",
        "2",
        "--retry",
        "--quiet",
    )

    state["trigger_function"] = FUNCTION_NAME
    STATE_FILE.write_text(json.dumps(state, indent=2))
    print("Trigger deployed. Cloud Scheduler can now publish safely to the topic.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

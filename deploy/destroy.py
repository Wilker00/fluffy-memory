"""Tear down deployed resources.

    python deploy/destroy.py            # delete the Agent Runtime deployment
    python deploy/destroy.py --all      # also remove the Scheduler job and topic

The hackathon guidance is explicit about switching services off after
recording. Run this as soon as the demo footage is captured.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import vertexai  # noqa: E402

from app.settings import settings  # noqa: E402

STATE_FILE = Path(__file__).resolve().parent.parent / ".deployed_agent.json"


def delete_agent_engine() -> bool:
    if not STATE_FILE.exists():
        print(f"No {STATE_FILE.name}; nothing recorded to delete.")
        print("If a deployment exists, list it with:")
        print("  gcloud ai reasoning-engines list --region $GOOGLE_CLOUD_LOCATION")
        return False

    state = json.loads(STATE_FILE.read_text())
    resource_name = state["resource_name"]

    print(f"Deleting {resource_name} ...")
    client = vertexai.Client(
        project=state.get("project", settings.project),
        location=state.get("location", settings.location),
    )
    # force=True also removes child sessions and memories, which otherwise
    # block deletion and keep incurring storage cost.
    client.agent_engines.delete(name=resource_name, force=True)
    STATE_FILE.unlink()
    print("Deleted. Deployment state file removed.")
    return True


def delete_trigger_resources() -> None:
    project = settings.project
    region = settings.location
    topic = settings.pubsub_topic

    commands = [
        (
            "Scheduler job",
            [
                "gcloud",
                "scheduler",
                "jobs",
                "delete",
                "fluffy-memory-sweep",
                "--location",
                region,
                "--project",
                project,
                "--quiet",
            ],
        ),
        (
            "Cloud Run trigger function",
            [
                "gcloud",
                "functions",
                "delete",
                "fluffy-memory-trigger",
                "--gen2",
                "--region",
                region,
                "--project",
                project,
                "--quiet",
            ],
        ),
        (
            "Pub/Sub topic",
            ["gcloud", "pubsub", "topics", "delete", topic, "--project", project, "--quiet"],
        ),
    ]

    for label, cmd in commands:
        print(f"Deleting {label} ...")
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0:
            print("  deleted")
        else:
            print(f"  skipped: {result.stderr.strip()[:160]}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Tear down deployed resources.")
    parser.add_argument(
        "--all",
        action="store_true",
        help="Also delete the Scheduler job, trigger function, and Pub/Sub topic.",
    )
    args = parser.parse_args()

    if args.all:
        delete_trigger_resources()
    delete_agent_engine()

    print("\nThe staging bucket and Model Armor template are left in place;")
    print("they cost effectively nothing and re-running setup would recreate them.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

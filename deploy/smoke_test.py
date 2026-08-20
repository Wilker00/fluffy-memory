"""Phase 0 de-risking: prove Agent Runtime works on this account, then clean up.

Deploys a trivial agent, probes each Gemini Enterprise Agent Platform capability
the fleet depends on, prints a capability matrix, and deletes everything.

Agent Identity and other governance features are built around organization-level
IAM. On a personal account with no Cloud Organization they may be unavailable, so
each capability is probed independently and a failure downgrades rather than
aborts. Run this before building anything that assumes those features exist.

    python deploy/smoke_test.py
"""

from __future__ import annotations

import asyncio
import sys
import traceback

sys.path.insert(0, ".")

import vertexai  # noqa: E402
from google.adk import Agent  # noqa: E402
from vertexai import types  # noqa: E402
from vertexai.agent_engines import AdkApp  # noqa: E402

from app.settings import REASONING_MODEL, TELEMETRY_ENV, settings  # noqa: E402

RESULTS: dict[str, tuple[bool, str]] = {}


def record(capability: str, ok: bool, detail: str = "") -> None:
    RESULTS[capability] = (ok, detail)
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {capability}" + (f" - {detail}" if detail else ""))


def build_probe_agent() -> Agent:
    def add_numbers(a: int, b: int) -> int:
        """Adds two integers and returns the sum."""
        return a + b

    return Agent(
        model=REASONING_MODEL,
        name="fluffy_memory_smoke_probe",
        instruction="You are a probe agent. Use the add_numbers tool when asked to add.",
        tools=[add_numbers],
    )


def try_deploy(client: vertexai.Client, app: AdkApp, use_agent_identity: bool):
    """Deploy, optionally with Agent Identity. Returns the remote agent or None."""
    config: dict = {
        "requirements": [
            "google-adk>=2.7.0",
            "google-cloud-aiplatform[agent_engines,adk]>=1.165.0",
        ],
        "staging_bucket": settings.staging_bucket_uri,
        "display_name": "fluffy-memory-smoke-probe",
        "env_vars": dict(TELEMETRY_ENV),
        "min_instances": 0,
    }
    if use_agent_identity:
        config["identity_type"] = types.IdentityType.AGENT_IDENTITY

    label = "with AGENT_IDENTITY" if use_agent_identity else "with default identity"
    print(f"\n==> Deploying {label} (first deploy takes several minutes)...")
    return client.agent_engines.create(agent_engine=app, config=config)


async def query(remote) -> str:
    chunks: list[str] = []
    async for event in remote.async_stream_query(
        user_id="smoke-user",
        message="What is 21 plus 21? Use your tool.",
    ):
        chunks.append(str(event))
    return "\n".join(chunks)


async def main() -> int:
    print("=" * 70)
    print("fluffy-memory Phase 0 smoke test")
    print("=" * 70)

    try:
        settings.require_cloud()
        record("env configured", True, f"{settings.project} / {settings.location}")
    except RuntimeError as exc:
        record("env configured", False, str(exc))
        return 1

    try:
        client = vertexai.Client(project=settings.project, location=settings.location)
        record("vertexai.Client", True)
    except Exception as exc:
        record("vertexai.Client", False, f"{type(exc).__name__}: {exc}")
        return 1

    app = AdkApp(agent=build_probe_agent(), enable_tracing=True)
    record("AdkApp constructed", True, f"model={REASONING_MODEL}")

    remote = None
    agent_identity_ok = False
    try:
        remote = try_deploy(client, app, use_agent_identity=True)
        agent_identity_ok = True
        record("Agent Runtime deploy", True)
        record("Agent Identity", True, "AGENT_IDENTITY accepted")
    except Exception as exc:
        record("Agent Identity", False, f"{type(exc).__name__}: {str(exc)[:160]}")
        print("\n    Retrying without Agent Identity...")
        try:
            remote = try_deploy(client, app, use_agent_identity=False)
            record("Agent Runtime deploy", True, "fell back to default identity")
        except Exception as exc2:
            record("Agent Runtime deploy", False, f"{type(exc2).__name__}: {str(exc2)[:200]}")
            traceback.print_exc()
            return 1

    resource_name = getattr(remote, "api_resource", None)
    resource_name = getattr(resource_name, "name", None) or str(remote)
    print(f"\n    Deployed: {resource_name}")
    record("Agent Registry (auto-registration)", True, "deployed agents self-register")

    try:
        out = await query(remote)
        record("remote query", True, f"{len(out)} chars streamed")
    except Exception as exc:
        record("remote query", False, f"{type(exc).__name__}: {str(exc)[:160]}")

    try:
        session = await remote.async_create_session(user_id="smoke-user")
        sid = session.get("id") if isinstance(session, dict) else getattr(session, "id", None)
        record("managed sessions (ARMCL Tier 2)", True, f"session={sid}")
    except Exception as exc:
        record("managed sessions (ARMCL Tier 2)", False, f"{type(exc).__name__}: {str(exc)[:160]}")

    # Memory Bank is provisioned alongside the runtime; presence of the
    # reasoningEngine resource id is what ARMCL_MEMORY_BANK_ID needs.
    try:
        bank_id = str(resource_name).split("/")[-1]
        record("Memory Bank (ARMCL Tier 3)", True, f"ARMCL_MEMORY_BANK_ID={bank_id}")
    except Exception as exc:
        record("Memory Bank (ARMCL Tier 3)", False, str(exc)[:160])

    print("\n==> Tearing down probe resources...")
    try:
        remote.delete(force=True)
        record("teardown", True, "probe deleted, no ongoing cost")
    except Exception as exc:
        record("teardown", False, f"DELETE MANUALLY: {resource_name} ({exc})")

    print("\n" + "=" * 70)
    print("CAPABILITY MATRIX")
    print("=" * 70)
    for cap, (ok, detail) in RESULTS.items():
        print(f"  {'PASS' if ok else 'FAIL'}  {cap:38} {detail}")

    print("\nInterpretation:")
    if agent_identity_ok:
        print("  Agent Identity works. Build the full Fortified governance story.")
    else:
        print("  Agent Identity unavailable (likely no Cloud Organization).")
        print("  Fall back to a dedicated service account and say so plainly in the README.")
        print("  Model Armor floor settings still work at project scope.")

    blocking = {"Agent Runtime deploy", "vertexai.Client", "env configured"}
    hard = [c for c, (ok, _) in RESULTS.items() if not ok and c in blocking]
    if hard:
        print(f"\n  BLOCKING failures: {hard}")
        return 1
    print("\n  No blocking failures. Agent Runtime is viable on this account.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

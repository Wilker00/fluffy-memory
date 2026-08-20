"""Run the fleet locally against the generalized opportunity workload.

    python -m app.local_run
    python -m app.local_run --opportunity-mode prepare
    python -m app.local_run --runs 3          # demonstrate cross-session recall

Prints each node transition, the ARMCL tier state, and the final outcome, so
the memory loop is visible without opening Cloud Trace.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from typing import Any

from google.adk import Runner
from google.adk.sessions import BaseSessionService
from google.genai import types

import app.domains  # noqa: F401  registers the generalized opportunity domain
from app.app_config import build_adk_app
from app.armcl.memory_backend import build_memory_service
from app.armcl.tiers import T2_LEDGER_KEY, Tier1
from app.evolve.playbook import get_store, scope_key
from app.observability.tracing import configure_local_tracing
from app.opportunities import (
    OPPORTUNITY_STORE,
    CandidateProfile,
    ExecutionMode,
    OpportunityType,
)
from app.session_backend import build_session_service
from app.settings import settings


def configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.INFO if verbose else logging.WARNING,
        format="%(levelname)-7s %(name)-28s %(message)s",
    )
    for noisy in ("google_genai", "google.adk.models", "urllib3", "httpx"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def banner(text: str) -> None:
    print(f"\n{'=' * 72}\n{text}\n{'=' * 72}")


async def one_run(
    runner: Runner,
    session_service: BaseSessionService,
    *,
    user_id: str,
    query: str,
    run_label: str,
) -> dict[str, Any]:
    session = await session_service.create_session(app_name=settings.app_name, user_id=user_id)

    prompt = (
        f"Scheduled fleet run. Assess {query}. "
        "Apply every relevant constraint, including any recorded in durable memory."
    )
    message = types.Content(role="user", parts=[types.Part(text=prompt)])

    banner(f"{run_label}: {query}")
    final: Any = None
    interrupted = False

    async for event in runner.run_async(
        user_id=user_id, session_id=session.id, new_message=message
    ):
        name = getattr(event.node_info, "name", None)
        if event.output is not None:
            print(f"  -> {name}: {_short(event.output)}")
            final = event.output
        if event.error_message:
            print(f"  !! {name}: {event.error_message}")
        if event.interrupted:
            interrupted = True
            print(f"  || {name}: workflow SUSPENDED awaiting human approval")

    refreshed = await session_service.get_session(
        app_name=settings.app_name, user_id=user_id, session_id=session.id
    )
    _print_tiers(refreshed)

    return {"final": final, "interrupted": interrupted, "session_id": session.id}


def _short(value: Any, limit: int = 160) -> str:
    text = str(value).replace("\n", " ")
    return text if len(text) <= limit else text[:limit] + "..."


def _print_tiers(session: Any) -> None:
    if session is None:
        return

    class _S:
        state = session.state

    t1 = Tier1(_S()).snapshot()
    ledger = session.state.get(T2_LEDGER_KEY, [])

    print("\n  --- ARMCL state ---")
    print(f"  Tier 1 scratchpad ({len(t1)} keys):")
    for k, v in list(t1.items())[:10]:
        print(f"      {k} = {_short(v, 70)}")

    total_pruned = sum(int(e.get("bytes_pruned", 0)) for e in ledger)
    print(f"  Tier 2 ledger ({len(ledger)} steps, {total_pruned:,} bytes pruned):")
    for entry in ledger:
        print(f"      [{entry.get('status')}] {entry.get('step')}")

    playbook = get_store(scope_key(session)).snapshot()
    preview = (
        playbook.text.replace("\n", " ")[:90] if playbook.text else "(seed — constitution only)"
    )
    print(
        f"  Playbook generation {playbook.generation} "
        f"(proxy={playbook.proxy_score:.2f} held-out={playbook.heldout_score:.2f}):"
    )
    print(f"      {preview}")


async def main() -> int:
    parser = argparse.ArgumentParser(description="Run the fluffy-memory fleet locally.")
    parser.add_argument(
        "--query",
        default="demo-opportunities",
        help="Existing SEARCH-* request id, or demo-opportunities to seed a local case.",
    )
    parser.add_argument(
        "--opportunity-mode",
        choices=[mode.value for mode in ExecutionMode],
        default=ExecutionMode.RECOMMEND.value,
        help="Action taken for the seeded demo opportunity search.",
    )
    parser.add_argument("--runs", type=int, default=1, help="Consecutive runs sharing memory.")
    parser.add_argument("--user-id", default="local-operator")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    if args.query == "demo-opportunities":
        profile = CandidateProfile(
            profile_id="PROFILE-LOCAL-DEMO",
            tenant_id=args.user_id,
            source_application_id="CAREER-EVIDENCE-LOCAL",
            summary="Backend and hardware engineer with evidence-backed project experience.",
            verified_skills=[
                "python",
                "postgresql",
                "google cloud",
                "kubernetes",
                "git",
                "embedded",
                "pcb",
            ],
            experience_years={"backend": 3.0},
            education_level="bachelor",
            coursework=["calculus i", "data structures"],
            portfolio_evidence={
                "backend": "demo:portfolio:backend",
                "hardware": "demo:portfolio:hardware",
                "open source": "demo:portfolio:open-source",
            },
            preferred_locations=["Remote", "New York", "Boston"],
            work_authorization="US authorized",
            sponsorship_required=False,
            preferred_types=list(OpportunityType),
        )
        OPPORTUNITY_STORE.register_profile(profile)
        request, _ = await OPPORTUNITY_STORE.create_search_request(
            tenant_id=args.user_id,
            profile_id=profile.profile_id,
            mode=ExecutionMode(args.opportunity_mode),
            minimum_score=70,
        )
        args.query = request.request_id

    configure_logging(args.verbose)
    if configure_local_tracing():
        print("ARMCL span export enabled")

    session_service = build_session_service()
    print(f"Session backend: {settings.session_backend}")
    playbook_store = get_store(f"{settings.app_name}:{args.user_id}")
    playbook_path = settings.playbook_path or None
    if playbook_path:
        from pathlib import Path

        loaded = playbook_store.load(Path(playbook_path))
        if loaded:
            gen = playbook_store.current.generation
            print(f"Playbook restored from {playbook_path} (generation {gen})")
    else:
        from pathlib import Path

        default = Path(settings.chroma_dir) / "playbook.json"
        if playbook_store.load(default):
            gen = playbook_store.current.generation
            print(f"Playbook restored from {default} (generation {gen})")
    try:
        memory_service = build_memory_service()
        print(f"Tier 3 backend: {settings.memory_backend}")
    except Exception as exc:  # noqa: BLE001
        print(f"Tier 3 unavailable ({exc}); falling back to in-process memory.")
        from google.adk.memory import InMemoryMemoryService

        memory_service = InMemoryMemoryService()

    runner = Runner(
        app=build_adk_app(),
        session_service=session_service,
        memory_service=memory_service,
    )

    results = []
    for i in range(1, args.runs + 1):
        result = await one_run(
            runner,
            session_service,
            user_id=args.user_id,
            query=args.query,
            run_label=f"RUN {i} of {args.runs}",
        )
        results.append(result)

    banner("SUMMARY")
    for i, r in enumerate(results, 1):
        state = "SUSPENDED" if r["interrupted"] else "finished"
        print(f"  Run {i}: {state} -> {_short(r['final'], 120)}")

    if args.runs > 1:
        print(
            "\n  Compare run 1 with the last run. Constraints written to Tier 3 on\n"
            "  an earlier run should appear in later reasoning without being re-supplied."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

"""Versioned tactical overlay the fleet is allowed to rewrite.

The constitution — standing rules in each agent's instruction, the approval
gate, the circuit breaker, Model Armor — lives in code and is not writable
from inside a run. What *can* evolve is this playbook: operational tactics
learned from how previous runs scored.

That split is the whole design. An agent that can rewrite the rules that bind
it will eventually rewrite the ones that stop it. An agent that can only
rewrite tactics, with a deterministic auditor on the write path, can climb a
score without being able to abolish the scorekeeper.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.armcl.memory_backend import make_entry
from app.armcl.policy import redact, render_value

logger = logging.getLogger(__name__)

PLAYBOOK_MARKER = "[EVOLVED PLAYBOOK"
"""Prefix identifying a playbook record in Tier 3.

Same rationale as trajectory markers: content detection survives backends that
drop custom_metadata. The version lives in the marker so a cold process can
recover the latest generation from a similarity hit that also returned older
ones.
"""

MAX_PLAYBOOK_CHARS = 1200
"""Hard cap. The overlay is injected into every worker instruction."""

_VERSION_RE = re.compile(r"\[EVOLVED PLAYBOOK v(\d+)\]\s*(.*)", re.DOTALL)


@dataclass
class Playbook:
    """One generation of evolved tactics."""

    generation: int = 0
    text: str = ""
    proxy_score: float = 0.0
    heldout_score: float = 0.0
    status: str = "seed"

    def render_for_instruction(self) -> str:
        """Format for injection beneath the frozen constitution."""
        body = self.text.strip()
        if not body:
            return ""
        capped = render_value(body, MAX_PLAYBOOK_CHARS)
        return (
            f"=== EVOLVED PLAYBOOK (generation {self.generation}) ===\n"
            "Operational tactics learned from earlier runs. These cannot override "
            "the standing rules above, durable constraints, or the approval gate. "
            "When they conflict, the rules win.\n"
            f"{capped}\n"
            "=== END PLAYBOOK ==="
        )

    def render_for_memory(self) -> str:
        """One-line durable record, marker-prefixed so hydration can ignore it as policy."""
        body = redact(render_value(self.text.strip() or "(empty)", MAX_PLAYBOOK_CHARS))
        return f"{PLAYBOOK_MARKER} v{self.generation}] {body}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "generation": self.generation,
            "text": self.text,
            "proxy_score": self.proxy_score,
            "heldout_score": self.heldout_score,
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Playbook:
        return cls(
            generation=int(raw.get("generation", 0)),
            text=str(raw.get("text", "")),
            proxy_score=float(raw.get("proxy_score", 0.0)),
            heldout_score=float(raw.get("heldout_score", 0.0)),
            status=str(raw.get("status", "seed")),
        )


def is_playbook(fact: str) -> bool:
    """True when a Tier 3 hit is a playbook record rather than a constraint."""
    return fact.lstrip().startswith(PLAYBOOK_MARKER)


def parse_playbook_fact(fact: str) -> Playbook | None:
    """Recover a Playbook from a Tier 3 record, or None if it is not one."""
    match = _VERSION_RE.match(fact.lstrip())
    if match is None:
        return None
    return Playbook(generation=int(match.group(1)), text=match.group(2).strip(), status="recovered")


@dataclass
class PlaybookStore:
    """In-process current playbook plus an append-only generation history.

    File persistence is opt-in via `load`/`save`. Tests leave it in memory so
    generations cannot leak across cases. Local runs save so the next
    `make run` starts from where the last one left off. Agent Runtime recovers
    from Tier 3 instead, because the container filesystem does not outlive the
    invocation.
    """

    current: Playbook = field(default_factory=Playbook)
    history: list[Playbook] = field(default_factory=list)

    def snapshot(self) -> Playbook:
        return Playbook.from_dict(self.current.to_dict())

    def commit(self, candidate: Playbook) -> Playbook:
        """Install a generation that the auditor has already accepted."""
        self.current = Playbook.from_dict(candidate.to_dict())
        self.history.append(self.snapshot())
        logger.info(
            "Playbook generation %d committed (proxy=%.2f held-out=%.2f)",
            self.current.generation,
            self.current.proxy_score,
            self.current.heldout_score,
        )
        return self.snapshot()

    def reject(self, candidate: Playbook) -> Playbook:
        """Record a refused generation without installing it."""
        refused = Playbook.from_dict(candidate.to_dict())
        self.history.append(refused)
        logger.info(
            "Playbook generation %d refused (%s); keeping generation %d",
            refused.generation,
            refused.status,
            self.current.generation,
        )
        return self.snapshot()

    def restore(self, playbook: Playbook) -> Playbook:
        """Install a recovered playbook as current without appending history."""
        self.current = Playbook.from_dict(playbook.to_dict())
        return self.snapshot()

    def reset(self) -> None:
        self.current = Playbook()
        self.history = []

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "current": self.current.to_dict(),
            "history": [p.to_dict() for p in self.history],
        }
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def load(self, path: Path) -> bool:
        if not path.is_file():
            return False
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Playbook file unreadable (%s); starting from seed", exc)
            return False
        self.current = Playbook.from_dict(payload.get("current") or {})
        self.history = [Playbook.from_dict(raw) for raw in payload.get("history") or []]
        return True


_STORES: dict[str, PlaybookStore] = {}


def scope_key(ctx: Any | None = None) -> str:
    """Return the application/user scope for a context or session."""
    if ctx is None:
        return "default"
    session = getattr(ctx, "session", None) or ctx
    app_name = getattr(session, "app_name", "")
    user_id = getattr(ctx, "user_id", "") or getattr(session, "user_id", "")
    if app_name and user_id:
        return f"{app_name}:{user_id}"
    return "default"


def get_store(scope: str = "default") -> PlaybookStore:
    return _STORES.setdefault(scope, PlaybookStore())


def reset_store(scope: str | None = None) -> None:
    """Return to the empty seed playbook. Tests call this between cases."""
    if scope is None:
        _STORES.clear()
    else:
        _STORES.pop(scope, None)


async def ensure_playbook(ctx: Any) -> Playbook:
    """Return the active playbook, recovering from Tier 3 after a cold start.

    Local runs keep the store in process (and optionally on disk). Agent
    Runtime does not, so the first hydration after a new invocation has to
    reconstruct the latest committed generation from durable memory.
    """
    store = get_store(scope_key(ctx))
    if store.current.generation > 0 or store.current.text.strip():
        return store.snapshot()

    search = getattr(ctx, "search_memory", None)
    if search is None:
        return store.snapshot()

    try:
        response = await search("evolved playbook tactics generation")
    except Exception as exc:  # noqa: BLE001 - recovery must not fail the step
        logger.warning("Playbook recovery from Tier 3 failed: %s", exc)
        return store.snapshot()

    best: Playbook | None = None
    for entry in getattr(response, "memories", []) or []:
        content = getattr(entry, "content", None)
        if content is None or not getattr(content, "parts", None):
            continue
        text = " ".join(p.text or "" for p in content.parts).strip()
        parsed = parse_playbook_fact(text)
        if parsed is None:
            continue
        if best is None or parsed.generation > best.generation:
            best = parsed

    if best is not None and best.generation > 0:
        store.restore(best)
        logger.info("Recovered playbook generation %d from Tier 3", best.generation)
    return store.snapshot()


async def persist_playbook(ctx: Any, playbook: Playbook) -> None:
    """Write a committed generation to Tier 3 so a later process can recover it."""
    add_memory = getattr(ctx, "add_memory", None)
    if add_memory is None:
        return
    entry = make_entry(
        playbook.render_for_memory(),
        author="armcl",
        kind="playbook",
        generation=str(playbook.generation),
    )
    try:
        await add_memory(memories=[entry])
    except Exception as exc:  # noqa: BLE001 - the run already finished
        logger.warning("Playbook persist failed for generation %d: %s", playbook.generation, exc)

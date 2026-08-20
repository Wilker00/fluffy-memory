"""The three ARMCL tiers, expressed over ADK primitives.

  Tier 1  Active scratchpad     ctx.state, namespaced under armcl:t1:
  Tier 2  Episodic thread state ADK session events and session state
  Tier 3  Semantic long-term    BaseMemoryService (Memory Bank or Chroma)

Nothing here invents its own storage. Each tier is a typed view over something
ADK already persists, which is what lets the same code run locally against
in-memory services and on Agent Runtime against managed sessions and Memory
Bank without modification.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

T1_PREFIX = "armcl:t1:"
T1_CHECKPOINT_PREFIX = "armcl:ckpt:"
T2_LEDGER_KEY = "armcl:t2:ledger"
T2_BREAKER_KEY = "armcl:t2:breaker"
T2_REJECTED_KEY = "armcl:t2:rejected"


class HasState(Protocol):
    """Minimal surface ARMCL needs. Satisfied by ADK's Context."""

    @property
    def state(self) -> dict[str, Any]: ...


@dataclass
class LedgerEntry:
    """One reconciled step in the episodic record.

    `status` is what the step reported at the time it ran. `outcome` is how it
    was later judged, which is only knowable once verification has happened.
    Keeping them separate is what makes the ledger a usable trajectory record:
    a step can succeed on its own terms and still be part of a rejected
    attempt, and collapsing the two loses exactly the signal worth learning
    from.
    """

    step: str
    keys_written: list[str]
    bytes_pruned: int
    summary: str
    values: dict[str, Any] = field(default_factory=dict)
    status: str = "SUCCESS"
    outcome: str = "PENDING"

    def to_dict(self) -> dict[str, Any]:
        return {
            "step": self.step,
            "keys_written": self.keys_written,
            "bytes_pruned": self.bytes_pruned,
            "summary": self.summary,
            "values": self.values,
            "status": self.status,
            "outcome": self.outcome,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> LedgerEntry:
        return cls(
            step=raw.get("step", "?"),
            keys_written=list(raw.get("keys_written", [])),
            bytes_pruned=int(raw.get("bytes_pruned", 0)),
            summary=raw.get("summary", ""),
            values=dict(raw.get("values", {})),
            status=raw.get("status", "SUCCESS"),
            outcome=raw.get("outcome", "PENDING"),
        )


def matches_key(stored: str, wanted: str) -> bool:
    """True when a stored key is the dotted-path form of the wanted name.

    Distillation flattens nested payloads to dotted keys, so a fact an agent
    knows as `risk_level` is stored as `facts.risk_level`. Matching on the
    final segment is what keeps gap analysis honest about what it actually
    holds; without it the loop reports a gap for a value sitting in the frame.
    """
    return stored == wanted or stored.rsplit(".", 1)[-1] == wanted


class Tier1:
    """Ephemeral scratchpad for the current run.

    Backed by ctx.state so ADK persists it with the session; the namespace
    prefix keeps ARMCL's bookkeeping from colliding with agent output keys.
    """

    def __init__(self, ctx: HasState) -> None:
        self._state = ctx.state

    def resolve(self, key: str) -> str | None:
        """Find the stored name for a requested key, or None.

        Exact matches win outright. Otherwise a dotted key whose last segment
        matches is accepted, but only when exactly one candidate exists.
        Several candidates means the request is genuinely ambiguous (three
        discovered items each carry an `item_id`), and inventing a winner
        there would hand an agent the wrong entity with full confidence.
        Reporting the gap is the safe answer.
        """
        if f"{T1_PREFIX}{key}" in self._state:
            return key

        candidates = [
            stored for stored in self.snapshot() if stored != key and matches_key(stored, key)
        ]
        return candidates[0] if len(candidates) == 1 else None

    def get(self, key: str, default: Any = None) -> Any:
        resolved = self.resolve(key)
        if resolved is None:
            return default
        return self._state.get(f"{T1_PREFIX}{resolved}", default)

    def set(self, key: str, value: Any) -> None:
        self._state[f"{T1_PREFIX}{key}"] = value

    def merge(self, values: dict[str, Any]) -> list[str]:
        for key, value in values.items():
            self.set(key, value)
        return list(values)

    def has(self, key: str) -> bool:
        return self.resolve(key) is not None

    def missing(self, required: list[str]) -> list[str]:
        """Gap analysis: which required parameters are absent."""
        return [key for key in required if not self.has(key)]

    def snapshot(self) -> dict[str, Any]:
        return {k[len(T1_PREFIX) :]: v for k, v in self._state.items() if k.startswith(T1_PREFIX)}

    def evict(self, keys: list[str]) -> None:
        for key in keys:
            self._state.pop(f"{T1_PREFIX}{key}", None)

    # -- checkpointing ---------------------------------------------------
    def checkpoint(self, label: str) -> None:
        """Snapshot the scratchpad so a failed attempt can be undone.

        Stored under its own prefix so the checkpoint is invisible to
        `snapshot()` and never reaches an agent's context frame.
        """
        self._state[f"{T1_CHECKPOINT_PREFIX}{label}"] = dict(self.snapshot())

    def restore(self, label: str) -> list[str]:
        """Roll the scratchpad back to a checkpoint.

        Without this, a rejected attempt's output stays in the scratchpad and
        the retry re-plans while looking at the artifact that just failed.
        Errors compound forward instead of being undone.

        Returns:
            The keys discarded by the rollback. Empty when no such checkpoint
            exists, in which case current state is left alone.
        """
        saved = self._state.get(f"{T1_CHECKPOINT_PREFIX}{label}")
        if saved is None:
            return []

        discarded = [key for key in self.snapshot() if key not in saved]
        self.evict(discarded)
        for key, value in saved.items():
            self._state[f"{T1_PREFIX}{key}"] = value
        return discarded


class Tier2:
    """Episodic record of the current task.

    The ledger is a compact, append-only list of what each step produced.
    Session events hold the full history, but replaying them costs tokens; the
    ledger is what hydration actually reads.
    """

    def __init__(self, ctx: HasState) -> None:
        self._state = ctx.state

    def append(self, entry: LedgerEntry) -> None:
        ledger = list(self._state.get(T2_LEDGER_KEY, []))
        ledger.append(entry.to_dict())
        self._state[T2_LEDGER_KEY] = ledger

    def recent(self, limit: int = 3) -> list[LedgerEntry]:
        ledger = self._state.get(T2_LEDGER_KEY, [])
        return [LedgerEntry.from_dict(raw) for raw in ledger[-limit:]]

    def all_entries(self) -> list[LedgerEntry]:
        return [LedgerEntry.from_dict(raw) for raw in self._state.get(T2_LEDGER_KEY, [])]

    def find_value(self, key: str) -> Any:
        """Search backwards through the ledger for a previously written key.

        This is what closes the dependency gap: a step that needs an identifier
        produced several steps earlier recovers it here instead of stalling to
        ask the operator. Keys are matched on their final dotted segment for
        the same reason Tier 1 resolves them that way.
        """
        for raw in reversed(self._state.get(T2_LEDGER_KEY, [])):
            values = raw.get("values", {}) or {}
            if key in values:
                return values[key]

            candidates = [stored for stored in values if matches_key(stored, key)]
            if len(candidates) == 1:
                return values[candidates[0]]

            # Backward compatibility for ledgers created before typed values
            # were added. New entries never need to parse their human summary.
            written = [stored for stored in raw.get("keys_written", []) if matches_key(stored, key)]
            if len(written) == 1:
                prefix = f"{written[0]}="
                for part in str(raw.get("summary", "")).split("; "):
                    if part.startswith(prefix):
                        return part[len(prefix) :]
        return None

    def record_outcome(self, step: str, outcome: str) -> bool:
        """Label how a step was ultimately judged.

        Stamps the most recent entry for that step. Entries are never removed,
        so the ledger stays an audit record; this only fills in a verdict that
        was not knowable when the step ran. Without it every trajectory reads
        as a success and the record cannot distinguish good runs from bad.
        """
        ledger = list(self._state.get(T2_LEDGER_KEY, []))
        for raw in reversed(ledger):
            if raw.get("step") == step:
                raw["outcome"] = outcome
                self._state[T2_LEDGER_KEY] = ledger
                return True
        return False

    def label_pending(self, outcome: str) -> int:
        """Apply a terminal outcome to every step still awaiting judgement."""
        ledger = list(self._state.get(T2_LEDGER_KEY, []))
        labelled = 0
        for raw in ledger:
            if raw.get("outcome", "PENDING") == "PENDING":
                raw["outcome"] = outcome
                labelled += 1
        self._state[T2_LEDGER_KEY] = ledger
        return labelled

    # -- circuit breaker -------------------------------------------------
    def record_rejection(self) -> int:
        count = int(self._state.get(T2_BREAKER_KEY, 0)) + 1
        self._state[T2_BREAKER_KEY] = count
        return count

    def record_rejected_artifact(self, artifact: Any) -> None:
        """Remember an artifact the critic refused, to detect non-convergence."""
        if artifact in (None, ""):
            return
        seen = list(self._state.get(T2_REJECTED_KEY, []))
        seen.append(str(artifact))
        self._state[T2_REJECTED_KEY] = seen

    @property
    def rejected_artifacts(self) -> list[str]:
        return list(self._state.get(T2_REJECTED_KEY, []))

    def reset_rejections(self) -> None:
        self._state[T2_BREAKER_KEY] = 0

    @property
    def rejection_count(self) -> int:
        return int(self._state.get(T2_BREAKER_KEY, 0))


@dataclass
class ContextFrame:
    """The dynamic sliding frame handed to an agent before it acts.

    Deliberately small. The point of ARMCL is that an agent sees the few facts
    that matter for the current step rather than the entire history.
    """

    scratchpad: dict[str, Any] = field(default_factory=dict)
    recent_steps: list[LedgerEntry] = field(default_factory=list)
    retrieved_facts: list[str] = field(default_factory=list)
    prior_outcomes: list[str] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    hydrated_from: list[str] = field(default_factory=list)

    def render(self) -> str:
        """Format for injection into an agent instruction.

        Every value is capped. The frame is prepended to an agent's system
        instruction on each turn, so one oversized field here would reintroduce
        the context bloat the whole loop exists to prevent.
        """
        from app.armcl.policy import render_value

        lines = ["=== ARMCL CONTEXT FRAME ==="]

        if self.scratchpad:
            lines.append("Active state:")
            lines += [f"  {k} = {render_value(v)}" for k, v in self.scratchpad.items()]

        if self.recent_steps:
            lines.append("Recent steps:")
            lines += [
                f"  [{e.status}] {e.step}: {render_value(e.summary, 240)}"
                for e in self.recent_steps
            ]

        if self.retrieved_facts:
            lines.append("Durable organizational memory:")
            lines += [f"  - {render_value(fact, 240)}" for fact in self.retrieved_facts]

        # Kept in its own section rather than merged above. A constraint binds;
        # a prior outcome is evidence about what has already been tried. An
        # agent shown both under one heading will either obey history as if it
        # were policy or discount policy as if it were history.
        if self.prior_outcomes:
            lines.append("How previous runs on this item ended:")
            lines += [f"  - {render_value(outcome, 240)}" for outcome in self.prior_outcomes]

        if self.gaps:
            lines.append(f"Unresolved parameters: {', '.join(self.gaps)}")

        lines.append("=== END CONTEXT FRAME ===")
        return "\n".join(lines)

    @property
    def is_empty(self) -> bool:
        return not (
            self.scratchpad or self.recent_steps or self.retrieved_facts or self.prior_outcomes
        )

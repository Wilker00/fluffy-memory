"""Agent Registry: capability contracts and conformance checking.

Manifests in `manifests/` declare what each agent is allowed to do. This module
loads them and checks the running graph against them, which is the difference
between a registry that documents intent and one that catches drift.

The checks are cheap and run in the test suite, so a tool added to an agent
without updating its contract fails CI rather than shipping silently.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

MANIFEST_DIR = Path(__file__).resolve().parent.parent / "manifests"


@dataclass
class TierScope:
    read: bool = False
    write: bool = False
    rationale: str = ""


@dataclass
class AgentManifest:
    """A declared capability contract."""

    agent_id: str
    version: str
    display_name: str
    description: str
    model: str
    capabilities: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    memory_scopes: dict[str, TierScope] = field(default_factory=dict)
    guardrails: dict[str, str] = field(default_factory=dict)
    failure_policy: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AgentManifest:
        scopes = {
            tier: TierScope(
                read=bool(spec.get("read")),
                write=bool(spec.get("write")),
                rationale=spec.get("rationale", ""),
            )
            for tier, spec in (data.get("memory_scopes") or {}).items()
        }
        return cls(
            agent_id=data["agent_id"],
            version=data["version"],
            display_name=data.get("display_name", data["agent_id"]),
            description=data.get("description", ""),
            model=data["model"],
            capabilities=list(data.get("capabilities", [])),
            tools=list(data.get("tools", [])),
            memory_scopes=scopes,
            guardrails={k: str(v) for k, v in (data.get("guardrails") or {}).items()},
            failure_policy=dict(data.get("failure_policy") or {}),
            raw=data,
        )

    def may_write_durable_memory(self) -> bool:
        scope = self.memory_scopes.get("tier3")
        return bool(scope and scope.write)


def load_manifests(directory: Path | None = None) -> dict[str, AgentManifest]:
    """Load every manifest, keyed by agent_id."""
    directory = directory or MANIFEST_DIR
    manifests: dict[str, AgentManifest] = {}
    for path in sorted(directory.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        manifest = AgentManifest.from_dict(data)
        manifests[manifest.agent_id] = manifest
    return manifests


def describe_fleet(directory: Path | None = None) -> str:
    """Human-readable catalog. What a discovery endpoint would serve."""
    manifests = load_manifests(directory)
    lines = ["fluffy-memory fleet capability catalog", "=" * 60]
    for manifest in manifests.values():
        durable = "yes" if manifest.may_write_durable_memory() else "no"
        lines += [
            f"\n{manifest.display_name}  ({manifest.agent_id} v{manifest.version})",
            f"  model            {manifest.model}",
            f"  capabilities     {', '.join(manifest.capabilities) or 'none'}",
            f"  tools            {', '.join(manifest.tools) or 'none'}",
            f"  writes Tier 3    {durable}",
        ]
    return "\n".join(lines)


def check_conformance(agents: dict[str, Any]) -> list[str]:
    """Compare running agents against their contracts.

    Returns a list of violations; empty means conformant.

    Args:
        agents: Mapping of agent_id to the live ADK agent object.
    """
    manifests = load_manifests()
    violations: list[str] = []

    for agent_id, agent in agents.items():
        manifest = manifests.get(agent_id)
        if manifest is None:
            violations.append(f"{agent_id}: running but has no manifest")
            continue

        model = getattr(agent, "model", None)
        if isinstance(model, str) and model != manifest.model:
            violations.append(f"{agent_id}: running model {model!r} != declared {manifest.model!r}")

        declared = set(manifest.tools)
        actual = {
            getattr(t, "__name__", getattr(t, "name", str(t)))
            for t in (getattr(agent, "tools", None) or [])
        }
        for undeclared in sorted(actual - declared):
            violations.append(f"{agent_id}: uses undeclared tool {undeclared!r}")
        for unused in sorted(declared - actual):
            violations.append(f"{agent_id}: declares unused tool {unused!r}")

    for agent_id in manifests:
        if agent_id not in agents:
            violations.append(f"{agent_id}: manifest exists but agent is not in the graph")

    return violations


if __name__ == "__main__":
    print(describe_fleet())

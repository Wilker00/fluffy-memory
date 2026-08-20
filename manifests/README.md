# Agent Registry manifests

Declarative capability contracts for each agent in the fleet: what it does,
which tools it may call, which memory tiers it may read and write, and which
model it runs on.

Two reasons these are files rather than code.

**Discovery.** The Fortified Enterprise Fleet track asks how an organization
discovers your agents. A manifest is what a registry ingests, and keeping them
versioned in the repo means the catalog is reviewable in a pull request rather
than configured by hand in a console.

**Memory isolation, stated explicitly.** Each manifest declares its tier
permissions. `memory.write` on Tier 3 is the interesting one: only the critic
and the analyst may write durable organizational memory, because Tier 3 shapes
every future run and a scout that could write to it would let a single bad
retrieval become permanent policy. The evolver is deliberately excluded: it
proposes tactics, and a deterministic auditor is the only writer of the
playbook. An agent that both rewrites its instructions and installs the
rewrite will game the metric.

## Schema

| Field | Meaning |
| --- | --- |
| `agent_id` | Stable identifier |
| `version` | Semantic version of the contract |
| `model` | Pinned model. Must be Gemini 3.5 or newer |
| `capabilities` | What the agent is for, in discovery terms |
| `tools` | Tools it may invoke. Anything absent is out of scope |
| `memory_scopes` | Per-tier read and write permissions |
| `guardrails` | Which screening applies at which boundary |
| `failure_policy` | Retry and escalation behaviour |

## Current state

These manifests describe the fleet accurately and are loaded by
`app/registry.py` to assert that the running graph matches its declared
contract. Publishing them to a hosted Agent Registry is a deployment step, not
a code change.

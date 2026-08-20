# Architecture

Design decisions and the reasoning behind them. Several are responses to ADK 2.0
behaviour that is easy to get wrong, and each of those was verified against the
installed package rather than taken from documentation.

---

## 1. ARMCL is a loop, not a store

The distinction matters. A memory *store* is queried when an agent decides to
query it, which means an agent that does not realise it is missing context will
not ask for it — the exact failure mode of long workflows.

ARMCL instead injects two hooks into every step:

**Pre-action hydration** (`app/armcl/hydrate.py`) runs before a node acts. It
performs gap analysis against Tier 1, recovers missing keys from the Tier 2
ledger, and issues one Tier 3 similarity query. The result is a *context frame*
prepended to the agent's instruction. The agent never asks; the context is
already there.

**Post-action reconciliation** (`app/armcl/reconcile.py`) runs after output is
produced. It distils the payload into structured deltas, classifies each by
salience, writes them to the appropriate tier, and discards the bulk.

Hydration is deliberately cheap: a ledger read plus at most one vector query. It
runs before every node, so anything more expensive would dominate the workflow's
cost.

## 2. The tiers are views over ADK primitives

No bespoke storage. Tier 1 is `ctx.state` under an `armcl:t1:` prefix. Tier 2 is
session state plus events. Tier 3 is a `BaseMemoryService`.

This is what lets identical code run locally against in-memory services and on
Agent Runtime against managed sessions and Memory Bank, with the backend chosen
by one environment variable.

### Why Tier 2 keeps a ledger

Session events hold the full history, but replaying them costs tokens
proportional to task length. The ledger is a compact append-only record of what
each step *produced* — keys written, bytes pruned, a one-line summary — and
that is what hydration actually reads. `Tier2.find_value()` searches it
backwards to close dependency gaps.

## 3. Salience decides the tier, and size beats key names

`app/armcl/policy.py` classifies every extracted fact:

- **DURABLE** — constraints, policies, verdicts. These change future decisions,
  so they go to Tier 3 and outlive the session.
- **EPISODIC** — identifiers, intermediate values. Useful within the task.
- **EPHEMERAL** — bulk. Dropped after the step.

Size is checked **before** the key hints. A field named `policy_document`
holding 5000 characters is bulk regardless of its name, and promoting it to
Tier 3 would poison durable memory with a payload retrieved on every subsequent
query. This ordering was added after observing exactly that: a large value with
a constraint-shaped key was being carried into the context frame and undoing the
compression.

Every rendered value is independently capped. Capping only the joined summary
would let one oversized field crowd out every other fact.

### Redaction happens on write, not read

`redact()` runs over delta *values*, not just summaries, before anything reaches
Tier 1. Tier 1 is persisted as session state, so a credential left in a value
would outlive the step that observed it and remain retrievable indefinitely.
Model Armor screens content crossing trust boundaries; this is the narrower
guarantee that secrets never enter memory at all.

## 4. Guardrails return verdicts and never raise

`app/guardrails/armor.py` returns a `GuardrailVerdict`. It does not raise.

ADK 2.0 catches exceptions from nodes to apply `RetryConfig`. A `raise` on a
blocked document would re-run the block two more times, triple the cost, and log
three failures for one malicious input. A block is a *decision*, not a transient
fault, so it travels as data and the graph routes on it.

Failure direction differs by direction of travel:

- **Inbound fails closed.** If screening is unavailable, untrusted input is
  treated as blocked. Unscreened external content reaching an agent is the
  indirect prompt-injection path.
- **Outbound fails open.** A screening outage should not silently discard work
  already done. The verdict records `UNAVAILABLE`, so the gap is visible in the
  trace rather than hidden.

The offline fallback is named honestly. `GUARDRAIL_BACKEND=regex` selects a
heuristic matcher that is emphatically not Model Armor, every verdict carries
the backend that produced it, and a demo should show `model_armor`.

## 5. Routing is emitted through `ctx.route`

This one is a genuine trap. The ADK 2.0 documentation shows a router returning
`Event(route=[...])`. Against `google-adk` 2.7.1 that is silently wrong: `Event`
has no `route` field, pydantic discards the argument, and the router's
conditional edges never match. The branch simply ends, with only a log line to
show for it.

The correct form sets the route on the context:

```python
async def route_assessment(ctx: Context, node_input):
    ctx.route = "DECLINE"     # matched against the edge dict
    return node_input         # this sets output, not the route
```

Both routers normalise unrecognised values onto a named route before emitting,
so no `DEFAULT_ROUTE` edge is needed. That is also required: a `DEFAULT_ROUTE`
pointing at a node already named in the same dict is rejected by the graph
validator as a duplicate edge.

## 6. The approval gate is an agent, not a function node

`ctx.request_confirmation()` requires a tool context — it keys the pending
confirmation by `function_call_id`, which only exists inside a tool call. A
plain function node calling it raises
`ValueError: request_confirmation requires function_call_id`.

So the gate is an agent with a single confirmation tool
(`app/agents/approver.py`). The tool writes pending state through ARMCL *before*
suspending, because execution does not return to that point with in-process
state intact.

The pause is the interesting part for ARMCL. Agent Runtime persists the workflow
and releases compute; the run may resume much later in a different process.
Nothing held in memory survives, so when the executor continues it recovers the
identifier and the approved plan from Tier 1 and Tier 2. The gate is marked
`rerun_on_resume=False` so the operator is not asked twice.

This is native ADK resumability, not only a managed-hosting assumption:
`app/app_config.py` constructs an `App` with
`ResumabilityConfig(is_resumable=True)`, and the approval function is wrapped in
`LongRunningFunctionTool`. Local runs use `DatabaseSessionService` by default.
Because resume is at-least-once, `act` receives a stable idempotency key derived
from the durable session, item, and plan. A Pub/Sub redelivery re-enters an
unfinished session but suppresses one whose terminal event is already present.

## 7. Failure tolerance is structural

Three mechanisms, none of which rely on asking a model to behave:

**Retries.** Every agent node carries
`RetryConfig(max_attempts=3, initial_delay=1.0, backoff_factor=2.0)`. Verified
working: a node failing twice recovers on the third attempt with
`ctx.attempt_count` incrementing.

**No broad exception handling.** Nothing wraps a node body in
`except Exception` — that would disable `RetryConfig` by consuming the failure.
Catching `BaseException` would be worse: `NodeInterruptedError` derives from it
specifically so user code cannot swallow it, and doing so would break
human-in-the-loop pause entirely.

**Circuit breaker.** The critic's REJECT edge cycles back to the analyst. Cycles
are how agent systems hang, so `route_judgement` counts rejections in Tier 2 and
diverts to `circuit_broken` at the threshold. The bound is enforced in the graph,
not requested in a prompt. `tests/test_graph_routing.py` asserts a permanently
rejecting critic cannot loop more than the threshold.

## 8. Observability targets the invisible part

ADK instruments agent and tool calls already. Memory operations are the
interesting part of this system and are invisible by default, so
`app/observability/spans.py` wraps hydration and reconciliation in spans
carrying the keys consulted, hit and miss counts, bytes pruned, compression
ratio, and the reason memory was queried.

Attributes carry keys, scores, and counts — never memory content. Prompt and
response payloads are routed to Cloud Logging by the platform, and duplicating
them into spans would put sensitive text somewhere with weaker access controls.

Telemetry never breaks execution: every span helper swallows its own errors. A
broken exporter must not take the fleet down.

## 9. The domain seam

`app/tools/protocol.py` defines four operations. They are not arbitrary — each
creates a condition ARMCL must handle:

| Operation | Condition it creates |
| --- | --- |
| `discover` | Produces an identifier a later step needs → dependency gap |
| `inspect` | Returns bulk around a few useful fields → distillation pressure |
| `act` | The real-world effect → the step worth pausing before |
| `verify` | Can legitimately fail → gives the critic something to reject |

A domain implementing these four methods runs on the fleet unchanged. The
synthetic workload in `app/reference/` implements them badly on purpose: 4000
lines of telemetry per inspection, one unit that fails verification exactly
once, and one carrying a constraint that should cause a decline on a later run.

## 10. Deliberate omissions

**Agent Gateway.** Requires organization-level configuration a personal account
may not have. `make smoke` reports whether it is reachable rather than assuming.

**Redis.** Tier 2 is session state. Adding a second datastore for data ADK
already persists would be infrastructure for its own sake.

**A model call for delta extraction.** Distillation is deterministic. It runs
after every tool call, so a model round trip there would multiply latency and
cost across the whole workflow for a job that key-shape heuristics do
adequately. Raw session-event extraction is opt-in because it can bypass the
redaction boundary; the default durable path writes only explicitly distilled,
redacted facts. `ARMCL_SYNC_RAW_SESSION_EVENTS=true` enables managed semantic
extraction when the deployment's data policy permits full event retention.

## 11. Self-evolution rewrites tactics, never the constitution

Trajectory memory tells a later run what already happened. That is recall, not
improvement. The teaching pattern is the next step: after a run, rewrite the
instructions, watch the score climb, and catch the rewrite that gamed the
metric.

The constitution is not writable from inside a run. Standing rules live in
agent instructions, the approval gate is a graph edge, the circuit breaker is
a counter in Tier 2, and Model Armor returns a verdict. None of those are
prompt text the evolver can replace.

What *can* evolve is a versioned playbook of operational tactics, injected
beneath the constitution and labelled as such. Scout, analyst, and executor
see it. The critic and the approval gate do not — they are the scorekeepers,
and a rewrite that said "be lenient" must not grade itself.

The evolver is a language model. It sees the proxy score (completions good,
halts bad, rejections costly) and the ledger, and it proposes a complete
replacement playbook. It has no tools and no reconciling callback: distilling
its output into ARMCL would promote tactics into durable policy.

The auditor owns the metric and write path. Its static layer is deterministic;
its behavioral layer uses an isolated Gemini call to execute the current and
candidate playbooks on hidden cases, then scores the structured decisions in
code. Four vetoes apply:

1. **Constitution freeze.** Patterns that disable the critic, skip approval,
   or ignore standing rules never install.
2. **Static held-out fixtures.** Cases the evolver was not shown. "Always decline"
   climbs the proxy — no rejections, no halts — and fails `clean_work_still_allowed`.
   That is the definition of gaming, and it rolls back.
3. **Behavioral evaluation.** Hidden clean-work, durable-policy, approval, and
   changed-retry scenarios must all behave correctly, and the candidate cannot
   regress from the installed generation.
4. **Size.** An overlay longer than the cap would crowd the constitution out
   of the context window.

A refused generation is recorded in history so the next proposal can see that
this shape already lost. The bound is in the write path, not in a request that
the rewriter behave.

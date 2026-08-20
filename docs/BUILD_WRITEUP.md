# Building ARMCL: what the docs didn't tell me

*A build write-up for the All Things Agentic Hackathon, Fortified Enterprise
Fleet track.*

---

## The problem I actually wanted to solve

Every long agent workflow I have watched fails the same way, and it is never
the reasoning. It is bookkeeping.

Step seven needs an identifier that step one produced. The identifier is
technically still in the transcript, four thousand lines of telemetry ago,
which is to say it is gone. So the agent stops and asks a human a question the
system already knew the answer to.

That is the failure I wanted to remove. Not "make the agent smarter" —
**make it stop forgetting things it was already told.**

ARMCL is the result: Autonomous Retrieval and Memory Context Loop. Two hooks
around every step of a multi-agent workflow. Before a step runs, work out what
it needs and go get it. After it runs, distil what it produced and file it by
how long it matters.

## Four failures, one loop

I made the reference workload deliberately hostile, because a memory engine
that only works on clean input is not a memory engine:

**Dependency gap.** `inspect_item()` needs an id produced four steps earlier.

**Noisy output.** A single inspection returns 4000 lines of sensor telemetry
around three fields that matter.

**Time gap.** High-risk items suspend for human approval. The workflow resumes
cold, potentially in a different process.

**Cross-session recall.** A constraint learned on run one has to change the
outcome of run three.

The last one is the whole thesis. Run one meets UNIT-7, discovers that Policy 14
requires a signed failover plan, and escalates. Run three starts with an empty
scratchpad and *declines* — citing a policy it never saw, that nobody
re-supplied.

## Three things the documentation got wrong

I want to be specific here, because these cost me real time and each one fails
in a way that produces no error message.

### 1. `Event(route=...)` silently does nothing

The ADK 2.0 docs show a conditional router returning an `Event` with a `route`:

```python
def router(node_input: str):
    return Event(route=["approved"])
```

Against `google-adk` 2.7.1, `Event` has no `route` field. It is a pydantic
model, so the argument is discarded without complaint. The router runs, the
node produces output, and every conditional edge fails to match. The branch
just ends. The only evidence is a log line:

```
Node 'router' has conditional/DEFAULT edges but none were matched
by the emitted route(s): None. The branch will end.
```

The working form sets the route on the context:

```python
async def route_assessment(ctx: Context, node_input):
    ctx.route = "DECLINE"     # this is what edges match against
    return node_input         # this sets output, which is a different thing
```

I found this by writing a fifteen-line probe workflow and running it, rather
than trusting the example. That habit paid for itself three times over.

### 2. `request_confirmation` doesn't work in a function node

Human-in-the-loop is the obvious way to gate a consequential action, and
`ctx.request_confirmation()` is the obvious API. Put it in a workflow function
node and you get:

```
ValueError: request_confirmation requires function_call_id.
This method can only be used in a tool context.
```

It keys the pending confirmation by `function_call_id`, which only exists
inside a tool call. So the approval gate has to be an *agent with a
confirmation tool*, not a node. That is a structural difference, not a
one-liner, and it is worth discovering on day two rather than day eleven.

### 3. Duplicate edges are a hard validation error

I wrote a routing dict with a `DEFAULT_ROUTE` fallback pointing at the same
node as a named route — a belt-and-braces default. The graph refused to build:

```
Graph validation failed. Duplicate edge found:
from=route_assessment, to=approval_gate
```

Fair enough. The fix was to normalise unknown values inside the router, which
is better anyway: the fallback is now logged rather than silent.

## The design decision I'd defend hardest

**Guardrails return verdicts. They do not raise.**

The natural instinct on detecting a prompt injection is to throw. Do that in
ADK and you have built a bug: the framework catches node exceptions to apply
`RetryConfig`, so a blocked document runs three times, costs three times as
much, and logs three failures for one malicious input.

A block is a *decision*, not a transient fault. So:

```python
class GuardrailVerdict(BaseModel):
    state: Literal["CLEAN", "BLOCKED", "UNAVAILABLE"]
    backend: Literal["model_armor", "regex"]
    filters_matched: list[str]
```

The verdict travels as data and the graph routes on it. Blocked content goes to
a quarantine node that records the attempt — evidence, not a swallowed error.

The direction of failure differs by direction of travel. Inbound screening
**fails closed**: if Model Armor is unreachable, untrusted input is treated as
blocked, because unscreened external content reaching an agent is the whole
attack. Outbound **fails open**, because a screening outage should not silently
discard work that already happened; the verdict records `UNAVAILABLE` so the
gap is visible in the trace instead of hidden.

There is also an offline heuristic fallback, and I made a point of naming it
honestly. `GUARDRAIL_BACKEND=regex` is a pattern matcher, it is not Model Armor,
and every verdict carries the backend that produced it. A demo that quietly
shows a regex while claiming enterprise guardrails is a demo that deserves to
lose.

## The bug that would have sunk the demo

I nearly shipped something embarrassing.

ARMCL classifies each extracted fact by salience — durable constraints go to
long-term memory, identifiers stay episodic, bulk gets dropped. The classifier
was keyed on the *field name*.

Then I ran it against a 5000-character telemetry blob and printed the context
frame:

```
Active state:
  primary_item_id = UNIT-7
  risk_level = high
  telemetry = xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx... [2000 more chars]
```

The engine whose entire purpose is preventing context bloat was injecting two
thousand characters of noise into every prompt.

Two fixes. Size is now checked *before* key hints, so a field called
`policy_document` holding 5000 characters is bulk regardless of its name. And
every rendered value is independently capped, because capping only the joined
summary lets one oversized field crowd out everything else.

The result on the reference workload: **99.7% of the raw payload pruned**, every
decision-relevant fact retained.

A related catch from the same session: redaction was running on summaries but
not on the delta *values* being written to Tier 1. Tier 1 is persisted as
session state — a credential left there outlives the step that saw it and stays
retrievable. Redaction now runs on write, before anything is stored.

## Bounding the loop that eats your budget

The critic can reject the executor's work, which sends the workflow back to the
analyst. That edge is a cycle, and cycles are how agent systems hang.

The instinct is to tell the model to converge. That is not a bound, it is a
hope. The actual bound counts rejections in Tier 2 and diverts to a halt node
at the threshold:

```python
count = t2.record_rejection()
if count >= settings.circuit_breaker_threshold:
    ctx.route = "HALT"
```

There is a test asserting that a permanently-rejecting critic cannot loop more
than three times. Halting with an explanation is the correct outcome for a
worker that will not converge; retrying forever burns budget and never
terminates.

## Letting it rewrite itself — and catching the cheat

Recall is not improvement. After a run the fleet now proposes a rewrite of its
own tactics and tries to climb a score. The standing rules, the approval gate,
and the circuit breaker stay in code; only a playbook of operational tactics
is writable.

The rewriter is a model. The auditor is not. The proxy score the evolver sees
rewards completions and penalises halts, which means "always decline" looks
like a clever improvement. The auditor scores a held-out set the evolver was
never shown, and that case fails it. The generation rolls back. Tests pin
both the climb and the catch, without a live model.

## What I'd tell someone starting this build

**Probe the framework before you write against it.** Three of my worst hours
were spent on documented behaviour that does not match the installed package.
A fifteen-line script that actually runs the API answers questions no amount of
re-reading does.

**Make the demo workload hostile on purpose.** My synthetic domain returns
4000 lines of noise, has a unit that fails verification exactly once, and
carries a constraint that only bites on a later run. Every one of those exists
to make a specific claim falsifiable.

**Keep the domain behind a seam.** Everything domain-specific sits behind a
four-method protocol. I still haven't settled the real domain, and it hasn't
blocked a single day of engine work.

**Name your fallbacks honestly.** Local Chroma instead of Memory Bank, regex
instead of Model Armor — fine, both are pragmatic. Reporting which one actually
ran is what makes the system trustworthy rather than merely demoable.

---

*Repo: [fluffy-memory](https://github.com/YOUR_USERNAME/fluffy-memory) —
Apache 2.0. Built with ADK 2.0, Gemini 3.5, Vertex AI Agent Runtime, Memory
Bank, Model Armor, and Cloud Scheduler → Pub/Sub → Cloud Run functions.*

*#AllThingsAgenticHackathon*

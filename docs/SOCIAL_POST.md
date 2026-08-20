# Social post drafts

Fill in the repo URL and demo link before posting. The hashtag
`#AllThingsAgenticHackathon` is required for the bonus point.

---

## X / Twitter (thread)

**1/**
Most long agent workflows don't fail at reasoning. They fail at bookkeeping.

Step 7 needs an ID from step 1. It's buried under 4,000 lines of telemetry. So
the agent stops and asks a human a question the system already knew.

I built ARMCL to fix that.

#AllThingsAgenticHackathon

**2/**
ARMCL = Autonomous Retrieval & Memory Context Loop.

Two hooks around every step:
→ before: work out what's missing, go get it
→ after: distil what happened, file it by how long it matters

Three tiers: scratchpad, episodic ledger, durable memory.

**3/**
The result on my test workload:

99.7% of raw payload pruned.
Every decision-relevant fact kept.

An agent that inspects 212,000 characters of telemetry carries three fields
forward. That's the difference between a workflow that finishes and one that
overflows.

**4/**
The demo I care about:

Run 1 — fleet meets UNIT-7, finds Policy 14 requires a signed failover plan,
escalates.

Run 3 — cold session, empty scratchpad. It *declines*, citing a policy it never
saw this run and nobody re-supplied.

That's cross-session memory doing real work.

**5/**
Three things the ADK 2.0 docs got wrong that cost me hours:

• `Event(route=...)` silently does nothing — set `ctx.route` instead
• `request_confirmation` only works in a tool context, not a function node
• duplicate edges in a routing dict are a hard validation error

**6/**
My favourite design call: guardrails return verdicts, they never raise.

ADK retries on exceptions. Raise on a blocked prompt injection and you run the
block three times.

A block is a decision, not a fault. It travels as data and the graph routes on
it.

**7/**
Built on ADK 2.0, Gemini 3.5, Vertex AI Agent Runtime + Memory Bank, Model
Armor, and Cloud Scheduler → Pub/Sub → Cloud Run functions.

Apache 2.0, full write-up on the trapdoors in the repo:
[REPO_URL]

#AllThingsAgenticHackathon

---

## LinkedIn

Most long-running agent workflows don't fail because the model reasons badly.
They fail at bookkeeping.

Step seven needs an identifier that step one produced. It's technically still
in the transcript — four thousand lines of telemetry ago, which is to say it's
gone. So the agent stops and asks a human a question the system already knew
the answer to.

For the All Things Agentic Hackathon I built **ARMCL** — an Autonomous
Retrieval & Memory Context Loop — to remove that failure mode. Not to make the
agent smarter, just to stop it forgetting what it was already told.

It's a loop rather than a store, which is the important distinction: a store
only gets queried when an agent realises it's missing something, and an agent
that doesn't realise won't ask. ARMCL injects two hooks into every step
instead. Before a step runs, it does gap analysis and recovers what's missing.
After it runs, it distils the output and files each fact by how long it
matters — ephemeral, episodic, or durable.

Measured results on the reference workload: 99.7% of raw payload pruned, every
decision-relevant fact retained.

The demo that matters: run one encounters a high-risk asset, discovers a policy
constraint, and escalates for approval. Run three starts with an empty
scratchpad and declines the same work — citing a constraint it never observed
in that session and that nobody re-supplied.

Three things I'd pass on to anyone building on ADK 2.0:

**Probe the framework before writing against it.** Three documented behaviours
didn't match the installed package, and each failed silently rather than
throwing. A fifteen-line script that actually calls the API answers questions
that re-reading the docs will not.

**Guardrails should return verdicts, not raise.** ADK catches node exceptions
to apply retry policy, so throwing on a detected prompt injection re-runs the
block three times. A block is a decision, not a transient fault.

**Bound your cycles structurally.** My critic can reject work and send it back
for revision. That's a cycle, and cycles are how these systems hang. The bound
is a counter in the graph, not an instruction in a prompt — with a test proving
a permanently-rejecting critic can't loop more than three times.

Built with ADK 2.0, Gemini 3.5, Vertex AI Agent Runtime and Memory Bank, Model
Armor, and Cloud Scheduler → Pub/Sub → Cloud Run functions. Apache 2.0.

Repo and full write-up, including the trapdoors: [REPO_URL]

#AllThingsAgenticHackathon

# Evaluation

Two layers, because they answer different questions.

## Deterministic tests (`tests/`)

These verify the ARMCL mechanism itself: tier routing, distillation, redaction,
gap analysis, guardrail verdicts, circuit-breaker bounds, and cross-session
recall. They use a fake context rather than a live model, so they are fast,
free, and cannot flake on model variance.

```bash
make test
```

`tests/test_cross_session_recall.py` is the one that matters most. It proves a
constraint recorded in one session reaches an agent's instruction in a later,
cold session, and that per-task state does *not* cross that boundary.

## Model-in-the-loop eval (`eval/`)

`cross_session_recall.evalset.json` checks that the *agents* actually act on
what ARMCL retrieves. Mechanism tests can pass while a model ignores the
context frame it was handed, and only this layer catches that.

Requires cloud credentials, the ADK evaluation extra, and spends tokens:

```bash
pip install -e ".[eval]"
gcloud auth application-default login
python -m eval.run_eval --project YOUR_GOOGLE_CLOUD_PROJECT
```

The runner stores the raw ADK output under `eval/results/` and writes
`eval/results/latest_metrics.json` with trajectory success rate, memory recall
consistency, refusal accuracy, response match, and amortized average latency.
It also corrects an ADK CLI edge case: the underlying command can exit zero
when every case failed during inference, while this wrapper exits non-zero.

### The three cases

`run_1_learns_the_constraint` — first encounter with UNIT-7. The fleet inspects
it, finds Policy 14, and escalates. The constraint is written to Tier 3.

`run_2_declines_from_memory_alone` — a cold session issuing the identical
request. The expected outcome is DECLINE, justified by a constraint this run
never observed directly. If run 2 behaves like run 1, ARMCL is not working.

`dependency_gap_resolved_without_the_operator` — asserts `inspect_item` is
called with **no arguments**. The identifier comes from memory. A trajectory
where the model supplies the identifier itself, or asks the operator for it,
fails this case.

### Reading the thresholds

`tool_trajectory_avg_score` is set to 0.6 rather than 1.0 on purpose. The
assertion worth making is that the *right tools* are called with the right
absent arguments, not that a language model produces a byte-identical
trajectory across runs. A threshold of 1.0 here would fail on harmless
variation and teach you to ignore the eval.

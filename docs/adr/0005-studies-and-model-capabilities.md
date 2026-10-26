# ADR 0005: Studies and Model Capabilities — Two New Seams Above and Below the Runner

## Status

Proposed, 2026-10-26. Complements ADR 0004 (route capabilities). Does not change
`PlantAdapter`, `ControllerAdapter`, or the Runner loop.

## Context

`docs/60` lists 35 operating conditions. Classified by *what it takes to close
them*, the picture is not "the architecture is wrong"; it is "the architecture
has three layers and only one of them exists":

| Closure class | Rows | Meaning |
| --- | --- | --- |
| `closed` | 8 | works today, tested |
| `test-only` | 6 | expressible today, no test proves it |
| `postprocess` | 2 | derived metric over one run's samples |
| `adapter` | 4 | new model or netlist template, zero kernel edits (T1) |
| `controller` | 2 | new controller / protection state machine |
| `study` | 3 | needs orchestration *above* a single run |
| `capability` | 3 | needs the model to answer a question `advance()` cannot |
| `kernel-impl` | 5 | kernel implementation change, **no protocol change** |
| `contract` | 1 | genuinely needs a contract change (already decided in ADR 0004) |
| `never` | 1 | declared out of scope |

Only **one** row needs a contract change. Everything else is either
implementation, a new adapter, or a missing layer. That is the useful measure of
whether the architecture fits: **can a scenario be added without touching
`PlantAdapter` / `ControllerAdapter` / the Runner loop?**

## Decision

Add two seams. Do not change the existing three objects.

### Seam 1 — `Study` (above the Runner)

A *study* is a plan over executions, not an execution:

- `parameter_sweep`: vary `plant_config`/`initial_state`/`input_schedule`, run
  each point, aggregate (efficiency curve, line/load regulation, worst case).
- `monte_carlo`: sample parameters from declared distributions, run, aggregate
  a distribution.
- `periodicity`: run a transient repeatedly until the trailing window is
  periodic within tolerance (brute-force PSS). This is deliberately a study
  rather than a solver: it needs no new numerical machinery, and it inherits the
  per-window constraint enforcement of each constituent run.
- AC/loop-gain sweeps are **not** a study over transient runs; they need
  Seam 2. A study plan may still reference them once the capability exists.

Rules:

1. A study composes runs that already exist. It never reaches inside the Runner.
2. Each constituent run keeps its own `evidence_level`. A study's aggregate is
   **not** automatically `comparable`: the aggregate declares which per-point
   level it may inherit and what it added (sampling, aggregation, resolution).
3. A study result is a first-class artifact with its own manifest link per
   point, so `inspect-run`/`review` still work per run.
4. A study whose constituent runs cannot be executed fails closed, like every
   other route.

### Seam 2 — optional model capabilities (below the Plant)

`advance()` answers "what happens next in time". Some questions need something
else:

| Declaration | Answers | Serves |
| --- | --- | --- |
| `small_signal_model(operating_point)` | A/B/C/D or transfer function at one operating point | AC sweep, loop gain, impedance (docs/60 C4, C5) |
| `loss_model()` | conduction / switching / magnetic loss terms | efficiency breakdown (C6) |
| `steady_state(...)` | equilibrium at a given input | already partly present as `analytical_steady_state` |

Rules:

1. All of them are **optional**. Absence is reported, never inferred. A plant
   that cannot linearize makes AC analysis *unavailable*, not `limited`.
2. They are queried by a study, not by the Runner. The Runner's behaviour is
   unchanged, so I1 (in-run enforcement) is unaffected.
3. `whole_run_engine.LinearStateSpace` is the existing shape to reuse; do not
   introduce a second state-space type.
4. A plant may implement `small_signal_model` analytically or by
   small-signal identification from transient responses; either way it must
   declare which, because the evidence differs.

### Explicitly rejected for now

- **Changing the actuator to a vector.** `ActionRequest.value` is a scalar and
  `BoundedActionPolicy` is a scalar range. Multiphase interleaving with a shared
  duty and internal phase management covers the common case with an adapter
  (docs/60 B7); per-phase current-sharing research would need a contract change
  and is `specialized`, not `common`. Not now; if it becomes required it needs
  its own ADR with a migration plan for every existing plant.
- **AC analysis inside the Runner loop.** That would put a frequency sweep in a
  time-stepped kernel, which is the definition of patch-on-patch.
- **Thermal, device physics, EMI, PCB parasitics** stay out of scope
  (`docs/43`); adapters may consume extracted data.

## Consequences

- The kernel keeps three objects; two layers are added around them. Adding a
  scenario means adding an adapter, a controller, a study, or a capability —
  never editing the protocols.
- `docs/60` gains a closure-class column, and `tests/test_coverage_matrix.py`
  enforces two rules: every row is classified exactly once, and the `contract`
  class is **budgeted** — it may not exceed three rows, and every row in it must
  cite an ADR. Growing the contract therefore requires a recorded decision, not
  a new field.
- Study-level evidence is a new vocabulary problem. It is deliberately deferred:
  until a study route exists, aggregates must be presented as a set of per-run
  results, not as a new evidence level.

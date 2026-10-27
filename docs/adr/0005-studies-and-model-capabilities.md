# ADR 0005: Studies and Model Capabilities — Two New Seams Above and Below the Runner

## Status

Seam 1 (Study) **implemented in 0.15.0**. Seam 2 (optional model capabilities)
remains Proposed; it is `docs/61` stage S8. Complements ADR 0004 (route
capabilities). Does not change `PlantAdapter`, `ControllerAdapter`, or the
Runner loop.

Implemented by `src/pe_sim/study.py` (planning rules) and
`src/pe_sim/study_runner.py` (orchestration), with `pe-sim study` as the only
entry point. Neither module imports the runtime, the CLI, or a solver: a point
is executed through an injected callable, so the same runner works with a real
`Runner` composition and with fakes in tests.

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

## Seam 1 — implementation notes (0.15.0)

These are the concrete rules Seam 1 was implemented with. They are decisions,
not options: a study that cannot express its result under them is out of scope.

1. **A point is an ordinary experiment document.** `pe-sim study` writes each
   point's document to `<output>/points/pNN/config.json` before running it, and
   the point's package is published under `<output>/runs/<study>-pNN`. The
   document — not the study report — is the reproducible input of that point.
2. **Aggregates are `per-point`, never `comparable`.**
   `aggregate.evidence_level` is the literal string `per-point`.
   `inherited_ceiling` is the weakest level any point **published**; it looks
   only at the level, never at qualification, and an unrecognised or missing
   level counts as `undeclared` and is ranked as `diagnostic_only`. Whether the
   study may be read across points is answered only by
   `cross_point_comparability`, which is `claimed` only when all of these hold:
   at least two records with unique `run_id` and unique `index`; every point
   `QUALIFIED`; every point itself `comparable`; and identical non-empty
   `run_mode`, `plant.id`, and route triple. Otherwise it is `not_claimed` with
   codes from a closed set (`empty_study`, `single_point_study`,
   `duplicate_points`, `point_not_qualified`, `points_not_all_comparable`,
   `point_route_not_declared`, `point_identity_incomplete`,
   `point_evidence_level_not_declared`, `route_identity_differs`). Every
   aggregate carries the limitation `aggregate_does_not_inherit_comparable`,
   and a parameter path may appear only once per plan: a repeated path would
   silently collapse an axis while still publishing duplicate points.
3. **Points are read, never re-derived.** Per-point `status`,
   `evidence_level`, and `manifest_sha256` come from the published
   `manifest.json`. The study layer re-derives no evidence level and recomputes
   no digest: the cap has exactly one producer (ADR 0004 rule 2).
4. **Monte Carlo determinism is a contract.** Draws consume only
   `random.Random(seed).random()`; a normal draw uses the module's own
   Box-Muller transform. The report records every draw and their canonical
   `draws_sha256`, and the runner replays the draws before starting a run, so a
   planning bug fails before any point executes.
5. **`periodicity` is a tolerance-based repetition test, not a PSS solver.**
   Each point lengthens the transient (`duration × extend_factor^k`); within one
   point the trailing `compare_period × compare_windows + 1` samples are
   compared pairwise with an **index** lag of `compare_period` (not a time lag),
   giving `compare_period × (compare_windows − 1) + 1` pairs. All rows are
   validated, not only the tail, and the result records
   `sampling_interval_s` so a reader can see the assumption. Convergence is a
   declared-tolerance statement (`pss_convergence_is_tolerance_based`), and the
   report keeps the per-point numbers even when the study ends without
   converging.
6. **Statistics are descriptive.** `mean`/`min`/`max`/`rms`/`peak_to_peak`/
   `last`/`ratio_of_means` over recorded samples are labelled
   `descriptive_statistics_only_no_loss_claim`. Power, efficiency, and loss
   decomposition require a `loss_model()` and stay in Seam 2.

## Consequences
- The kernel keeps three objects; two layers are added around them. Adding a
  scenario means adding an adapter, a controller, a study, or a capability —
  never editing the protocols.
- `docs/60` gains a closure-class column, and `tests/test_coverage_matrix.py`
  enforces two rules: every row is classified exactly once, and the `contract`
  class is **budgeted** — it may not exceed three rows, and every row in it must
  cite an ADR. Growing the contract therefore requires a recorded decision, not
  a new field.
- Study-level evidence is a new vocabulary problem, and 0.15.0 resolved it
  **without** adding a level: an aggregate reports the literal
  `per-point`, names the weakest point it inherited, and states separately
  whether it claims cross-point comparability. Until that rule existed,
  aggregates had to be presented as a set of per-run results only.

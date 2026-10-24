# Time Model and Engine Boundary v1

**Status:** Phase A2 contract delivered in 0.5.8; the **whole-run execution
route and its first engine are implemented in 0.5.10**. It records who owns
time, what a trace must promise, how a route is priced, and what the route
still cannot do.

## 1. Decision D1: dual ownership, priced by evidence

| Route | Who owns time | In-run enforcement | Evidence cap |
| --- | --- | --- | --- |
| `stepped` (default) | kernel | safety, validity, applicability checked per window | `comparable` |
| `whole_run` | engine | post-hoc trace checks only | `limited` |

**Why dual, and not one or the other.**

- A kernel that only supports stepped engines cannot accept an engine that
  returns a complete trace. That rejects real solvers for a reason unrelated
  to evidence quality.
- A kernel that accepts whole-run traces at `comparable` would quietly
  redefine what `comparable` means, because it never observes the engine while
  it runs. `docs/43` invariant I1 says in-run enforcement is the property that
  makes the strong level meaningful.
- Pricing the route resolves both: the engine is admitted, and the loss of
  assurance is recorded instead of being hidden.

**Formal comparison** (`mode: formal_comparison`) remains restricted to the
stepped route. A whole-run trace may be useful, reproducible, and inspected,
but it cannot be the basis of a formal comparison claim in this contract.

**Revisit when** a whole-run engine exposes a window or checkpoint callback
that restores in-run enforcement, or when a trace-level enforcement policy is
reviewed and priced on its own terms. Until then the cap is not "temporary
polish"; it is the contract.

## 2. Declarations

A plant declares its route as data:

- `PlantRegistration.time_model` (`stepped` by default) is validated at
  registration time.
- `pe-sim list-plants` reports it, so a client can see the route before
  starting a run.
- The CLI checks the declared adapter shape: a stepped adapter must expose the
  six-method contract; a whole-run adapter must expose `trace_contract()` and
  `run_whole()`. No engine, topology, or vendor name appears in this check.
- The Runner reads `plant.time_model`, records it in the manifest, and
  announces it in `run.started`. An unknown value is refused. Both declared
  routes are executable as of 0.5.10; a route that cannot honour a requested
  option refuses it explicitly (section 6).

## 3. Trace contract

A whole-run engine declares a `TraceContract` before it runs:

| Field | Meaning |
| --- | --- |
| `cadence_s` | maximum accepted spacing between consecutive samples |
| `coverage_tolerance_s` | slack on the zero start and duration end |
| `schema` | `pe-sim.trace-contract.v1` |

`validate_whole_run_result` checks only what can be established afterwards:

1. at least one sample and at least one signal;
2. every signal has the same length as `time_s`;
3. every value is a finite real number;
4. `time_s` is strictly increasing;
5. the trace starts at zero and ends at `duration_s` within tolerance;
6. no gap exceeds `cadence_s + coverage_tolerance_s`.

It deliberately does **not** check actions, in-run safety, or control-law
behavior. Those belong to the stepped route and are exactly what the evidence
cap represents.

## 4. Engine boundary

```python
class WholeRunPlant(Protocol):
    time_model: str                      # "whole_run"
    def trace_contract(self) -> TraceContract: ...
    def run_whole(self, request: WholeRunRequest) -> WholeRunResult: ...
```

`WholeRunRequest` carries `duration_s`, `initial_state`, an optional `seed`,
and metadata. The engine owns its internal time axis and, by implication, its
control loop: the kernel does not inject the project controller window by
window on this route. That is why the controller cannot be treated as
kernel-enforced on a whole-run trace.

`WholeRunResult` carries `time_s`, `signals`, `final_state`, and metadata. It
is the engine's claim; a caller must pass it through
`validate_whole_run_result` before using it as evidence.

## 5. Evidence pricing

The pricing is code, not prose:

| Function | Stepped | Whole run |
| --- | --- | --- |
| `evidence_cap` | `comparable` | `limited` |
| `cap_evidence_level` | identity | caps `comparable` to `limited` |
| `formal_comparison_allowed` | `True` | `False` |
| `route_limitations` | `()` | `in_run_enforcement_unavailable`, `post_hoc_validation_only` |

`manifest["time_model"]` records the route, and `_derive_evidence_level` passes
every success level through `cap_evidence_level`. A route cannot reach a level
its evidence does not support. The levels used here are the runtime manifest
vocabulary (`diagnostic_only` < `functional` < `limited` < `comparable`), not
the Stage 12 classification vocabulary (`docs/22`), which has separate axes.

## 6. The executable whole-run route (0.5.10)

`pe_sim.whole_run_route.run_whole_run_route` is the kernel side of the route.
It keeps one package layout: the manifest is assembled by the same
`_build_run_manifest` helper the stepped route uses, so a client sees the same
keys plus `time_model`, `route_limitations`, and `enforcement`.

**Refused before any filesystem change.**

| Request | Result |
| --- | --- |
| `mode: formal_comparison` | `ValueError` in the Runner; the CLI emits `formal_comparison_rejected` and exits 2 |
| `checkpoint_interval_steps` / `resume_from` / `interrupt_after_steps` / `cancel_after_steps` | `ValueError`: the kernel owns no steps to checkpoint, resume, or interrupt |
| `applicability: enforce` | `ValueError`: there are no kernel-owned windows to enforce at |
| unknown `time_model`, missing `trace_contract`/`run_whole`, wrong contract type | `ValueError` |

**What the route does.**

1. Builds `WholeRunRequest(duration_s, initial_state, seed, metadata)` from the
   experiment document; `metadata` carries documented execution facts only
   (`input_schedule`, `control_period_s`, `run_id`, `experiment_id`).
2. Calls `trace_contract()` once and `run_whole(request)` once.
3. Validates the returned trace with `validate_whole_run_result`. Problems are
   not a crash: the package is published as `RUN_FAILED` with
   `failure.category = trace_contract_violation` and the validator problems
   verbatim in `logs/run.json` and in an events entry.
4. Converts the trace to the normal sample rows (`step_index`, `time_s`, one
   column per signal) and runs the same plugin chains as a stepped run, with
   the check context marked `post_hoc: true`.
5. Prices the result through `_derive_evidence_level(..., time_model=...)`.

**Cancellation.** A `CancelToken` is polled before the engine call and once
after it. Cancelling before publishes an `INCOMPLETE` package with no samples
and `failure.cancellation_observed = "before_engine_call"`. A cancellation
observed after the engine returned still publishes `INCOMPLETE`: the client
asked to stop, so the package records the trace length it had reached rather
than pretending the request arrived earlier.

**Evidence level in practice.** A whole-run run is always `exploratory`
because formal comparison is refused, and an exploratory success records
`functional`. That is *below* `limited` on the runtime ladder
(`diagnostic_only` < `functional` < `limited` < `comparable`), so the
structural `limited` cap is never the binding constraint for this route; it
exists to stop a future change from promoting a whole-run run past `limited`.

**Still not delivered:** trace resampling, checkpoint/resume for whole-run
runs, in-run enforcement callbacks, cross-version comparability for a
whole-run trace, and any reviewed identity pin for the first engine (it
remains `not_pinned`).


## 7. Related documents

| Document | Relationship |
| --- | --- |
| `docs/43_kernel_roadmap_v1.md` | Phase A2, D1, evidence pricing table |
| `docs/42_kernel_interface_topology_v1.md` | what the kernel may know about an adapter |
| `docs/44_run_interaction_contract_v1.md` | run events and terminal result |
| `docs/45_error_and_rejection_codes_v1.md` | how a refused route is reported |

# Time Model and Engine Boundary v1

**Status:** Phase A2 contract, delivered in 0.5.8. It records who owns time,
what a trace must promise, and how a route is priced. The whole-run execution
route is **declared but not implemented**: the Runner refuses `whole_run`
before touching the filesystem rather than silently mis-running it.

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
  announces it in `run.started`. An unknown value is refused. `whole_run` is
  refused in this release with an explicit "route is not implemented" error,
  before any run directory or temporary package exists.

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

## 6. Current status and non-goals

**Delivered (0.5.8):** D1, the declaration, the trace contract, the engine
protocol, the post-hoc validator, the pricing functions, catalog/CLI/manifest
visibility, and tests.

**Not delivered:** an executable whole-run route, a second engine, trace
resampling, checkpoint/resume for whole-run runs, and any comparability claim
for a whole-run trace. Declaring `whole_run` is refused until the route lands.

**Non-goals:** choosing a resampling policy, performance work, or allowing a
whole-run trace to bypass the evidence cap.

## 7. Related documents

| Document | Relationship |
| --- | --- |
| `docs/43_kernel_roadmap_v1.md` | Phase A2, D1, evidence pricing table |
| `docs/42_kernel_interface_topology_v1.md` | what the kernel may know about an adapter |
| `docs/44_run_interaction_contract_v1.md` | run events and terminal result |
| `docs/45_error_and_rejection_codes_v1.md` | how a refused route is reported |

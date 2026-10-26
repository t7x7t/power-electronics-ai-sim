# User-Discovery Backlog (v1)

**Status:** backlog only. This document records follow-ups found by an
independent new-user test on 2026-10-22 and updated after the 2026-10-23
kernel review (`docs/38_kernel_review_v0.md`). It does not change the release
support matrix, and none of the items below are implicit release claims.

## Fixed immediately

| ID | Finding | Resolution |
| --- | --- | --- |
| UD-F1 | `backend list` / `setup` guidance printed `pe-sim backend setup ngspice ...`, but the CLI requires `--name ngspice` | Guidance in `registry.py` and the CLI `next_steps` now uses `--name ngspice`; regression tests assert it |
| UD-F2 | Installing `PySpice==1.5` selected NumPy 2.x/current SciPy/ContourPy; restoring NumPy 1.26.4 left `pip check` broken | `requirements-pyspice-tested.txt` pins a compatible set (`numpy 1.26.4`, `scipy 1.13.1`, `matplotlib 3.9.4`, `contourpy 1.3.1`, PySpice 1.5); user acceptance runs `pip check` and records the result |
| UD-F3 | `--backend` defaulted to `fake` while `plant_id` was ignored; a config declaring `ngspice-buck` could publish a FakePlant run | CLI now maps `plant_id` to a reviewed backend, rejects unknown/mismatched combinations before running, validates the adapter class, and prints the selected backend/plant class |
| UD-F4 | The class named `FakePIController` implemented only `kp`; `ki` was silently ignored | Added a real integral state, output limits, integral-clamping anti-windup, checkpoint/restore, and strict `controller_config` validation. Unknown `input_schedule` keys are also rejected |
| UD-F5 | The PSFB example and `psfb-step` CLI were fake-backend fixtures whose names implied a PSFB model | Removed from the release tree; PSFB remains a future reviewed topology, not a shipped example or CLI command |

## Functional follow-ups

### UD-01: CLI controller capability and compatibility

**Finding:** the CLI now supports a true PI (`kp`, `ki`, limits, integral
clamping), but it still lacks a controller registry, PID (derivative) action,
feedforward/load compensation, and custom controller selection. Custom
controllers require the Python API.

**Required work:**

- define a controller registry keyed by `controller_id` (`pi`, `pid`, and the
  existing fixture controller), with explicit configuration schemas;
- add derivative state/filtering and feedforward/load compensation without
  hiding the control law;
- preserve backward compatibility for existing `controller_id: "pi"` and
  `kp`/`ki` configurations;
- document that controller identity and parameters are recorded in the
  manifest;
- add CLI, contract, checkpoint/restore, and end-to-end acceptance tests.

**Acceptance:** a user can select and configure PI/PID/feedforward from JSON,
the run publishes a `QUALIFIED` package with the controller identity, and
existing configurations keep their behavior.

**Implemented in 0.5.3 (registry, derivative, feedforward):**

- `controller_id` is resolved through a binding table (`pi`, `pid`) instead of
  a hardcoded single id. Each binding declares the configuration keys it
  accepts, so a copied PID configuration under `pi` is rejected rather than
  silently ignored.
- The control law is explicit and unit-bearing:

  ```text
  u = kp*e + sum(ki*e*dt) + kd*de/dt + kff_v*vref
  ```

  | Gain | Unit |
  | --- | --- |
  | `kp` | duty per volt |
  | `ki` | duty per (volt * second) |
  | `kd` | duty per (volt / second) |
  | `kff_v` | duty per volt of reference |

- The derivative acts on the measured error and is optionally low-pass filtered
  with `kd_filter_tau_s`, using `alpha = dt / (tau + dt)`.
- A zero gain disables its term **and** removes it from the recorded identity,
  so a `pi` configuration stays numerically and identity-compatible with 0.5.2.
- Derivative state (`previous_error`, `filtered_derivative`) participates in
  `snapshot()`/`restore()`, so checkpoint and resume stay deterministic.
- Both copies of `experiment.schema.json` (repository and packaged) accept the
  new keys and reject unknown `controller_id` values.

**Still open:** load-compensation feedforward needs a load estimate the kernel
cannot observe, so `kff_v` is reference feedforward only. Bumpless transfer and
industrial anti-windup variants are out of scope for a reference controller.
Custom controller *classes* still require the Python API; adapter discovery is
tracked by UD-04.

**Implemented in 0.14.0 (registry and compensators):** `controller_id` is no
longer a schema enum. It is resolved through `pe_sim.controller_registry`, and
`pi`, `pid`, `type2`, `type3`, and `biquad` are registered built-ins. Type II and
Type III are Tustin-mapped at the control period and record their discrete
coefficients in the controller identity; the runtime applies one explicit
difference equation (ADR 0007). A configuration copied from another controller
still fails, because the registration declares the keys it accepts.

### UD-02: topology coverage

**Finding:** the reviewed real-solver path covers only the switching Buck;
Boost exists only as an averaged model, and PSFB/LLC are not implemented.

**Required work:**

- add a reviewed switching Boost netlist, Plant adapter, provenance, topology
  descriptor, Stage 10/11 admission, and real-ngspice acceptance;
- treat each additional topology (PSFB, LLC, ...) as its own reviewed model
  slice with a documented claim boundary;
- do not infer support from examples or from an averaged fixture.

**Acceptance:** each topology has its own scope document, netlist identity,
real-solver acceptance, and explicit non-claims.

### UD-03: waveform resolution and analysis export

**Finding:** run artifacts record only control-window boundary samples; the
switching ripple requires the adapter's internal `_last_result.table` or a
manual netlist/process call.

**Required work:**

- add an explicit, reviewed accessor/export for the solver window waveform
  without exposing mutable solver internals;
- define sampling/decimation policy and artifact-size limits;
- add a derived analysis artifact for ripple/loss workflows, clearly outside
  the immutable raw run package;
- document that boundary samples remain the control-loop truth and that raw
  waveforms are solver evidence, not automatically published contracts.

**Acceptance:** a user can obtain a documented switching waveform with
provenance, while the standard run package remains bounded and immutable.

### UD-04: engine-neutral adapter protocol and plugin trust levels

**Finding:** the process boundary and provenance are appropriate, but the code
is still shaped around one engine. `NetlistBuilder` is typed to Buck
parameters, `NgspiceBuckPlant` combines topology and engine invocation, and the
CLI uses hardcoded allow-lists. `docs/33` describes entry points but they are
not implemented.

**Required work:**

- define a minimal `EngineAdapter` protocol (`probe`, `capabilities`, `solve`)
  and a generic `SimulationRequest`/`SimulationResult` pair;
- parameterize the model builder by a reviewed topology IR rather than by
  `BuckNetlistParameters`;
- separate window semantics from engine invocation;
- implement `importlib.metadata` entry-point discovery with three trust levels:
  `reference`, `third-party`, and `fixture`;
- forbid third-party adapters from being classified as reference evidence;
- add a `pe-sim backend verify-plugin` conformance entry point.

**Acceptance:** a second engine and a second topology can be added without
editing the Runner, the artifact rules, or the CLI allow-list; the reference
Buck behavior stays pinned.

**Prerequisite status (0.5.1):** the tolerance half of comparison is closed
(`ToleranceSpec`, docs/41 §4) and the public surface is now tiered, so adapter
authors have a declared contract to program against. Sample grid alignment is
still positional; a second engine with a different time grid will not compare
against the ngspice windows until a resampling policy is reviewed. Treat that
as the first task of UD-04, not as a leftover of the tolerance work.

**Implemented in 0.5.4 (registration, discovery, trust):** `plant_registry`
turns the plant catalog into data, `pe_sim.plants` entry points let a
distribution ship its own plant, and every registration carries a trust level
that is forced to `third-party` for entry points. The kernel no longer
enumerates plants in the CLI, the experiment schema, the identity pins, or the
post-processing allow-list. `EngineAdapter`, engine capability negotiation, and
the time-grid policy are unchanged and still belong to A2 / D1.

### UD-05: phase-continuous PWM for arbitrary control ratios

**Finding:** the window netlist restarts the PWM carrier at `t=0`. The adapter
now rejects control periods that are not an integer multiple of the switching
period, because the alternative is a silent effective-duty change.

**Required work:** carry the carrier phase across window boundaries (for
example a `PWM phase` parameter and an explicit wrap-capable waveform), or
split the solve at carrier boundaries. Update the netlist template version and
all reviewed identities with a model-review record.

**Acceptance:** a non-integer control/switching ratio produces the same
carrier phase and ripple as a continuously running solver within a declared
tolerance.

### UD-06: agent and release attestation

**Finding:** `QUALIFIED` and the manifest are automated local evidence. There
is no actor identity, signed release, or package signature. The review pin in
`reviewed_identities.json` makes code changes visible but can itself be edited.

**Implemented in 0.5.0 (Level 1):** a detached attestation records the
manifest/package/artifact-index identity outside the package, and an
append-only `attestations/anchor-index.jsonl` is verified against a reachable
Git commit. `verify-attestation` reports `L1_anchored` only when the record and
the attestation are both committed. The payload/signatures split keeps Level 2
signatures append-only. See `docs/40_validity_and_authenticity_v1.md`.

**Remaining work:** signed tags or package signatures (L2), optional
actor/agent recording, and an SBOM/source inventory gate. Do not describe
self-reported actor fields as a security control, and do not describe L1 as
protection against a local administrator.

**Acceptance:** a reviewer can distinguish a locally built exploratory package
from a release package whose source and build identity were signed.

### UD-07: per-window netlist persistence

**Finding:** `plant.evidence` records the first/last window netlist digests and
solver invocation count, but not the netlist text. `preview-netlist` reproduces
the template from the configuration.

**Required work:** optionally retain a bounded set of exact window netlists in
a derived inspection directory, not in the immutable run package, or provide a
documented deterministic reconstruction command using the recorded state and
action sequence.

**Acceptance:** a reviewer can reproduce the exact netlist for any recorded
window without relying on private adapter internals.

## Priority

**This ordering is user impact, not a work sequence.** Where and when these
items consume kernel attention is decided by `docs/43_kernel_roadmap_v1.md`.
In particular, items below that add control algorithms, topologies, or models
are adapter or product content and are outside the kernel roadmap's scope.

1. UD-01 (largest user-facing control gap).
2. UD-04 (required before claiming multi-engine support).
3. UD-03 (needed for switching-level analysis).
4. UD-02 (new topology scope, one reviewed slice at a time).
5. UD-05 and UD-07 (model and verification refinements).
6. UD-06 (release attestation, when distribution justifies it).

UD-F1 and UD-F2 are fixed and covered by tests. The 2026-10-23 kernel review
fixed the implementation-pin, package-inspection, and failure-log gaps; its
residual findings are recorded above.

# User-Discovery Backlog (v1)

**Status:** backlog only. This document records follow-ups found by an
independent new-user test on 2026-10-22. It does not change the release support
matrix, and none of the items below are implicit release claims.

## Fixed immediately

| ID | Finding | Resolution |
| --- | --- | --- |
| UD-F1 | `backend list` / `setup` guidance printed `pe-sim backend setup ngspice ...`, but the CLI requires `--name ngspice` | Guidance in `registry.py` and the CLI `next_steps` now uses `--name ngspice`; regression tests assert it |
| UD-F2 | Installing `PySpice==1.5` selected NumPy 2.x/current SciPy/ContourPy; restoring NumPy 1.26.4 left `pip check` broken | `requirements-pyspice-tested.txt` pins a compatible set (`numpy 1.26.4`, `scipy 1.13.1`, `matplotlib 3.9.4`, `contourpy 1.3.1`, PySpice 1.5); user acceptance runs `pip check` and records the result |

## Functional follow-ups

### UD-01: CLI controller capability and compatibility

**Finding:** `pe-sim run` always constructs `FakePIController` and exposes only
`kp`, so it is proportional control, not a configurable PI/PID or feedforward
workflow. Custom controllers require the Python API.

**Required work:**

- define a controller registry keyed by `controller_id` (at minimum `pi`, `pid`,
  and the existing fixture controller), with explicit configuration schemas;
- add integral/derivative state, output limits, anti-windup, derivative filter,
  and measurement-key handling;
- support optional feedforward/load compensation without hiding the control law;
- preserve backward compatibility for existing `controller_id: "pi"` and
  `kp`-only configurations;
- document that controller identity and parameters are recorded in the
  manifest;
- add CLI, contract, checkpoint/restore, and end-to-end acceptance tests.

**Acceptance:** a user can select and configure PI/PID/feedforward from JSON,
the run publishes a `QUALIFIED` package with the controller identity, and
existing configurations keep their behavior.

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

## Priority

1. UD-01 (largest user-facing control gap).
2. UD-03 (needed for switching-level analysis).
3. UD-02 (new topology scope, one reviewed slice at a time).

UD-F1 and UD-F2 are fixed and covered by tests.

# ngspice Backend Scope and Acceptance (v1)

**Status:** real-backend slice definition. It narrows the project claim to one
reviewed switching-level reference circuit and the evidence the adapter
actually records. It is not a physical, thermal, hardware, or product claim.

## Purpose

This document defines the first external-simulator surface admitted to the
repository: the asynchronous Buck reference is constructed through the PySpice
object API and executed by a process-boundary ngspice adapter. It complements,
and does not replace, the release scope matrix in
`docs/27_minimum_local_release_scope_v0.md` and the toolchain policy in
`docs/28_release_toolchain_policy_v0.md`.

ADR `docs/adr/0002-ngspice-process-boundary.md` records why **execution** stays
on a process boundary instead of PySpice's native solver linkage. PySpice is
used only as the reference circuit-construction frontend.

## Supported surface

| Area | Level | Scope | Boundary |
| --- | --- | --- | --- |
| `NgspiceBuckPlant` | Supported locally | Ideal switching asynchronous Buck with a voltage-controlled switch, freewheeling diode, inductor, capacitor, and resistive load, solved by a separately installed ngspice executable. | One reviewed circuit; ideal elements only. No device, magnetic, thermal, or loss model. |
| PySpice construction (reference path) | Verified local reference (opt-in) | `--model-builder pyspice` builds the reference Buck through the PySpice object API and executes the generated netlist with the reviewed ngspice adapter. | Requires a separate PySpice 1.5 installation. PySpice native execution is not supported against ngspice 41; see `docs/34`. |
| Native netlist generation (fallback) | Deterministic test/CI path | Fixed template `ngspice-buck-v1`, fixed numeric formatting, SHA-256 identity. | Available for offline tests and CI; it is not the reference construction path. No user-authored netlist, no arbitrary SPICE pass-through. |
| Window execution | Deterministic | One zero-order-hold control window per ngspice invocation; segment endpoints are linearly interpolated inside the solved window. | Linear interpolation of a solved waveform is not a substitute for a solver step; see the timing note below. |
| Provenance | Supported | Executable name, SHA-256, parsed version, solver settings, netlist/output/log digests, exit code, duration. | No claim of cross-machine numerical equivalence. |
| Stage 10 metrics | Restricted | The published ngspice package may be read by the restricted metric registry. | Only the reference metric set and declared signal names, and only after the same clean-worktree, `PUBLISHED`, hash-complete, and `QUALIFIED` gates as every other family. |
| Stage 11 visualization | Restricted | The switching Buck descriptor and the same `vout`/`inductor_current` trace names are exported through the headless visualization contract exporter. | Display only; no topology editing or solver control; the browser workbench is temporarily withdrawn. |

## Switching Buck reference definition

The reference netlist is constructed through the PySpice object API and
contains exactly these elements:

- `V1`: ideal DC input source;
- `Vpwm`: ideal pulse source with period `Tsw` and width `D * Tsw`;
- `S1`: voltage-controlled switch with explicit `Ron`/`Roff` and threshold
  `Vt = 0.5 V`;
- `D1`: Shockley diode with explicit `Is` and emission coefficient `N`;
- `L1`, `C1`: ideal inductor and capacitor with initial conditions;
- `R1`: ideal resistive load.

The model is deliberately ideal. It does not include switch capacitance, diode
reverse recovery, inductor resistance or saturation, capacitor ESR/ESL,
dead time, gate drive, or thermal behavior.

## Solver settings

Solver settings are explicit and travel with the evidence:

| Setting | Default | Notes |
| --- | --- | --- |
| `integration_method` | `gear` | `gear`, `trap`, or `euler`; recorded verbatim. |
| `reltol` | `1e-3` | Relative tolerance. |
| `abstol` | `1e-9` | Current tolerance. |
| `vntol` | `1e-6` | Voltage tolerance. |
| `chgtol` | `1e-13` | Charge tolerance. |
| `gmin` | `1e-12` | Minimum conductance. |
| `max_step_s` | `None` | Optional internal step bound. |
| `temperature_c` | `27` | Recorded as `.temp`. |
| `num_digits` | `15` | `wrdata` output precision. |

The rendered `.options` line, `.temp` line, and `.tran` line are part of the
netlist digest. Changing a setting therefore changes the plant identity.

## Window semantics

The Runner advances the Plant in `plant_step_s` segments and requires the final
observation of each control window to land exactly on the requested endpoint.

The adapter therefore solves `max(duration, control_period_s)` seconds at the
first segment of a control window, caches the waveform, and linearly
interpolates the remaining segment endpoints. The cache key is
`(window_start, duty, input_voltage, load_resistance)`. A changed duty or
external input invalidates the cache and starts a new solve from the current
state.

The generated window restarts the PWM carrier at its own `t = 0`. The adapter
therefore requires `control_period_s` to be an integer multiple of the
switching period. A non-integer ratio is rejected before the run rather than
silently changing the effective duty and ripple at each window boundary. A
phase-continuous implementation for arbitrary ratios is future work
(`docs/37` UD-05).

Consequences that reviewers must keep in mind:

- the internal waveform is sampled at `duration/1000` or `Tsw/200`, whichever
  is smaller, unless `sample_step_s` is supplied;
- interpolation is linear and can under-resolve a switching edge if the sample
  step is coarse; the default step is chosen to keep at least 200 samples per
  switching period;
- the adapter never asks ngspice to persist internal solver state. `restore()`
  re-seeds `IL`/`VC` and re-solves the next window. A resumed run is
  **state-equivalent within tolerance**, not bit-identical. Resume evidence
  must use a declared numerical tolerance.

## Failure semantics

| Condition | Result |
| --- | --- |
| executable missing or not queryable | Configuration rejection before the run, or `RUN_FAILED` with `backend_process_failure` if it disappears mid-run. |
| timeout | `RUN_FAILED`, category `backend_timeout`. |
| non-zero exit without solver failure | `RUN_FAILED`, category `backend_process_failure`. |
| `no convergence`, `timestep too small`, `gmin/source stepping failed`, iteration limit | `RUN_FAILED`, category `numerical_nonconvergence`. |
| output file missing, truncated, ragged, non-monotonic, or non-finite | `RUN_FAILED`, category `backend_process_failure`. |
| warning-only log with a complete parseable trajectory | accepted; the log digest is retained. |

A failed window never becomes a successful run, and no partial trajectory is
published as `QUALIFIED`.

## Evidence and provenance

A run records at least:

- `environment.backends[0]`: `name = ngspice`, parsed `version`,
  `executable_name`, `executable_sha256`, `executable_size_bytes`,
  `solver_settings`, `limitations`;
- `plant.identity`: model kind, level, parameters, netlist template version,
  solver settings, implementation source digests, and backend status/version;
- each window's netlist digest, output digest, log digest, exit code, and
  duration (available through the adapter's last result and the audit stream);
- `plant.evidence.first_window` and `plant.evidence.last_window` with the
  corresponding digests, plus `solver_invocations`;
- `failure.backend_evidence` with a bounded solver log excerpt when a window
  fails.

## Human verification

```powershell
pe-sim preview-netlist examples/ngspice_buck_step/config.json --duty 0.5
pe-sim inspect-run runs/ngspice-buck-reference
```

`preview-netlist` renders the same builder output without starting ngspice.
`inspect-run` verifies package integrity and reports
`plant.implementation.review.status`. The reviewed solve-path pin is
`src/pe_sim/reviewed_identities.json`; update it only with an explicit
model-review record.

Local absolute executable paths are intentionally omitted from portable
evidence.

## Acceptance matrix

| ID | Area | Check | Status |
| --- | --- | --- | --- |
| N14-01 | Contract | `check_plant_adapter` passes for `NgspiceBuckPlant`. | Implemented |
| N14-02 | Determinism | Netlist rendering and digest are stable across runs. | Implemented (unit) |
| N14-03 | Parsing | Malformed, ragged, non-monotonic, and non-finite output is rejected. | Implemented (unit) |
| N14-04 | Process failures | Timeout, non-zero exit, missing output, and convergence messages are classified. | Implemented (fake executable) |
| N14-05 | Window cache | Ten segments in one control window produce one solver call. | Implemented (injected solver) |
| N14-06 | Provenance | Executable version, digest, and solver settings are recorded as `known` when available. | Implemented (fake executable); real check opt-in |
| N14-07 | Equilibrium | Constant duty reaches `D * Vin` within the declared tolerance. | Opt-in, `PE_SIM_NGSPICE`; `vout` within 5% relative and inductor current within 40% relative (ripple included). |
| N14-08 | Repeatability | Identical inputs produce identical samples on one machine. | Opt-in, `PE_SIM_NGSPICE`; exact tuple equality on the first five windows. |
| N14-09 | CLI | `pe-sim run ... --backend ngspice-buck` publishes a `QUALIFIED` package with a known backend. | Opt-in, `PE_SIM_NGSPICE`; the same path is exercised in CI with a fake executable. |
| N14-10 | Recovery | Interrupted run resumes to a tolerance-equal trajectory. | Not yet automated |

## Explicit exclusions

This slice does not claim or verify:

- physical calibration, device models, parasitics, losses, magnetics, thermal
  behavior, or component selection;
- PSFB, LLC, or any topology other than the switching Buck reference;
- cross-machine numerical equivalence or an exact-hash repeatability claim for
  the external solver;
- solver convergence for user-authored circuits;
- hardware safety, compliance, product readiness, or maintainer approval;
- PySpice interoperability (an optional future adapter, not part of this
  release).

## Running the opt-in acceptance

```powershell
$env:PE_SIM_NGSPICE = "D:\tools\ngspice-44.2\Spice64\bin\ngspice_con.exe"
pe-sim backend doctor
.\\.venv\Scripts\python.exe -m pytest -q tests/test_ngspice_real_backend.py
.\\.venv\Scripts\pe-sim.exe run examples/ngspice_buck_step/config.json --backend ngspice-buck
```

On Windows, prefer the console build (`ngspice_con.exe`). Discovery replaces a
sibling GUI `ngspice.exe` automatically and records the substitution.

# Kernel Review and Browser-Surface Withdrawal (v0)

**Status:** engineering review record, 2026-10-23. It records verified findings,
the changes made in response, and the remaining gaps. It is not a physical,
thermal, hardware, or product claim.

## 1. Decision: withdraw the browser surface

The TypeScript/Vite reference workbench rendered unusable and visibly broken
content. It was removed from the shipped tree:

- `workbench/` (Vite application, fixtures, build configuration);
- `tests/test_workbench_browser_e2e.py` and the browser-only release gate;
- `tests/test_external_visualization_consumer.py`, which depended on a
  workbench fixture;
- `docs/30_stage11_browser_e2e_v0.md`;
- Node/npm and Playwright from `docs/28`, `docs/29`, CI, the release gate, and
  `requirements-release.txt`.

The following headless surfaces remain and are still tested:

- the three renderer-neutral contracts (`TopologyDescriptor`, `TraceDataset`,
  `OutputDataset`);
- `pe-sim export-workbench` (the name is retained for compatibility; it is an
  offline data exporter, not a UI);
- `pe-sim visualization-serve` (loopback read-only JSON service);
- `examples/visualization_consumer/` (standard-library consumer).

A future UI must consume only these contracts and must not receive Runner,
Plant, Controller, or mutation authority.

## 2. Containment of agent-induced changes

The project already had these controls:

| Control | Where | What it proves |
| --- | --- | --- |
| Dirty/unknown worktree rejection | `pe_sim.dirty`, `--mode formal_comparison` | A formal run cannot come from uncommitted changes. |
| Reviewed-implementation formal gate | `runtime._require_formal_evidence`, `reviewed_identities.json` | A formal run is rejected when the plant's pinned solve path has `implementation.review.status == "unreviewed"`. |
| Manifest provenance | `manifest.json` | Source commit, branch, worktree status, worktree-analysis digest, environment, backend provenance, and component hashes are recorded. |
| Contract hash pins | `tests/test_default_contracts.py` | The safety and qualification contracts cannot change silently. |
| Per-run qualification | `qualification.json`, `QUALIFIED` | Only the configured automated checks passed for that run; it is not physical validation. |
| Audit stream | `audit.json` | Adapter call order, counts, and failures are recorded. |

The review found two concrete gaps and fixed them:

1. **Incomplete implementation identity.** `NgspiceBuckPlant.manifest_identity()`
   hashed only `ngspice_plants.py` and `netlist.py`. The PySpice construction
   path (`netlist_builder.py`), process boundary (`process.py`), solver settings
   (`settings.py`), and waveform parser (`wrdata.py`) were not covered. An agent
   could change circuit construction or result parsing without changing the
   recorded implementation hash.
   **Fix:** `pe_sim.backends.identity` hashes the six solve-path modules and
   exposes `implementation.sha256` plus a `review` record. The checked-in
   `src/pe_sim/reviewed_identities.json` pins the reviewed value. A test fails
   when the code changes without updating the pin.
2. **No quick independent verification.** A user or reviewer had to recompute
   hashes manually, and a failed `ngspice` solve deleted its log with the
   temporary directory.
   **Fix:** `pe-sim inspect-run` re-verifies the manifest digest, package
   digest, artifact index, publication marker, sample count, time monotonicity,
   and non-finite values, and reports the implementation review status.
   `NgspiceProcess` now attaches a bounded log excerpt to structured failures;
   the Runner records it in `failure.backend_evidence` and
   `plant.evidence.last_failure`.

Residual risks that are deliberately not hidden:

- the review pin is a tripwire, not a signature; a write-capable process can
  edit both the code and the pin, so Git review is still required;
- the Runner, artifact writer, and qualification code are covered by Git
  provenance, not by the solve-path pin;
- there is no signed release, SBOM signature, or attested actor identity;
- `QUALIFIED` remains an automated per-run state, not a human approval.

These are recorded as UD-06 in `docs/37_user_discovery_backlog_v1.md`.

## 3. External-simulator interface

### Appropriate and retained

- **Process boundary only.** `NgspiceProcess` is the only module that starts an
  external simulator. No GPL library is linked, no binary is bundled, and a
  missing executable is a configuration rejection rather than a silent
  fallback.
- **Deterministic construction.** The netlist is generated from a fixed
  template with fixed numeric formatting; timestamps, host names, and absolute
  paths are absent, so the SHA-256 is a stable circuit identity.
- **Explicit numerical settings.** Solver options, temperature, and output
  precision are recorded and rendered into the netlist digest.
- **Structured failure semantics.** Timeout, non-zero exit, missing output,
  unparsable output, and convergence messages map to explicit categories.
- **Discovery and provenance.** Explicit path, environment variable, user
  configuration, and `PATH` are tried in a documented order; the executable
  name, SHA-256, size, parsed version, and limitations are recorded without
  publishing machine-local paths.

### Not yet engine-neutral

The layering in `docs/33` is sound, but the code is still shaped around one
backend:

- there is no `EngineAdapter` protocol or generic `SimulationResult`;
  `NgspiceProcess` and `NgspiceRunResult` are concrete;
- `NetlistBuilder.render()` is typed to `BuckNetlistParameters`, so it cannot
  describe another topology or analysis generically;
- `NgspiceBuckPlant` combines topology, window semantics, and engine invocation;
- the CLI uses the hardcoded `_PLANT_BACKENDS`/`_BACKEND_CLASSES` allow-lists,
  and the entry-point mechanism described in `docs/33` is not implemented;
- the CLI's `_build_ngspice_buck` uses the ngspice-specific resolver even though
  a generic `resolve_backend_executable` exists.

**Recommendation (UD-04):** introduce a minimal engine-neutral protocol
(`probe`, `capabilities`, `solve`), a topology-parameterized model-builder
contract, and entry-point discovery with three trust levels (`reference`,
`third-party`, `fixture`). Do this before claiming multi-solver support. Do not
refactor the accepted Buck slice while doing it; keep its behavior pinned.

### Correctness fix: phase continuity

Each window netlist restarts the PWM pulse at its own `t=0`. That is only
phase-continuous when the control window is an integer number of switching
periods. A non-integer ratio silently changed the effective duty and ripple at
every window boundary. The adapter now rejects such configurations with a clear
error. A phase-continuous implementation for arbitrary ratios remains UD-05.

## 4. User verification

New read-only commands:

```powershell
pe-sim preview-netlist examples/ngspice_buck_step/config.json --duty 0.5
pe-sim inspect-run runs/ngspice-buck-reference
```

`preview-netlist` renders the same builder output as a real window (same
parameters, solver settings, and initial state) without starting a simulator.
`inspect-run` reports package integrity separately from run outcome:

- `status = ok` means the package hashes and artifact index match;
- `run_status`/`run_ok` report whether the recorded execution succeeded;
- `plant.implementation.review.status` is `reviewed`, `unreviewed`, or
  `not_pinned`;
- `plant.evidence` contains the first/last window digests and solver invocation
  count;
- `failure.backend_evidence` contains a bounded solver log excerpt when the
  backend failed.

Remaining verification gaps:

- switching-resolution waveform access remains UD-03;
- the exact per-window netlist text is not stored in the run package, only its
  digest; `preview-netlist` reproduces the template from the configuration;
- there is no HTML/PDF report generator; the JSON summary is the supported
  interface.

## 5. Findings

| ID | Severity | Finding | Status |
| --- | --- | --- | --- |
| K-01 | High | Implementation identity omitted the PySpice builder, process boundary, solver settings, and parser. | Fixed |
| K-02 | High | No independent package verification command; users had to recompute hashes. | Fixed (`inspect-run`) |
| K-03 | High | Backend failure logs were deleted with the temporary directory. | Fixed (bounded excerpt) |
| K-04 | High | Non-integer control/switching period ratio silently changed PWM phase. | Fixed (fail closed); continuous implementation is UD-05 |
| K-05 | Medium | Browser workbench rendered unusable content. | Removed |
| K-06 | Medium | Simulator interface is ngspice-specific despite the multi-engine documentation. | Backlog UD-04 |
| K-07 | Medium | No actor identity or signed release evidence. | Backlog UD-06 |
| K-08 | Low | Exact per-window netlist text is not persisted. | Backlog UD-07 |

## 6. Verification

- Full local suite: **323 passed, 7 skipped** (Python 3.13 project `.venv`).
- New regression tests: reviewed-implementation pin, formal-mode unreviewed
  rejection, control-period ratio, netlist preview, run inspection, tamper
  detection, failure-log evidence, and packaged review-pin availability.
- `compileall` and `git diff --check` are run before publication.

This record is local engineering evidence only. It does not establish physical
model validity, hardware safety, or product readiness.

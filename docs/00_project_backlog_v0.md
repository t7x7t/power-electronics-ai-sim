# Project Backlog and Stage Map (v0)

This document is the hand-off checklist for the complete project. It records
what is implemented, what is only specified, and what must be reviewed before
the project can claim the L0 goal. A passing test is evidence for the tested
behavior only; it is not a hardware or product approval.

## Status vocabulary

- **Done**: implemented and covered by a local verification result.
- **Partial**: a useful slice exists, but an important capability or review is
  still missing.
- **Planned**: described or intended, but not implemented.
- **Blocked by review**: implementation must wait for a human decision.

## Sixteen-stage map

| # | Stage | Status | Current evidence and remaining work |
|---|---|---|---|
| 1 | Requirements, scope, and responsibility boundaries | Partial | L0/L1 documents define the infrastructure boundary, user responsibility, and Agent limits. A named requirement owner and final approver still need to be recorded. |
| 2 | Project and environment configuration | Partial | `pyproject.toml`, MIT license, Git baseline, tested-version record, and light CI exist. Environment locking, platform policy, and release evidence are incomplete. |
| 3 | Public data and interface contracts | Done (v1 incremental) | v0 contracts are extended with normalized capability negotiation, explicit event/multi-rate gates, schema compatibility checks, observation visibility/time-window checks, snapshot hash verification, and regression tests. A reusable Plant/Controller conformance harness is now available. Full event scheduling semantics and richer unit registries remain future work. |
| 4 | Plant and Controller integration | Partial (L1 references) | Self-owned ideal averaged Buck and Boost Plants now implement the public lifecycle, units, external-input updates, snapshots, and fail-closed bounds. Fake Plant, Fake Load Plant, and Fake PI Controller remain compatibility fixtures. Real `D:\PySpice` code and PSFB/LLC migration are intentionally deferred pending review. |
| 5 | Generic Runner | Partial (modular audited lifecycle v1) | Low-friction `run_experiment` facade, composable policies, explicit CREATED/RUNNING/terminal state transitions, call-order audit, checkpoint/resume, interruption as INCOMPLETE, structured capability failures, policy-driven action handling, configured primary measurements, and segmented plant timing are implemented and tested. Process-level recovery, full event scheduling, controller state persistence conventions, and richer timing semantics remain future work. |
| 6 | Safety and qualification checks | Done (v1 extensible runtime checks) | `SafetyPlugin`, `ValidityPlugin`, and `QualificationPlugin` are composable through `RunOptions`; structured findings cover observation integrity (finite values, timestamps, age, required fields, units, ranges), qualification, and fail-closed execution error categories. Sensor fault injection and real-hardware sensor handling remain intentionally out of scope. |
| 7 | Run artifacts and Manifest | Done (v1 provenance and package identity) | Atomic artifacts, JSON/NPZ samples, events, metrics, logs, complete runtime/environment/backend provenance, stable component hashes, non-self-referential Manifest digest, deterministic package digest, and dirty-worktree evidence linkage are implemented and tested. Dependency locking, real backend discovery, and release-level baseline reports remain later work. |
| 8 | Failure and recovery behavior | Done (v1 Python/file transaction boundary) | Public lifecycle matrix rejects illegal transitions; interrupted/cancelled runs are `INCOMPLETE`, backend failures remain `RUN_FAILED`, checkpoints are validated, resume provenance is recorded, and abandoned temporary directories are scanned without publication. Automatic retention/deletion policy remains an operator-approved follow-up; OS-level crash recovery and physical-model recovery remain out of scope. |
| 9 | Reproducibility | Partial (preparatory v1) | Recommended-environment checks, deterministic Fake/Buck/Boost comparison, and explicit exact/tolerance/unknown outcomes are implemented. Cross-machine and real Ngspice reproducibility is not verified. |
| 10 | Metrics and data export | Partial | Basic samples, events, and sample-count metrics are exported. A metric registry, versioning, comparison, and report generation are planned. |
| 11 | Data visualization | Planned | No supported plotting or report CLI exists yet. Visualizations must retain a link to the source run and Manifest. |
| 12 | Evidence and conclusion levels | Partial | Manifest evidence levels and restrictions are present. Complete qualification/comparability/learning gates and conclusion templates are planned. |
| 13 | Learning and adaptation | Planned | The policy says only eligible evidence may enter learning. No learning, adaptation, checkpoint, or rejection workflow exists. |
| 14 | PySpice/Ngspice migration | Blocked by review | The source tree has not been migrated. PI/PID and model implementations require independent technical, provenance, and license review first. |
| 15 | Testing and acceptance | Partial | Local tests cover the v0 contracts, Runner, artifacts, Schema, provenance, and dirty modes. Real PSFB acceptance, full CI tiers, visualization, and migration gates are future work. |
| 16 | Agent collaboration governance | Partial | Agent policy and independent acceptance are documented. Task records, approval evidence, and automated hand-off gates are not yet standardized. |

## Completed governance additions

- **MIT license** applies to project-owned infrastructure and future reviewed
  project-owned examples. It does not relicense unreviewed `D:\PySpice` or
  third-party material.
- **Git baseline** is established on `main` with tag `v0.1.0`; later commits
  add provenance and governance improvements. The baseline is source identity,
  not a correctness claim.
- **CI** runs lightweight installation, Schema, test, CLI, and Fake smoke checks
  on supported Python versions. It does not validate physical model validity,
  hardware safety, or a user's engineering conclusion.
- **Dirty analysis and run modes** are implemented in `pe_sim.dirty`. The
  read-only `pe-sim git-status` command reports clean/dirty/unknown status,
  categorized paths, reasons, and cleanup suggestions. Exploratory runs may
  proceed and record the report; `formal_comparison` refuses dirty or unknown
  source state before creating a run directory.
- **Responsibility boundary**: the maintainer provides tools, reference code,
  documentation, and known limitations. A GitHub user is responsible for
  evaluating and using the project, including models, parameters, results,
  safety, compliance, and engineering decisions. No support, result guarantee,
  or hardware approval is promised.

## Immediate backlog before real-model migration or formal comparison

1. Keep the dirty analysis and formal gate as a required entry point for formal
   comparison; add a release/baseline report that records commit, CI result,
   tested environment, and human review.
2. Provide a reproducible recommended-environment installation/check command;
   distinguish verified versions from merely supported version ranges.
3. Add a source/third-party inventory template and review gate before any
   `D:\PySpice` file, model, document, or result enters this repository.
4. Split CI into fast per-change checks and manually triggered expensive or
   real-physics checks; keep long simulations out of ordinary push/PR jobs.
5. Record the named project maintainer and the user's local experiment reviewer
   without implying that the maintainer approves downstream engineering use.

## Stage 2 and Stage 3 follow-up backlog

The following items are deliberately retained after the current baseline. They
are not evidence that the completed slices are invalid; they identify work
needed before the repository can support broader real-model integration and
formal result exchange.

### Necessary before broader real-model integration

- **Stage 2 environment/release evidence**: add a reproducible environment
  check command, distinguish verified versions from supported ranges, and
  publish a baseline report containing commit, CI result, tested environment,
  and human review record.
- **Stage 2 source and license intake**: add a source/third-party inventory
  template and require a recorded provenance/license decision before external
  code, models, documents, or results enter the repository.
- **Stage 2 CI layering**: keep fast per-change checks in ordinary push/PR
  jobs and move expensive or real-physics checks to explicit manual or
  scheduled workflows.
- **Stage 3 conformance fixture**: implemented as
  `pe_sim.conformance.check_plant_adapter()` and
  `check_controller_adapter()`; run it for every new adapter before
  integration and extend it when the public contract gains new rules.
- **Stage 3 event and multi-rate execution**: define the actual event schedule
  semantics, supported event types, and execution rules; capability declaration
  alone is not sufficient for a real event-driven backend.
- **Stage 3 unit and timing registry**: define approved measurement/action
  units and conversion rules, including how sampling age, offsets, and
  tolerance are represented in artifacts.
- **Stage 3 contract evidence**: record negotiated capabilities, contract
  versions, and compatibility decisions in the run Manifest and add explicit
  migration tests for future schema changes.

### Can follow after the first real adapter

- Lock all Python dependencies and external executables per platform.
- Add richer lifecycle, interruption/recovery, and cross-machine
  reproducibility checks.
- Expand provenance with operating system, dependency, Plant/Controller,
  contract, and CI identifiers.
- Add migration scanners for absolute paths, secrets, generated output, and
  license/source metadata.

### Optional when usage justifies the cost

- Signed releases, SBOM/license automation, package/container distribution,
  cloud real-physics jobs, advanced CI caching, and automatic impact-based
  test selection.

## Stage 4 gap triage

### Immediate before calling the L1 integration slice accepted

- **Completed in the current slice**: add a supported CLI/configuration entry
  for the Buck and Boost references, so users and agents can exercise the
  adapters without importing internal Python classes directly.
- **Completed in the current slice**: add analytical equilibrium sanity checks
  and integration-step sensitivity checks for the ideal averaged equations.
- **Completed in the current slice**: record reference-model parameters,
  topology identity, declared capabilities, and a deterministic model/source
  identity in the run Manifest. Placeholder hashes are no longer used for
  reference adapters, and local checkout roots are omitted from run evidence.
- Keep the acceptance statement explicitly limited to L1 adapter and timing
  behavior. Do not accept PSFB/LLC migration, switching behavior, thermal
  behavior, or hardware conclusions as part of this slice.

### Put in the backlog for later stages

- Generalize Runner result extraction so it does not require a hard-coded
  `vout` measurement name.
- Add topology-level sweeps for duty, input voltage, load, long-run stability,
  and CCM/DCM applicability limits.
- Add one independently reviewed complex topology, choosing PSFB or LLC after
  the L1 acceptance evidence is complete.
- Add real PySpice/Ngspice execution, external executable locking, convergence
  diagnostics, and cross-platform reproducibility only after source/license
  review and environment work are complete.
- Add L2 engineering approximations, including parasitics, losses, magnetics,
  and simplified thermal state, with a separate validation record.

### Optional and not required for the current infrastructure goal

- Implementing both PSFB and LLC immediately, importing network-sourced
  product models, or building a product-grade thermal/electrical model without
  calibration and an independent provenance record.

## Runner modular facade increment

Stage 5 now exposes `run_experiment(spec, plant, controller, options=None)` for
simple use and `RunOptions` with timing, recovery, and audit policies for
advanced use. The legacy `Runner().run(...)` signature remains supported.
Audit and lifecycle implementations are isolated in `pe_sim.runner`; timing,
checkpoint/recovery, and publication internals remain compatibility code in
`runtime.py` until their behavior is extracted with dedicated regression
tests. See `docs/15_runner_modular_api_v1.md`.

The immediate genericity gap for Stage 5 is now closed: action handling is
finite-only by default and no longer assumes a universal `[0, 1]` actuator
range. A Plant declaration or explicit `RunOptions(action_policy=...)` selects
bounded projection when required. The current acceptance evidence is the full
local suite (57 tests); this remains an infrastructure behavior result, not a
physical-model or hardware result.

## Later backlog

### Stage 5 follow-up backlog

- Extract timing, recovery, and publication into independently testable modules
  behind the existing facade; keep `Runner().run(...)` compatibility while the
  internal coordinator evolves.
- Define a complete event queue and deterministic same-time event priority
  policy; the current runner supports fixed control windows, sample offsets,
  action target times, and segmented `plant_step_s` advancement only.
- Standardize Controller snapshot/restore state serialization and validate
  checkpoint hashes and component/contract identities across processes.
- Add process-level checkpoint retention, crash recovery, and explicit resume
  provenance; current interruption handling is a testable Python-level
  `INCOMPLETE` path and is not hardware or operating-system recovery.
- Replace ad-hoc measurement names with a versioned measurement/unit registry
  and explicit result mappings for qualification and visualization.
- Add domain-specific safety/qualification rule packs and a versioned
  action/unit registry. The generic plugin interfaces and observation
  integrity checks are implemented; domain packs remain adapter-specific.
  Action bounds are explicit: generic Runner execution is finite-only by
  default, while a Plant declaration or `RunOptions(action_policy=...)` may
  opt into bounds (see `docs/15_runner_modular_api_v1.md` and
  `docs/16_stage6_safety_validity_qualification_v1.md`).

- Lock dependencies and external executables, including Ngspice, per platform.
- Add Windows and other required CI jobs after the runtime is portable.
- Expand provenance with dependency, OS, Plant/Controller, contract, and CI IDs.
- Implement migration scanners for absolute paths, secrets, generated output,
  and license/source metadata.
- Complete lifecycle/recovery tests, metric registry, comparison tools,
  visualization, evidence gates, and release automation.
- Migrate real models only after the source review and Gate 1/2 acceptance.

## Optional backlog

Cloud real-physics jobs, SBOM/license automation, signed releases, PyPI/Conda/
Docker packaging, advanced CI caching/parallelism, and automatic impact-based
test selection can be added when project usage justifies their cost.

## Current acceptance snapshot

The v0 Fake backend slice has been locally verified with the repository's test
suite and CLI smoke checks. This snapshot is not a claim that the L0 goal is
complete. Before declaring completion, update this document with the exact
test command, commit/tag, environment, reviewer, and remaining limitations.

The Stage 3 incremental contract update is documented in
`docs/12_public_contracts_stage3_v1.md` and was locally verified with
`pytest -q --basetemp .tmp/pytest-stage3` (27 passed) plus a successful
`psfb-step` CLI smoke run. The update does not claim physical model validity.

The Stage 4 L1 reference increment is documented in
`docs/13_stage4_reference_plants_v0.md` and was locally verified with
`pytest -q --basetemp .tmp/pytest-stage4-final` (37 passed) and
`python -m compileall -q src tests`. Buck/Boost integration remains a
reference-adapter result only; it does not establish switching, thermal,
hardware, or product validity.

The Stage 4 immediate-gap increment adds the Buck/Boost CLI/configuration
examples, analytical equilibrium and integration-step regression checks, and
portable reference component identities in Manifest records. Remaining Stage 4
items stay listed above under the later backlog and are intentionally not part
of this L1 acceptance.

The Stage 6 increment adds composable Safety/Validity/Qualification plugins,
structured observation findings, and stable execution-failure categories. It
was locally verified with `pytest -q --basetemp .tmp/pytest-stage6`,
`python -m compileall -q src tests`, and `git diff --check`.
These checks protect runtime evidence only; they do not validate a physical
model, hardware sensor, convergence configuration, or engineering conclusion.
Sensor-fault injection and real-sensor handling remain outside the common
infrastructure scope.

### Future compact diagnostic mechanism

The current Stage 6 checks are intentionally a runtime integrity gate. A later
increment should add a small, backend-neutral diagnostic contract so users can
understand and repair common failures without turning the infrastructure into a
product-grade solver UI. The target problem and approach are:

- Normalize `error`, `warning`, and informational diagnostics with a stable
  code, category, phase, severity, and blocking policy. The current finding
  fields provide the starting point, but non-blocking warning semantics still
  need a dedicated rule and CLI design.
- Preserve a structured location when an adapter can provide one: parameter
  path/value/unit/range, element and pin, net name, timestep, solver phase, or
  Plant/Controller method. Do not invent a location when the backend cannot
  prove it.
- Capture a reference to backend stdout/stderr and solver settings, then add
  small parsers for convergence, process, parameter, and topology failures.
- Add adapter-level preflight hooks for required parameters, units/ranges,
  pin/net connectivity, reference-ground rules, and backend configuration.
  These checks are model/backend-specific and are not part of the generic
  Stage 6 default.
- Emit concise CLI summaries plus links to `manifest.json`, `audit.json`,
  `events.json`, and raw logs. Include likely causes and suggested next checks
  only when they are evidence-based.

This mechanism is for diagnosis and evidence triage. It does not certify model
physics, convergence quality, hardware safety, or product compliance. Sensor
fault injection and real-sensor damage modeling remain outside the common
infrastructure boundary.

### Stage 7-9 implementation direction

The following order is the recommended prerequisite path before real-physics
formal comparison:

1. **Stage 7, provenance and package identity**: record a known `source_commit`
   (or explicitly mark the run non-formal), complete Python/platform/backend
   versions and solver settings, and derive stable Plant/Controller hashes from
   canonical identity/configuration. Define a non-self-referential Manifest
   digest (for example, hash the canonical Manifest with its digest and
   artifact index excluded), then compute a deterministic package/archive hash
   with sorted paths and normalized metadata. Keep the existing dirty-worktree
   report as a formal gate and link its result to the release/baseline report.
   Acceptance evidence is a Manifest whose provenance is complete, whose
   component hashes change when inputs change, and whose digest/package hash
   can be independently recomputed.
2. **Stage 8, failure transaction and recovery semantics**: define and test a
   complete state-transition matrix, including illegal transitions and the
   distinction between `RUN_FAILED`, `INCOMPLETE`, and `DISQUALIFIED`. Make
   artifact writes transactional (temporary directory, flush/close, atomic
   rename, and startup scan for abandoned directories). A crash or partial
   write must never become `RUN_OK`; recovery may validate the last checkpoint
   and mark the old run `INCOMPLETE`, then resume into a new auditable run.
   Add checkpoint retention, recovery provenance, and explicit handling for
   interruption, timeout, cancellation, and backend process loss.
3. **Stage 9, reproducibility evidence**: lock Python dependencies and the
   Ngspice/external executable per platform, record OS/architecture and
   numerical/thread settings, and require source/config/seed/component hashes.
   Run repeated same-machine trials first, then a cross-machine matrix using
   exact hashes where feasible and declared numerical tolerances otherwise.
   A reproducibility report must compare inputs, environment differences,
   sample/metric outputs, and tolerance decisions; unknown backend versions or
   uncontrolled randomness must be reported as non-reproducible rather than
   silently accepted.

These stages solve different practical problems: Stage 7 answers *which exact
code, model, configuration, and environment produced this result*; Stage 8
prevents interrupted or partially written simulations from being mistaken for
valid data and makes long runs recoverable; Stage 9 answers *whether another
run, machine, or later agent can reproduce and meaningfully compare the result*.

### Stage 7 completion record

Stage 7 v1 is implemented in `docs/17_stage7_provenance_and_package_identity_v1.md`.
The runner now records Python/platform/dependency information and adapter-declared
backend/solver metadata in both `environment.json` and `manifest.json`. Missing
backend declarations are explicit limitations. Plant/Controller fallback
identities include stable public configuration, and component digests are bound
into provenance. `manifest_sha256` excludes itself and other derived hash
fields; `package_sha256` covers the canonical Manifest view plus sorted,
relative-path hashes for every non-Manifest file. The dirty report is stored
without the local repository root and linked by `worktree_analysis_sha256`.

Acceptance evidence:

```text
pytest -q --basetemp .tmp/pytest-stage7
python -m compileall -q src tests
git diff --check
```

The acceptance result establishes portable identity and audit behavior only.
It does not lock external executables, validate real Ngspice provenance, or
claim cross-machine numerical reproducibility; those remain Stage 9 work.

### Stage 8 completion record

Stage 8 v1 is implemented in `docs/18_stage8_failure_recovery_v1.md`.
`LifecycleStateMachine.allowed_transitions()` exposes the complete state matrix
and raises `LifecycleTransitionError` for illegal transitions. Artifact files
are flushed and atomically replaced inside a temporary run directory before
directory publication. Startup scans report abandoned writer directories as
`INCOMPLETE` with missing-artifact evidence. Interruption and cooperative
cancellation retain recoverable checkpoints when possible; timeout, backend
process loss, convergence, and validation failures remain `RUN_FAILED`.
Resume validates checkpoint and component/contract identities, rejects a
completed source run, and records portable recovery provenance in the new
Manifest and event stream.

Acceptance evidence:

```text
pytest -q --basetemp .tmp/pytest-stage8
python -m compileall -q src tests
git diff --check
```

This establishes Python/file-transaction semantics only. It does not monitor
or restart an external process, recover machine power loss, or validate a
physical simulator's internal solver state. `result_retention` remains an
explicit configuration field; automatic deletion or archival is deferred
until an operator-approved retention policy is defined.

### Stage 8 deferred recovery and retention work

The following mechanisms are useful for long-running real simulations, but
were intentionally kept outside the Stage 8 v1 acceptance boundary. They must
be added with explicit evidence and operator policy rather than inferred from
the current Python-level checkpoint behavior:

- **External Ngspice/backend supervision (necessary before real batch runs):**
  launch the backend through a managed adapter, capture stdout/stderr, enforce
  timeout and resource limits, detect abnormal exit, and classify the exit
  reason. Restart/resume should be opt-in, bounded, and tied to a validated
  checkpoint; a restarted process must never silently become a successful run.
- **Operating-system interruption recovery (necessary for unattended jobs):**
  use durable run markers and a startup/recovery command to identify runs left
  by process termination, reboot, or power loss. Mark them `INCOMPLETE`, retain
  the last verified checkpoint, and require explicit operator or scheduler
  approval before resuming. This is recovery orchestration, not proof that a
  solver's internal state survived the interruption.
- **Retention and archival policy (necessary when storage is constrained):**
  define the meaning of `keep`, `on_failure`, and `temporary`, including a
  minimum evidence set, grace period, archival destination, deletion audit,
  and protection for `RUN_FAILED`/`INCOMPLETE` evidence. No automatic deletion
  should be enabled until this policy is reviewed and tested.
- **Physical-model recovery (adapter-specific and lower priority):**
  diagnose or repair invalid parameters, topology errors, and non-convergent
  model states only through a reviewed backend/Plant adapter. The generic
  Runner may report and preserve the failure, but must not invent component
  values or automatically alter a user's circuit.

These items address process resilience and operations. They do not replace
Stage 6 data-validity checks or establish physical, thermal, hardware, or
product safety.

### Stage 9 readiness and Stage 7 residual triage

Stage 9 can begin now as a **preparatory reproducibility slice**, while the
real-backend and cross-machine claims remain gated:

- **Can be implemented now:** a recommended-environment check command,
  verified-version versus supported-range reporting, dependency lock metadata
  for the current Python runtime, same-machine repeated-run comparison for the
  deterministic Fake/Buck/Boost references, and a reproducibility report
  format with explicit exact/tolerance/unknown outcomes.
- **Requires a reviewed real adapter:** Ngspice executable discovery and
  locking, solver/thread/locale capture, real-model repeated runs, and
  cross-machine numerical comparison. These depend on Stage 14 source/license
  review and a concrete backend contract.
- **Still a Stage 7 follow-up:** release/baseline report linking commit, CI,
  environment and human review; optional dirty-worktree diff/snapshot evidence
  for exploratory runs; and a policy for publishing runs whose backend status
  is `not_declared` or whose environment is only `partial`.

The recommended order is to complete the low-risk Stage 7 environment/release
evidence and Stage 9 preparatory checks first, then add real-backend locking and
cross-machine trials after source and license review. It is not appropriate to
claim Stage 9 complete using only the current Fake backend evidence.

### Stage 9 preparatory slice completion record

`docs/19_stage9_preparatory_reproducibility_v1.md` documents the implemented
low-risk slice. `pe-sim environment-check` compares the verified pins in
`requirements-tested.txt` with the supported ranges in `pyproject.toml` and
reports `pass`, `fail`, or `unknown` without assessing Ngspice. The
`pe_sim.compare_runs()` API and `pe-sim compare-runs` CLI compare clean,
known-provenance Fake/Buck/Boost runs and distinguish exact, tolerance,
mismatch, and unknown outcomes. Dirty or unknown environment evidence is never
accepted as reproducible. `build_baseline_report()` and
`pe-sim baseline-report` provide a release/baseline evidence template whose
CI and human-review fields remain `not_recorded` until an operator supplies
them.

Acceptance evidence:

```text
pytest -q tests/test_stage9_preparation.py --basetemp .tmp/pytest-stage9-prep
python -m compileall -q src tests
git diff --check
```

This does not lock or discover Ngspice, validate a physical model, or prove
cross-machine numerical reproducibility.

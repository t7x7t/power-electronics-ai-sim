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
| 5 | Generic Runner | Partial (modular audited lifecycle v1) | Low-friction `run_experiment` facade, pre-reset run-directory/identifier gates, formal-evidence admission, explicit CREATED/RUNNING/terminal state transitions, call-order audit, checkpoint/resume with full execution-spec identity, explicit Controller state protocols, interruption as INCOMPLETE, structured capability failures, policy-driven action handling, configured primary measurements, and endpoint-checked segmented plant timing are implemented and tested. Process-level recovery, full event scheduling, and richer timing semantics remain future work. |
| 6 | Safety and qualification checks | Done (v1 extensible runtime checks) | `SafetyPlugin`, `ValidityPlugin`, and `QualificationPlugin` are composable through `RunOptions`; structured findings cover observation integrity (finite values, timestamps, age, required fields, units, ranges), qualification, and fail-closed execution error categories. Sensor fault injection and real-hardware sensor handling remain intentionally out of scope. |
| 7 | Run artifacts and Manifest | Done (v1 provenance and package identity) | Atomic artifacts, JSON/NPZ samples, events, metrics, logs, complete runtime/environment/backend provenance, stable component hashes, non-self-referential Manifest digest, deterministic package digest, and dirty-worktree evidence linkage are implemented and tested. Dependency locking, real backend discovery, and release-level baseline reports remain later work. |
| 8 | Failure and recovery behavior | Done (v1 Python/file transaction boundary) | Public lifecycle matrix rejects illegal transitions; interrupted/cancelled runs are `INCOMPLETE`, backend failures remain `RUN_FAILED`, checkpoints are validated, resume provenance is recorded, and abandoned temporary directories are scanned without publication. Automatic retention/deletion policy remains an operator-approved follow-up; OS-level crash recovery and physical-model recovery remain out of scope. |
| 9 | Reproducibility | Partial (preparatory v1) | Recommended-environment checks, deterministic Fake/Buck/Boost comparison, and explicit exact/tolerance/unknown outcomes are implemented. Cross-machine and real Ngspice reproducibility is not verified. |
| 10 | Metrics and data export | Partial (restricted v1 slice) | Published qualified Fake/FakeLoad/Buck/Boost packages now support a versioned metric registry, explicit input-data version/fingerprint records, machine-checkable output schemas, numeric multi-run comparison, and source-linked JSON evidence reports. Domain metric registries, richer units/statistics, visualization, and physical conclusions remain later work. |
| 11 | Data visualization | Planned | No supported plotting or report CLI exists yet. Visualizations must retain a link to the source run and Manifest. |
| 12 | Evidence and conclusion levels | Partial (restricted v1 classification) | Stage 10 summaries/comparison reports can be classified as mechanism, functional, comparable, limited, or diagnostic_only with explicit limitations, blocked conclusions, and review records. Physical, research-safety, and hardware-readiness upgrades remain prohibited and domain conclusion templates remain later work. |
| 13 | Optional learning/adaptation integration boundary | Optional (boundary only) | Learning and adaptation are not required for the core simulation infrastructure. The project defines an opt-in boundary for reviewed adapters: only eligible, qualified, provenance-complete evidence may be handed to an external learner. No built-in trainer, online-learning loop, model update, or automatic adaptation workflow is planned for the core. |
| 14 | PySpice/Ngspice migration | Blocked by review | The source tree has not been migrated. PI/PID and model implementations require independent technical, provenance, and license review first. |
| 15 | Testing and acceptance | Partial (L1 Buck/Boost slice) | The project-owned ideal averaged Buck/Boost adapters now have a dedicated acceptance matrix covering equations, boundaries, numerical step sensitivity, small sweeps/long runs, conformance, Runner lifecycle/recovery, deterministic repetition, CLI smoke, and the Stage 10/12 evidence chain. This is infrastructure acceptance only; real Ngspice/PSFB, switching, thermal, hardware, product, visualization, migration, and final full-project gates remain future work. |
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

`docs/21_stage5_preflight_and_state_protocol_v1.md` records the next focused
Stage 5 increment: safe identifiers and output conflicts are rejected before
adapter reset, formal comparison has explicit evidence admission, Plant timing
must reach each requested window endpoint, checkpoint resume binds the complete
execution specification, and Controller state persistence uses an explicit
snapshot/restore or reset-serialized-state protocol. Non-blocking warnings are
now retained as diagnostics rather than being treated as execution failures.

## Later backlog

### Stage 5 follow-up backlog

- Extract timing, recovery, and publication into independently testable modules
  behind the existing facade; keep `Runner().run(...)` compatibility while the
  internal coordinator evolves.
- Define a complete event queue and deterministic same-time event priority
  policy; the current runner supports fixed control windows, sample offsets,
  action target times, and segmented `plant_step_s` advancement only.
- Extract timing, recovery, and publication into independently testable
  modules; keep the current Controller state protocols and checkpoint identity
  gates stable while the internal coordinator evolves.
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

### Stage 13 optional learning/adaptation boundary

The detailed boundary contract is documented in
`docs/20_stage13_optional_learning_boundary_v0.md`.

Stage 13 is deliberately an integration boundary rather than a mandatory
feature stage. Most users only need deterministic simulation, checks, artifacts,
comparison, and visualization; they do not need a trainer or an online adaptive
controller in this repository. Therefore the core project will not ship an
opinionated training framework, replay store, optimizer, policy updater, or
automatic parameter-tuning loop.

An advanced user may attach a project- or domain-specific learner through a
separate adapter. Such an adapter must consume an explicit, versioned contract
and remain subject to the existing gates:

- only evidence that is qualified and explicitly marked `LEARNING_ELIGIBLE` may
  be supplied to a learner;
- failed, incomplete, dirty, unknown-provenance, non-comparable, or
  human-unreviewed runs remain diagnostic-only;
- source/model/configuration/contract hashes and the learner or adapter version
  must be recorded in an auditable hand-off record;
- an update must be reviewable and reversible; the core Runner must not silently
  replace a controller, alter a model, or promote a learned result.

This boundary preserves extensibility without turning the common infrastructure
into a machine-learning product. Concrete learning semantics, datasets,
algorithm choice, update cadence, and acceptance criteria belong to the
downstream user or a separately reviewed adapter.

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

### Stage 5 preflight and state-protocol increment (current)

`docs/21_stage5_preflight_and_state_protocol_v1.md` records the implemented
preflight and recovery-contract slice. Run identifiers and output conflicts are
rejected before adapter reset, formal comparison has explicit seed/contract/
source/component/environment admission checks, Plant windows must reach their
requested endpoints, checkpoints bind the execution-semantic experiment hash,
and Controller persistence uses an explicit state protocol. Non-blocking
warning findings remain recorded without stopping a run.

Acceptance evidence:

```text
pytest -q --basetemp .tmp/pytest-stage5-preflight
python -m compileall -q src tests
git diff --check
```

This remains a Python-level runtime contract. It does not provide operating
system crash recovery, external Ngspice supervision, or physical validation.

### Stage 10 restricted post-processing increment (current)

`docs/21_stage10_restricted_metrics_export_v1.md` records the first post-run
slice. `load_run_evidence()` and `summarize_run()` consume only published,
qualified, clean, hash-complete reference packages; `export_metrics()` writes
linked JSON/CSV outside the immutable run directory. The slice is limited to
Fake/FakeLoad/Buck/Boost. A versioned metric registry, explicit data-contract
fingerprints, multi-run comparison, and source-linked evidence report are now
available; plots, physical validation, and engineering conclusions remain
outside this stage.

Acceptance evidence:

```text
pytest -q tests/test_stage10_restricted_postprocess.py --basetemp .tmp/pytest-stage10
pytest -q tests/test_stage10_metric_registry.py --basetemp .tmp/pytest-stage10-metrics
```

The increment is deliberately limited to numeric post-processing. Unknown
metric IDs are rejected, custom metrics require an explicit versioned
definition, and comparison/report outputs repeat every source run's `run_id`,
Manifest hash, and package hash. No plotting, controller-quality judgment,
physical validation, or engineering approval is produced.

### Stage 12 restricted evidence classification (current)

`docs/22_stage12_evidence_levels_v1.md` and
`schemas/stage12-evidence-classification.schema.json` define the current
machine-checkable boundary. Valid Stage 10 summaries may be classified as
`mechanism` or `functional`; a complete matching Stage 10 comparison may be
classified as `comparable`; mismatches and incomplete comparisons are
`limited`, while structurally valid failed or explicitly diagnostic evidence is
`diagnostic_only`. Dirty, unknown, malformed, or hash-incomplete provenance is
rejected at the classifier boundary and cannot receive any level.
Every output carries limitations, blocked conclusions, a required review flag,
and a review record. Physical-performance, research-safety, and
hardware-readiness are never emitted automatically.

Acceptance evidence:

```text
pytest -q tests/test_stage12_evidence.py --basetemp .tmp/pytest-stage12
```

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

### Stage 9 reproducibility evidence increment (current)

The preparatory slice now exposes an explicit `BackendProvenanceAdapter`
protocol and `ExecutableBackendAdapter`.  External backend version queries are
opt-in; absent adapters are `not_assessed`, failed queries are retained as
`unavailable`/`partial`, and solver settings are never inferred.  Environment
records include locale, floating-point characteristics, and common
thread-environment hints in addition to platform and dependency evidence.

`pe-sim audit-reproducibility` (aliases `reproducibility-audit` and
`audit-runs`) compares every pair in a repeated-run set and aggregates exact,
tolerance, mismatch, and unknown outcomes.  `pe-sim cross-machine-evidence`
builds a portable platform/environment matrix and returns `ready_for_trial`
only when distinct platform identities and valid provenance are present; it
does not claim cross-machine numerical equivalence.

`ReproducibilityReport` now includes identity hashes, environment equality, and
numeric/structural difference attribution.  A tolerance match is explicitly
distinct from an exact hash match.

Published-package comparisons now run an integrity gate before reading sample
values. The gate recomputes the Manifest and package SHA-256 digests, checks
the complete artifact index and required files, requires a `PUBLISHED` state
marker and a publishable terminal status, and validates a full hexadecimal
Git object id in `source_commit`. `RUN_FAILED`, `INCOMPLETE`, `DISQUALIFIED`,
missing-summary, or tampered packages are reported as `unknown`. Hand-written
manifests used by the preparatory fixtures remain explicitly non-release-grade
compatibility inputs.

Comparison is fail-closed for explicitly attempted external backends: an
`unknown`, `unavailable`, or `partial` backend record produces
`outcome=unknown`.  Only the built-in preparatory fixtures may proceed with
`not_declared`/`not_assessed` backend evidence, and their limitations remain
visible in the report.  Cross-machine evidence also requires aligned source
commits before it can be marked `ready_for_trial`.

Remaining Stage 9 work still requires a reviewed real backend adapter and is
not silently closed by these tools:

- lock and attest the Ngspice executable, solver tolerances, thread count,
  locale, and platform-specific numerical settings;
- repeat a real physical model on one machine and record backend logs;
- execute a cross-machine trial with independently captured evidence and a
  declared tolerance/attribution policy;
- investigate exact-hash versus numeric-tolerance drift (BLAS, compiler,
  architecture, and solver causes) without upgrading a functional fixture
  result into a physical or product claim.

Acceptance evidence for this increment:

```text
pytest -q tests/test_stage9_preparation.py tests/test_stage9_reproducibility.py --basetemp .tmp/pytest-stage9-repro
python -m compileall -q src tests
git diff --check
```

### Stage 15 L1 Buck/Boost acceptance increment (current)

`docs/23_stage15_l1_buck_boost_acceptance_v1.md` defines the restricted
acceptance matrix for the project-owned ideal averaged `BuckPlant` and
`BoostPlant`. The matrix covers public Plant/Controller conformance, declared
equilibrium equations, fixed-step sensitivity, parameter and duty boundaries,
small duty sweeps and finite long runs, Runner lifecycle/checkpoints and
Manifest/package hashes, interruption/resume, deterministic repeated samples,
CLI smoke runs, and the Stage 10 to Stage 12 provenance hand-off.

Acceptance evidence for this increment:

```text
pytest -q tests/test_stage15_buck_boost_acceptance.py --basetemp .tmp/pytest-stage15-l1
pytest -q --basetemp .tmp/pytest-stage15-full
python -m compileall -q src tests
git diff --check
```

The L1 result is infrastructure and reference-model evidence only. It does
not validate Ngspice, switching waveforms, convergence, thermal/loss models,
hardware or product behavior, cross-machine equivalence, Stage 11
visualization, Stage 13 learning, or Stage 14 migration. Stage 15 remains
`Partial` until the explicitly excluded real-model and final project gates
are separately reviewed.

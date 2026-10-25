# Project Backlog and Stage Map (v0)

This document is the hand-off checklist for the complete project. It records
what is implemented, what is only specified, and what must be reviewed before
the project can claim the L0 goal. A passing test is evidence for the tested
behavior only; it is not a hardware or product approval.

**Sequencing authority:** this document records *status*, not *order*. The
authoritative sequence, phase gates, invariants, and open decisions are in
`docs/43_kernel_roadmap_v1.md`. A stage marked `Planned` here does not mean it
is next.

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
| 2 | Project and environment configuration | Partial (release preparation) | `pyproject.toml`, MIT license, Git baseline, tested-version record, exact Python release policy, and light CI exist. The local release gate and a clean tagged baseline are still required. |
| 3 | Public data and interface contracts | Done (v1 incremental) | v0 contracts are extended with normalized capability negotiation, explicit event/multi-rate gates, schema compatibility checks, observation visibility/time-window checks, snapshot hash verification, and regression tests. A reusable Plant/Controller conformance harness is now available. Full event scheduling semantics and richer unit registries remain future work. |
| 4 | Plant and Controller integration | Partial (Buck reference + test fixtures) | The Buck reference is the PySpice-constructed switching model executed by ngspice (`docs/31`). Ideal averaged Buck and Boost Plants remain deterministic test fixtures for lifecycle, units, external-input updates, snapshots, and fail-closed bounds. Fake Plant, Fake Load Plant, and Fake PI Controller remain compatibility fixtures. PSFB/LLC migration is intentionally deferred pending review. |
| 5 | Generic Runner | Partial (modular audited lifecycle v1) | Low-friction `run_experiment` facade, pre-reset run-directory/identifier gates, formal-evidence admission, explicit CREATED/RUNNING/terminal state transitions, call-order audit, checkpoint/resume with full execution-spec identity, explicit Controller state protocols, interruption as INCOMPLETE, structured capability failures, policy-driven action handling, configured primary measurements, and endpoint-checked segmented plant timing are implemented and tested. Process-level recovery, full event scheduling, and richer timing semantics remain future work. |
| 6 | Safety and qualification checks | Done (v1 extensible runtime checks) | `SafetyPlugin`, `ValidityPlugin`, and `QualificationPlugin` are composable through `RunOptions`; structured findings cover observation integrity (finite values, timestamps, age, required fields, units, ranges), qualification, and fail-closed execution error categories. Sensor fault injection and real-hardware sensor handling remain intentionally out of scope. |
| 7 | Run artifacts and Manifest | Done (v1 provenance and package identity) | Atomic artifacts, JSON/NPZ samples, events, metrics, logs, complete runtime/environment/backend provenance, stable component hashes, non-self-referential Manifest digest, deterministic package digest, and dirty-worktree evidence linkage are implemented and tested. Dependency locking, real backend discovery, and release-level baseline reports remain later work. |
| 8 | Failure and recovery behavior | Done (v1 Python/file transaction boundary) | Public lifecycle matrix rejects illegal transitions; interrupted/cancelled runs are `INCOMPLETE`, backend failures remain `RUN_FAILED`, checkpoints are validated, resume provenance is recorded, and abandoned temporary directories are scanned without publication. Automatic retention/deletion policy remains an operator-approved follow-up; OS-level crash recovery and physical-model recovery remain out of scope. |
| 9 | Reproducibility | Partial (preparatory v1) | Recommended-environment checks, deterministic Fake/Buck/Boost comparison, and explicit exact/tolerance/unknown outcomes are implemented. Cross-machine and real Ngspice reproducibility is not verified. |
| 10 | Metrics and data export | Partial (restricted v1 slice) | Published qualified Fake/FakeLoad/Buck/Boost packages now support a versioned metric registry, explicit input-data version/fingerprint records, machine-checkable output schemas, numeric multi-run comparison, and source-linked JSON evidence reports. Domain metric registries, richer units/statistics, visualization, and physical conclusions remain later work. |
| 11 | Data visualization | Partial (headless slice; browser UI withdrawn) | Renderer-neutral `TopologyDescriptor`, `TraceDataset`/`SignalRegistry`, and `OutputDataset` contracts are implemented, explicitly published, validated, provenance-aware, immutable at the published boundary, and locally tested with Buck/Boost topology exporters. `pe_sim.workbench_export` is a constrained offline verified-run-to-fixture adapter, while `pe-sim visualization-serve` provides a versioned local read-only service for verified L1 Buck/Boost packages. The TypeScript/Vite browser workbench was temporarily withdrawn on 2026-10-23; `examples/visualization_consumer/` remains an independent headless consumer without simulation authority. Live delivery, a general remote API, and a general plugin runtime remain deferred. |
| 12 | Evidence and conclusion levels | Partial (restricted v1 classification) | Stage 10 summaries/comparison reports can be classified as mechanism, functional, comparable, limited, or diagnostic_only with explicit limitations, blocked conclusions, and review records. Physical, research-safety, and hardware-readiness upgrades remain prohibited and domain conclusion templates remain later work. |
| 13 | Optional learning/adaptation integration boundary | Optional (boundary only) | Learning and adaptation are not required for the core simulation infrastructure. The project defines an opt-in boundary for reviewed adapters: only eligible, qualified, provenance-complete evidence may be handed to an external learner. No built-in trainer, online-learning loop, model update, or automatic adaptation workflow is planned for the core. |
| 14 | PySpice/Ngspice integration | Partial (PySpice construction + ngspice execution) | The referenced switching Buck circuit is constructed through the PySpice object API and executed by the reviewed ngspice subprocess adapter in `pe_sim.backends` (scoped by `docs/31`). PySpice native solver execution remains excluded for GPLv3 and compatibility reasons (ADR 0002); the native deterministic netlist builder remains a test/CI fallback. Real-executable acceptance is opt-in through `PE_SIM_NGSPICE`; cross-machine equivalence, PSFB/LLC, and device/thermal models remain open. |
| 15 | Testing and acceptance | Partial (Buck reference + averaged fixtures) | The averaged Buck/Boost test fixtures have a dedicated deterministic acceptance matrix covering equations, boundaries, numerical step sensitivity, small sweeps/long runs, conformance, Runner lifecycle/recovery, deterministic repetition, CLI smoke, and the Stage 10/12 evidence chain. The Buck reference additionally requires PySpice construction plus real ngspice execution acceptance. This is infrastructure acceptance only; PSFB, thermal, hardware, product, and final full-project gates remain future work. A local release gate now records machine-readable evidence; browser interaction and human release review remain open. |
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
Buck-fixture CLI smoke run. The update does not claim physical model validity.

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

### Stage 14 ngspice process-boundary increment (current)

`docs/31_ngspice_backend_scope_v1.md` defines the first reviewed external
simulator surface, and `docs/adr/0002-ngspice-process-boundary.md` records the
process-boundary decision. The referenced Buck circuit is constructed through
the PySpice object API; the native deterministic netlist builder remains a
test/CI fallback. The implementation is isolated in `src/pe_sim/backends/`:

- PySpice circuit construction and native deterministic netlist generation,
  both with a stable digest;
- a fail-closed ASCII parser for `wrdata` output;
- a bounded ngspice subprocess runner with timeout, exit-code, log, and
  convergence classification;
- `NgspiceBuckPlant`, which solves one zero-order-hold control window per
  invocation and interpolates segment endpoints;
- an executable provenance adapter that records version, SHA-256, and explicit
  solver settings;
- CLI integration (`--backend ngspice-buck`, `--ngspice`) and the
  `examples/ngspice_buck_step/` configuration;
- restricted Stage 10 and Stage 11 admission for the `NgspiceBuckPlant`
  family.

Acceptance evidence:

```text
pytest -q --basetemp .tmp/pytest-stage14
python -m compileall -q src tests
git diff --check
```

Real-executable acceptance is opt-in:

```text
$env:PE_SIM_NGSPICE = "<path to ngspice console executable>"
pytest -q tests/test_ngspice_real_backend.py --basetemp .tmp/pytest-stage14-real
pe-sim run examples/ngspice_buck_step/config.json --backend ngspice-buck
```

The real-executable acceptance was verified locally on 2026-10-21 with
ngspice 41 (console `ngspice_con.exe`, SHA-256
`41a6bda700d9160089f2a39a14f94408172f7748215b68b81d39ea1bc26e300c`):
5 real-backend tests passed, including contract, provenance, CCM equilibrium,
determinism, and a published `QUALIFIED` CLI package. This remains reference
infrastructure evidence, not a physical or hardware claim.

This slice does not migrate `D:\PySpice` material, does not link PySpice, and
does not claim device, thermal, hardware, product, cross-machine, or
PSFB/LLC validity. The switching Buck model is one reviewed ideal reference
circuit.

An optional PySpice circuit frontend is available as a model builder: PySpice
constructs the reviewed circuit, while execution stays on the verified ngspice
process adapter. PySpice 1.5's native shared-library and subprocess-server modes
were verified to be incompatible with local ngspice 41, so native PySpice
execution is not claimed. See `docs/34_pyspice_optional_backend_v0.md`.

The end-user acceptance harness (`scripts/user_acceptance.py`, scope in
`docs/36_user_acceptance_v0.md`) starts from a clean Git snapshot and a fresh
virtual environment. It covers the no-simulator onboarding path, persistent
user configuration, a `QUALIFIED` ngspice run, parameter variation,
determinism, `pe-sim export-workbench`, and the external visualization
consumer. It verified both editable and wheel installations on 2026-10-21.
Two packaging defects were found and fixed: `jsonschema` was not a declared
runtime dependency, and the default experiment schema was not shipped in the
wheel. Regression coverage is in `tests/test_packaging.py`.

### User-discovery follow-ups (backlog)

An independent new-user test on 2026-10-22 is recorded in
`docs/37_user_discovery_backlog_v1.md`. It fixed the onboarding command typo
(`--name ngspice`), the PySpice/NumPy dependency conflict with
`requirements-pyspice-tested.txt`, backend selection (CLI now maps `plant_id`
to a reviewed backend and rejects mismatches), the false PI claim (real
integral state, limits, anti-windup, checkpoint/restore, strict configuration
keys), and removed the misleading PSFB fake example/CLI. The functional
follow-ups remain backlog, not release claims: PID/feedforward controller
configuration and registry, switching-level topology coverage, and documented
access to switching-resolution waveforms for ripple/loss analysis.

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

### Stage 16 applicability and authenticity L1 (0.5.0)

`docs/40_validity_and_authenticity_v1.md` defines the first implementation of
the two remaining credibility questions:

- **Applicability:** adapters may declare a `ModelClaim` and
  `ParameterProvenance`; the Runner records or enforces the declared envelope
  according to `RunOptions.applicability`. Qualification now distinguishes
  `full`, `limited`, and `failed` (renamed to `assessed` / `partial` / `failed`
  in 0.6.0; see `docs/50_evidence_vocabulary_v2.md`). A standard rule pack
  adds steady-state, analytical-equilibrium, and numerical-health checks.
  Successful solver logs are classified into bounded numerical-health
  evidence.
- **Credibility:** `inspect-run` now calculates a read-only factor assessment
  (`credible`, `limited`, `unknown`, `not_credible`) and exposes
  `qualification.level`. It never changes the integrity-only `status`.
- **Authenticity L1:** `attest-run` writes a detached attestation outside the
  run package and appends an anchor record to a Git-tracked
  `attestations/anchor-index.jsonl`. `verify-attestation` reports
  `L1_anchored` only when the exact record and attestation are in a reachable
  Git commit. The payload/signatures split reserves Level 2 signatures without
  changing the Level 1 payload.

Acceptance evidence:

```text
pytest -q tests/test_applicability.py tests/test_qualification_rules.py `
  tests/test_credibility.py tests/test_attestation.py --basetemp .tmp/pytest-stage16
```

L1 is a visibility anchor, not a security boundary. Run lineage, conformance-report
hashing, schema migration, and L2 signing remain open; the tolerance component
of CRED-04 and the public-surface component of COMPAT-02 were closed in 0.5.1.
The Stage 6 increment adds composable Safety/Validity/Qualification plugins,
structured observation findings, and stable execution-failure categories. It
was locally verified with `pytest -q --basetemp .tmp/pytest-stage6`,
`python -m compileall -q src tests`, and `git diff --check`.
These checks protect runtime evidence only; they do not validate a physical
model, hardware sensor, convergence configuration, or engineering conclusion.
Sensor-fault injection and real-sensor handling remain outside the common
infrastructure scope.

### Stage 17 kernel compatibility surface (0.5.1)

`docs/41_kernel_compatibility_surface_v1.md` closes three properties that were
previously decided ad hoc per change:

- **Public API tiers:** `pe_sim.public_api` classifies the top-level surface
  into `stable` (81 names) and `experimental` (141 names);
  `tests/fixtures/public_api.json` freezes the stable list so any change fails
  a test until the baseline is updated in the same commit.
- **Reference-plant review pins:** `buck` and `boost` use the same reviewed
  identity mechanism as `ngspice-buck`. `runtime.py` stays outside the pin and
  is declared as an `implementation.runner_loop` rule.
- **Tolerance policy:** `ToleranceSpec` adds relative and per-signal limits,
  records the policy digest in the comparison report, and reproduces the
  historical absolute-only behaviour when no policy is supplied.

Sample grid alignment is still positional and is the remaining half of CRED-04;
resampling is a prerequisite for UD-04 rather than a defect in this increment.
A deprecation schedule for stable names is deferred to 1.0.

Acceptance evidence:

```text
pytest -q tests/test_public_api.py tests/test_reference_plant_identity.py `
  tests/test_tolerance_policy.py --basetemp .tmp/pytest-stage17
```

### Stage 18 kernel interface topology (0.5.2)

`docs/42_kernel_interface_topology_v1.md` assesses whether the kernel is
correctly shaped for interface-based plug-ins of agents and physical simulators.

Conclusion: `PlantAdapter` already is the simulator interface and the Runner is
pure injection, so the shape is right. `EngineAdapter` and `AgentAdapter` are
deliberately **not** added because the kernel would only duplicate `PlantAdapter`
or create a second, self-reported trust path.

Three seam defects were fixed:

- **CRED-10:** the formal gate trusted an adapter's self-declared
  `review.status`. `identity.verify_adapter_identity` now recomputes the digest,
  the pin lookup, and the declaring module, and records the verdict at
  `plant.verification` outside the identity digest.
- **Manifest contract:** `evidence_level` declared the old Stage 12 vocabulary,
  so `comparable` and `limited` were rejected by the shipped schema and by
  `scripts/user_acceptance.py`. The schema now declares the runtime vocabulary,
  with a drift test.
- **CLI wiring:** four parallel encodings of the built-in plant catalog
  collapsed into one `_PlantBinding` table.

Acceptance evidence:

```text
pytest -q tests/test_adapter_verification.py `
  tests/test_manifest_evidence_level_contract.py --basetemp .tmp/pytest-stage18
```

### Stage 19 controller registry and PID/feedforward (0.5.3)

UD-01's registry and control-law work, recorded in `docs/37`:

- The CLI resolves `controller_id` through a binding table (`pi`, `pid`) whose
  rows declare the accepted `controller_config` keys, replacing the hardcoded
  single-id check. A PID key under `pi` is rejected instead of ignored.
- The reference controller gains a derivative term with optional first-order
  filtering and a reference feedforward term. A zero gain disables the term and
  omits it from the controller identity, so `pi` behaviour and identity are
  unchanged from 0.5.2.
- Derivative state is part of the controller snapshot, so checkpoint and resume
  remain deterministic.
- Both `experiment.schema.json` copies accept the new keys.

Load compensation needs a load estimate the kernel cannot observe, so only
reference feedforward is implemented. Custom controller classes remain a
Python-API feature until UD-04 adds adapter discovery.

Acceptance evidence:

```text
pytest -q tests/test_pid_controller.py tests/test_pi_controller.py `
  tests/test_stage8_failure_recovery.py --basetemp .tmp/pytest-stage19
```

### Stage 20 plant registry and extension gate (0.5.4)

Roadmap Phase A1 (`docs/43_kernel_roadmap_v1.md`). The kernel stopped naming the
plants it ships with.

- `pe_sim.plant_registry` holds `PlantRegistration` data (id, backend id,
  factory, schedule keys, config keys, trust level, export eligibility) and
  resolves a `plant_id` on demand. Importing it cannot start a simulator.
- `pe_sim.builtin_plants` is the project-owned composition root; the CLI loads
  it at start-up instead of keeping its own table.
- Third-party distributions register through the `pe_sim.plants` entry-point
  group. Discovery runs at most once and is only triggered by a lookup miss, so
  the built-in path never imports third-party code.
- Trust is assigned, not claimed: entry-point registrations are forced to
  `third-party` and `export_eligible = False`. The value is recorded at
  `manifest.plant.trust_level`.

Four kernel enumerations were removed: the CLI binding table and `--backend`
allow-list, the `plant_id` enum in `experiment.schema.json`, the six hardcoded
modules in `identity`, and the class-name list in `postprocess`.

Sample overrides, `plant_config` validation, and `input_schedule` validation now
come from the registration, so a plant cannot be half-registered.

Still open: the time-model abstraction (A2) is blocked on decision D1, and
engines that cannot be stepped therefore remain unsupported.

Acceptance evidence:

```text
pytest -q tests/test_plant_registry.py tests/test_plant_extension_gate.py `
  tests/test_plant_id_schema.py --basetemp .tmp/pytest-stage20
```

### Stage 21 read-only run catalog (0.5.5)

Roadmap Phase B, read-only query surface (`docs/43_kernel_roadmap_v1.md`).

`pe_sim.catalog.scan_runs` walks a runs root and describes every package it
finds from recorded metadata, exposed as `pe-sim list-runs`. Properties that
matter:

- **It is discovery, not verification.** It reads `manifest.json` and
  `.run-state.json` only, reports `limitations` saying so, and leaves integrity
  checking to `inspect-run`. There is one definition of "valid package".
- **It never writes.** A regression test compares file mtimes and sizes before
  and after a scan.
- **It reports what it cannot read.** An unreadable manifest is listed in
  `skipped` with a reason, and a missing state marker becomes an entry with a
  problem, because a silently absent entry is indistinguishable from a missing
  run.
- **Ordering is deterministic** (newest `created_at` first, then `run_id`), so
  two clients see the same list.
- Staging directories (leading dot) and trees deeper than the depth limit are
  pruned.

Acceptance evidence:

```text
pytest -q tests/test_run_catalog.py --basetemp .tmp/pytest-stage21
```

### Stage 22 run interaction contract (0.5.6)

Roadmap Phase B items 1 and 2 (`docs/43_kernel_roadmap_v1.md`). The kernel now
has a versioned way for a client to observe and stop a run, instead of the
`cancel_after_steps` test hook being the only option.

- `pe_sim.run_events` defines `pe-sim.run-event.v1`
  (`run.started` / `run.progress` / `run.finished`) and `pe-sim.run-result.v1`
  for the CLI's terminal result. Progress is rate-limited and is explicitly
  **not evidence**: it is never written into the package.
- `RunEventEmitter` is best-effort. A failing sink is counted in `metrics.json`
  and cannot change a status, a qualification, or the samples.
- `pe_sim.cancellation` defines `CancelToken.cancelled()`, polled once per
  control-window boundary. `FileCancelToken` backs `--cancel-file`; the runner
  only reads the path and never deletes the client's request.
- A cancelled run is `INCOMPLETE` with failure category `cancelled`, a
  checkpoint, and `recoverable: true`. It stays distinct from an operator
  interrupt (`interrupted`).
- `pe-sim list-plants` exposes registered plant declarations (id, backend,
  schedule/config keys, trust level) so capability discovery needs no Python
  import. `pe-sim list-runs` remains the package-discovery surface.

The wire contract and its non-goals are
`docs/44_run_interaction_contract_v1.md`. Remaining in Phase B: stable error
codes a client can branch on (item 4).

Acceptance evidence:

```text
pytest -q tests/test_run_events.py tests/test_cli_run_interaction.py `
  tests/test_plant_registry.py --basetemp .tmp/pytest-stage22
```

### Stage 23 error and rejection codes (0.5.7)

Roadmap Phase B item 4 (`docs/43_kernel_roadmap_v1.md`). This closes Phase B
and the T3 test: every failure a client can meet is now branchable without
matching prose.

- `pe_sim.error_codes` defines `pe-sim.rejection.v1`. Every CLI exit-2 refusal
  carries `schema`, `status`, `error_code`, and a human `error` string, while
  keeping the pre-0.5.7 `status` values.
- Six rejection codes: `invalid_request`, `unknown_plant`, `backend_mismatch`,
  `unknown_backend`, `formal_comparison_rejected`,
  `run_inspection_rejected`. `TypeError` from an adapter protocol violation is
  now `invalid_request`, not a traceback.
- `KERNEL_FAILURE_CODES` documents the categories the kernel and its built-in
  observation-validity / model-envelope checks can write to
  `manifest["failure"]["category"]`. It is documentation, not a whitelist:
  adapter/plugin categories pass through unchanged, and clients must treat
  unknown values as unverified. A raised check now takes its category from the
  first **blocking** finding rather than an earlier warning.
- `pe-sim.run-result.v1` adds `failure_category` (null on success), matching the
  manifest and the `run.finished` event.
- The evidence-reading views are schema-tagged: `inspect-run`
  (`pe-sim.run-inspection.v1`), `verify-attestation`
  (`pe-sim.attestation-verification.v1`), and `attest-run`
  (`pe-sim.attestation-write.v1`). The package and manifest remain the
  authority.

The contract is `docs/45_error_and_rejection_codes_v1.md`.

Acceptance evidence:

```text
pytest -q tests/test_error_codes.py tests/test_inspection.py `
  tests/test_attestation.py --basetemp .tmp/pytest-stage23
```

### Stage 24 time model and engine boundary (0.5.8)

Roadmap Phase A2 contract (`docs/43_kernel_roadmap_v1.md`). D1 is recorded as
**dual ownership, priced by evidence**: the kernel owns time for stepped
adapters and enforces constraints per window; an engine that owns its own time
axis is admitted only at `limited` evidence with post-hoc trace checks.

- `pe_sim.time_model` defines `stepped` / `whole_run`, the
  `pe-sim.trace-contract.v1` cadence/coverage contract, the structural
  `WholeRunPlant` protocol, and the post-hoc trace validator.
- `PlantRegistration.time_model` is declared, validated, and reported by
  `pe-sim list-plants`. The CLI checks the adapter shape for the declared
  route: six stepped methods or `trace_contract`/`run_whole`.
- The Runner records `manifest["time_model"]`, announces it in `run.started`,
  and caps every success level through `cap_evidence_level`. Formal comparison
  remains stepped-only.
- At 0.5.8 the execution route was declared but refused. **Stage 26 (0.5.10)
  implements it**; this entry records the contract half only.

What remained at 0.5.8: the executable whole-run route and the first real
whole-run engine (T1). Both landed in Stage 26. See
`docs/46_time_model_and_engine_boundary_v1.md`.

Acceptance evidence:

```text
pytest -q tests/test_time_model.py tests/test_time_model_integration.py `
  tests/test_plant_registry.py --basetemp .tmp/pytest-stage24
```

### Stage 25 compatibility policy and install extras (0.5.9)

Roadmap Phase C first increment (`docs/43_kernel_roadmap_v1.md`). D3 and D4
are recorded; the kernel can now state how to read a package produced by a
different schema version without touching it.

- `pe_sim.compatibility` classifies a manifest as `current`, `read_only`, or
  `incompatible`, names both versions, and carries
  `may_rewrite: false`. `inspect-run` reports the verdict in the top-level
  `compatibility` field; package integrity remains a separate `status`.
- `pe_sim.run_store` resolves discovery as explicit path ->
  `PE_SIM_RUNS_ROOT` -> `./runs`; `list-runs` and `visualization-serve` use it.
  Resolution never creates a directory.
- Install-time capability extras remain open: `docs/28` deliberately keeps
  PySpice out of dependencies/extras, and a new extra needs a release-policy
  decision. This stage does not change `environment-check`.

Still open: install-time extras and the upgrade gate (T4).

Acceptance evidence:

```text
pytest -q tests/test_compatibility.py tests/test_run_store.py `
  tests/test_inspection.py tests/test_run_catalog.py `
  --basetemp .tmp/pytest-stage25
```

### Stage 26 whole-run execution route (0.5.10)

Roadmap Phase A completion (`docs/43_kernel_roadmap_v1.md`, gate G-A, test T1).
The kernel now executes an engine that owns its own time axis, and a second
engine ships to prove the route is not a special case for one model.

- `pe_sim.whole_run_route` runs a `WholeRunPlant`: one `trace_contract()`, one
  `run_whole(request)`, post-hoc `validate_whole_run_result`, then the normal
  sample rows, plugin chains, and package layout. The manifest is assembled by
  the same `_build_run_manifest` helper the stepped route uses, so the two
  routes cannot drift into two definitions of evidence.
- `pe_sim.backends.whole_run_engine` is a project-owned deterministic linear
  state-space RK4 engine with an ideal averaged Buck reference plant. It owns
  its own discrete PI loop, because a whole-run engine cannot be driven window
  by window; the kernel-provided controller is recorded but not driven.
- Evidence: `time_model` is recorded, `route_limitations` lists
  `in_run_enforcement_unavailable` and `post_hoc_validation_only`,
  `enforcement` is `post_hoc_only`, and every level passes through the runtime
  cap. An exploratory whole-run run records `functional`; `limited` is its
  structural ceiling because formal comparison is refused outright.
- Refused before any filesystem change: `formal_comparison`,
  `checkpoint_interval_steps`, `resume_from`, `interrupt_after_steps`,
  `cancel_after_steps`, and `applicability: enforce`. A trace that violates its
  own contract is published as `RUN_FAILED` with
  `failure.category = trace_contract_violation` and the validator problems
  verbatim.
- Cancellation is polled before and after the engine call; both outcomes
  publish `INCOMPLETE` and record which observation point fired.

Acceptance evidence:

```text
pytest -q tests/test_whole_run_route.py tests/test_whole_run_engine.py `
  tests/test_time_model.py tests/test_time_model_integration.py `
  --basetemp .tmp/pytest-stage26
python -m pe_sim.cli run examples/whole_run_buck_step/config.json
python -m pe_sim.cli inspect-run runs/whole-run-buck-step --compact
```

Still open on this route: resampling, checkpoint/resume, in-run enforcement
callbacks, a reviewed identity pin for the engine (`not_pinned`), and workbench
export eligibility.

### Stage 27 upgrade gate and cross-release comparability (0.5.11)

Roadmap Phase C completion (`docs/43_kernel_roadmap_v1.md`, test T4). Reading a
package and comparing it are now two separate, explicit verdicts.

- `compare_runs` adds an additive `compatibility` block only when there is
  something to say: different schema (`not_assessed`), different producer
  `source_commit` (`not_assessed`), or an unreadable package (`unsupported`,
  outcome `unknown`). Packages from the same commit serialize exactly the
  payload they serialized before.
- `scripts/upgrade_check.ps1` is the gate: clean commit -> build wheel ->
  isolated venv -> pinned requirements -> installed `--version` matches ->
  `inspect-run` an older package -> `list-runs` its directory -> produce a
  package with the installed release -> `compare-runs` across releases ->
  require `cross_version_comparison: not_assessed` -> write
  `upgrade-evidence.json` (`pe-sim.upgrade-evidence.v1`).
- The fixture `tests/fixtures/older_packages/v0.5.9-buck` was produced by the
  v0.5.9 release itself (commit `ed45521…`), so the gate exercises a real
  older package rather than a hand-written manifest.
- `scripts/release_check.ps1` runs the gate as its `upgrade-gate` step and
  accepts `-OlderPackage` to point at another fixture.

Fixed along the way: `collect_git_provenance` treated a detached HEAD as
"provenance unavailable" because `git symbolic-ref -q` exits non-zero there.
Detached checkouts (common in CI and release worktrees) now record
`branch: "detached"` with a real commit.

Still open at 0.5.11: install-time extras (blocked by `docs/28`). The schema
migration registry landed with schema `0.2` in 0.6.0 (Stage 30).

Acceptance evidence:

```text
pytest -q tests/test_upgrade_gate.py tests/test_compatibility.py `
  tests/test_reproducibility_integrity.py tests/test_stage9_reproducibility.py `
  --basetemp .tmp/pytest-stage27
powershell -NoProfile -File scripts/upgrade_check.ps1 `
  -EvidenceDirectory .tmp/upgrade-evidence -PythonExecutable <python-3.12.7>
```

### Stage 28 lower-barrier user workflow and topology guard (0.5.12)

Roadmap Phase D completion (`docs/43_kernel_roadmap_v1.md`, gate G-D, test T6),
plus a structural guard for T2.

- `pe-sim demo [DIR]` reads a bundled experiment document from
  `pe_sim.templates`, writes `DIR/config.json` with a generated `run_id` and a
  run directory under `DIR/runs`, and runs it through the **same** path as
  `pe-sim run`. It prints `pe-sim.run-demo.v1` containing the normal
  `pe-sim.run-result.v1` payload plus read-only next steps
  (`inspect-run`, `visualization-serve`).
- Templates ship as package data and are kept byte-identical to `examples/` by
  `tests/test_packaging.py`, so an installed wheel supports the demo without a
  checkout.
- The demo refuses an unknown template with the branchable `invalid_request`
  code, and `--name` is not an allow-list: a new bundled template is a new data
  file, not a CLI edit.
- T2 guard: `tests/test_user_workflow.py` asserts that `plant_id` is not an
  enum in the experiment schema and that no kernel module (`cli.py`,
  `runtime.py`, `plant_registry.py`) holds a literal topology list. Extension by
  registration was already proven by `tests/test_plant_extension_gate.py`.
- Authority boundary: the consumer creates one configuration file it named and
  starts one exploratory run; every other step is read-only. It receives no
  Runner, Plant, Controller, plugin, or mutation authority.

Acceptance evidence:

```text
pytest -q tests/test_user_workflow.py tests/test_packaging.py `
  tests/test_cli_run_interaction.py tests/test_plant_extension_gate.py `
  --basetemp .tmp/pytest-stage28
pe-sim demo .tmp/demo-check
```

Still open: a browser UI (withdrawn), a remote API, authentication, and
multi-user operation.

### Stage 29 declared topology export and capability report (0.5.13)

Two remaining places where a consumer still had to know a module or a list are
now declarations.

- `PlantRegistration.topology_exporter` turns the workbench projection into
  registration data. `workbench_export` resolves the recorded `plant.id`
  through the registry and requires three declarations: `export_eligible`, a
  matching recorded class name, and an exporter that returns a
  `TopologyDescriptor`. The module no longer names a topology; it retries one
  lazy `load_builtin_plants()` so a library caller without the CLI composition
  root still works.
- `whole-run-buck` is now `export_eligible=True` and declares an exporter, so a
  whole-run package produces the same three read-only contracts. The trace
  reader accepts either recorded column spelling (`inductor_current` /
  `inductor_current_a`, `action` / `duty`) instead of encoding a plant list.
- `pe_sim.capabilities` (`pe-sim capabilities`) reports every optional
  capability, whether it is usable in the current process, and how to enable
  it. `--require ID` makes the command scriptable. It is explicitly not run
  evidence: it starts no simulator, writes no file, and its statuses are
  per-process.

Acceptance evidence:

```text
pytest -q tests/test_topology_export_registration.py tests/test_capabilities.py `
  tests/test_workbench_export.py tests/test_workbench_export_ngspice.py `
  tests/test_plant_registry.py tests/test_end_to_end_matrix.py `
  --basetemp .tmp/pytest-stage29
pe-sim capabilities --compact
```

Still open: an actual install-time extra (needs the `docs/28` policy decision).
The schema migration registry landed in Stage 30.

### Stage 30 evidence vocabulary v2 and schema 0.2 (0.6.0)

Engineering-governance pass: one token now means one thing.

- The evidence ladder (`diagnostic_only` / `functional` / `limited` /
  `comparable`) has exactly one definition, in `pe_sim.time_model`; Stage 12
  imports it instead of keeping a second copy.
- The qualification ladder is renamed to `assessed` / `partial` / `failed`. The
  old words (`full` / `limited` / `failed`) shared `limited` with the evidence
  ladder, so a reader could not tell which meaning applied.
- Stage 12's `mechanism` is now `scope: "mechanism"` on `functional` evidence;
  the classifier never emits a token outside the evidence ladder. The Stage 12
  schema becomes `stage12-evidence-classification-v2`.
- Manifest schema `0.1 -> 0.2`, with the first **migration registry**
  (`pe_sim.schema_registry`): `assess_package_compatibility` returns
  `read_only` plus a `migration` block for a registered path, and
  `incompatible` when no path exists. `inspect-run` reports the same mapping and
  presents the qualification level in the current vocabulary. Nothing rewrites a
  package.
- Shipped configs, templates, and the checked-in visualization fixture moved to
  `schema_version: "0.2"`; the v0.5.9 fixture stays `0.1` and is now the
  read-only example the upgrade gate exercises.

Acceptance evidence:

```text
pytest -q tests/test_evidence_vocabulary.py tests/test_schema_registry.py `
  tests/test_compatibility.py tests/test_inspection.py `
  tests/test_stage12_evidence.py tests/test_qualification_rules.py `
  --basetemp .tmp/pytest-stage30
```

Still open: an actual install-time extra (policy), and rendering the two ladders
as one "what may I claim" summary in a future UI.

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

### Stage 11 minimum visualization data core (current direction)

Stage 11 serves later visualization needs, but its immediate deliverable is
not a MATLAB replacement or a browser application.  The infrastructure first
provides three versioned, renderer-neutral, read-only data planes:

1. `TopologyDescriptor`: components, declared ports, nets, parameter
   summaries, and a stable descriptor fingerprint;
2. `TraceDataset`: explicitly registered selectable signals, units, sampling
   policy, timestamps, events, visibility, and trace provenance;
3. `OutputDataset`: versioned scalar, sequence, and tabular simulation-target
   outputs with field definitions, units, source/provenance, and data identity.

Only values intentionally published by a Plant, solver, Controller, runtime,
or reviewed adapter may enter a Trace or Output dataset.  The data contracts
must not inspect arbitrary Python state, disclose Plant truth by default, or
allow a renderer to alter topology connectivity, model parameters, Runner
state, or immutable run packages.  All derived data must retain a link to the
source run, Manifest hash, package hash, and source identity when these are
available.  New visualization artifacts belong outside the immutable run
package.

The immediate acceptance sequence is:

1. validate serialization, schema/version handling, units, finite values,
   explicit visibility, provenance, and fail-closed invalid-input behavior for
   all three data planes;
2. validate a read-only Buck/Boost reference adapter for each applicable data
   plane without changing default Runner behavior or published package bytes;
3. document the immutable-package/derived-artifact boundary and demonstrate
   that tampered or unknown provenance cannot be silently presented as trusted
   simulation evidence.

The three data-plane contracts, explicit publication rules, immutable-package
boundary, provenance handling, and focused minimum-core verification are
implemented. `docs/24_stage11_contracts_v1.md` (§1 and §7-§8) is the authoritative
scope and non-goal record for this phase. The browser workbench was withdrawn
because its rendered content was not usable; the headless exporter and local
read-only service remain as data interfaces.

After the minimum core is accepted, a versioned local read-only HTTP adapter
may consume only these three contracts. Live streaming, a general remote API, a
reusable UI/plugin runtime, or AI-assisted custom-symbol workflow remain future
work. Any future AI-produced symbol remains display-only and must bind to
already declared component ports; it may never create, remove, or rewire a net.

### Stage 11 headless surface (current)

The TypeScript/Vite reference workbench was removed on 2026-10-23 because its
rendered content was not usable. The renderer-neutral contracts, the offline
`export-workbench` adapter, and the loopback read-only service remain. They are
headless data interfaces and make no UI claim. A future UI must consume only
these contracts and must not gain Runner or Plant authority.

### Stage 11 minimum-release residual work

Formal Buck/Boost release-demo generation is now implemented by
`scripts/generate_formal_demos.py`. It is deliberately a separate local step:
the generator requires the exact verified toolchain and a clean Git commit,
creates only in a new/empty evidence directory, runs both L1 references in
`formal_comparison`, and revalidates published package hashes. The generated
packages remain functional deterministic Python reference evidence only; they
do not imply physical, Ngspice, thermal, hardware, or product validation.

The following items are release work, not a reason to expand the visualization
scope:

1. add an install-and-clean-checkout acceptance job that starts the local
   service and runs the independent consumer on the documented recommended
   Python versions;
2. publish an explicit support matrix for the constrained exporter/service
   and retain the hard rejection of unreviewed models until their own model
   review closes;
3. define release packaging/versioning for the headless contracts if they are
   later distributed as a separately versioned artifact.

Live traces, arbitrary browser plugin execution, topology editing, remote
multi-user hosting, and a MATLAB-scale IDE are deliberately not minimum-release
requirements.

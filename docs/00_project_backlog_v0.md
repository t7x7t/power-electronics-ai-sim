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
| 5 | Generic Runner | Done (minimal) | Reset, observation, controller call, timestamp checks, action projection, schedule, advancement, and artifact publication are implemented. Full lifecycle audit and richer timing are future work. |
| 6 | Safety and qualification checks | Partial | Non-finite values, action bounds, time rollback, and basic sample qualification fail closed. A broader extensible rule set is still needed. |
| 7 | Run artifacts and Manifest | Partial | Atomic artifacts, JSON/NPZ samples, events, metrics, logs, hashes, and Manifest exist. Complete environment/model provenance and final package hashes are incomplete. |
| 8 | Failure and recovery behavior | Partial | Failed runs retain artifacts and key failure paths are tested. Full state-transition matrix, interrupted-run recovery, and retention policy need implementation. |
| 9 | Reproducibility | Partial | Deterministic Fake runs and a tested environment record exist. Cross-machine and real Ngspice reproducibility is not verified. |
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

## Later backlog

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

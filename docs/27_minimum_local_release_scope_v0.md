# Minimum Local Release Scope and Support Matrix (v0)

**Status:** release-claim definition. A version tag may be created only after
the release gates in this document have evidence and a human reviewer records
the approval described below.

## Purpose and authority

This document defines the narrow public claim for the first shareable v0
release. It makes the project useful as a local, source-distributed simulation
infrastructure while avoiding an unsupported claim that it is a validated
power-converter, a general circuit simulator, or a MATLAB replacement.

It complements, rather than replaces, the detailed contracts and acceptance
documents. In particular, the L0 project guide remains the architectural
boundary; `docs/13_stage4_reference_plants_v0.md`,
`docs/23_stage15_l1_buck_boost_acceptance_v1.md`, and the Stage 11 documents
define detailed behavior; and `docs/09_license_and_git_baseline_scope_v0.md`
defines license and downstream responsibility. If a release note conflicts
with this document, this document limits the release claim until the conflict
is resolved.

## What "supported" means here

For this release, **supported** means that a project-owned feature has a
documented local entry point and is included in the stated local release
acceptance evidence. It does not create a service-level agreement, a promise
of maintainer response, a model-validity guarantee, or approval for a
downstream use.

**Verified reference** means the limited automated checks named in the
acceptance document passed for the stated project-owned implementation. It is
not a claim that the model represents a physical circuit accurately.

## Release support matrix

| Area | v0 release level | Scope that may be claimed | Important boundary |
| --- | --- | --- | --- |
| Python simulation infrastructure | Supported locally | The documented `run_experiment(...)`, compatible `Runner().run(...)`, CLI, lifecycle, audit, checkpoint/resume, artifact, provenance, safety, validity, and qualification boundaries. | It orchestrates adapters; it is not a general solver, real-time system, or engineering decision maker. |
| Project-owned Buck and Boost adapters | Verified L1 references | Deterministic ideal averaged continuous-conduction Buck and Boost reference Plants through the documented CLI/configuration and public Plant contract. | Only the Stage 15 equation, timing, boundary, recovery, and evidence checks are covered. Parameters are reproducible examples, not rated designs. |
| Fake/FakeLoad fixtures and `psfb-step` | Test fixture only | Contract, CLI, and Runner smoke verification. | `psfb-step` uses a fake backend. It is not a PSFB electrical model and must not be described as a PSFB reference result. |
| Controller examples | Reference integration only | The included simple PI-style reference controllers demonstrate the Controller boundary. | No controller quality, tuning suitability, stability margin, or deployment claim is made. |
| Stage 10/12 post-processing | Restricted local utility | Versioned metrics, comparison evidence, and bounded evidence classification for eligible packages. | It reports the limits of recorded evidence; it cannot validate physics, controller quality, or product conclusions. |
| Visualization contracts | Supported public data boundary | Versioned `TopologyDescriptor`, `TraceDataset`, and `OutputDataset` JSON documents with explicit publication and provenance rules. | A descriptor or rendering is not electrical truth and cannot alter a run, topology, or model. |
| Workbench exporter and local service | Constrained local adapter | Export or read-only HTTP display of integrity-checked, published project-owned L1 Buck/Boost packages through the three visualization contracts. | No simulation control, package writes, arbitrary model export, credentials, multi-user operation, or remote deployment is part of this release. The local service is intended to bind only to loopback. |
| `workbench/` | Source-distributed reference consumer | A minimal TypeScript/Vite workbench for read-only topology, waveform, and output display, built locally from source. | It is not a product UI, topology editor, plugin host, or separately versioned/distributed static application. |
| External visualization consumers | Supported extension boundary | A user or AI Agent may build a separate consumer against the three versioned JSON contracts or local service. | Consumers receive no Runner, Plant, Controller, mutation, reflection, or arbitrary browser-plugin authority. |

## Explicitly excluded from the v0 release claim

The release does not support, verify, or make an engineering claim about:

- PySpice or Ngspice execution, solver convergence, executable supervision, or
  cross-machine numerical equivalence;
- PSFB or LLC electrical models, switching/PWM waveforms, device models,
  parasitics, losses, magnetics, thermal behavior, rated power, or component
  selection;
- physical calibration, hardware sensors, HIL, electromagnetic behavior,
  safety, compliance, product readiness, or hardware approval;
- a general topology library, netlist authoring, topology editing, automatic
  wiring/layout, or AI-generated changes to circuit semantics;
- live trace delivery, a remote/multi-user visualization API, authentication,
  TLS deployment, or arbitrary browser-side plugin execution;
- built-in learning, online adaptation, optimizer/trainer behavior, or an
  automatic controller/model update workflow; and
- unreviewed material from `D:\PySpice` or any other third party. Such material
  is not part of this release and is not relicensed by the repository MIT
  license.

Adding any excluded item requires its own source/license review, interface and
acceptance evidence, and an explicit revision of this matrix. It must not be
silently inferred from a similarly named example or tool.

## Run and evidence terminology

The following terms are deliberately narrower than everyday usage:

| Term | Meaning | Does not mean |
| --- | --- | --- |
| `RUN_OK` | The Runner completed normal execution before post-run qualification. | The samples are qualified, comparable, physically valid, or safe for hardware use. |
| `QUALIFIED` | The individual run completed and passed its configured automatic safety, validity, and qualification checks. | A clean source tree, a formally comparable baseline, package integrity beyond its own publication checks, physical correctness, hardware safety, or release approval. |
| `PUBLISHED` | The artifact writer completed its package publication marker after its transaction/integrity checks. | Human review, a release tag, or permission to make an engineering conclusion. |
| `formal_comparison` | A run mode that refuses an unknown or dirty Git worktree before creating the run directory. | Physical-model validation or cross-machine reproducibility. |
| Stage 12 `functional` | A restricted classification for a qualifying project-owned reference run with the required provenance. | Physical performance, research safety, hardware readiness, product compliance, or controller superiority. |
| Release acceptance | The local release gate has passed for a specific clean commit and a human has reviewed the evidence. | Approval of a downstream model, circuit, experiment, product, or engineering decision. |

An exploratory dirty run may still be useful for debugging and may become
`QUALIFIED` under its configured checks. Its recorded dirty provenance prevents
it from being silently used as a formal comparison or release demonstration.

## Minimum release gates

A tagged minimum local release requires all of the following for the exact
release commit:

1. this scope matrix and the supported-version policy identify the release
   surface and the verified Python/Node toolchain;
2. a local release check runs the full required Python checks, Node dependency
   installation/build, local service/consumer verification, and emits a
   machine-readable evidence record;
3. clean-worktree, formal-mode Buck and Boost demonstration packages are
   generated from the release commit, checked for integrity, and retained as
   release evidence outside normal transient output;
4. browser-level local interaction checks cover the workbench consuming the
   local service; and
5. a human reviewer examines the evidence, records the exact commit, versions,
   test result, package hashes, date, scope, and non-claims, then creates the
   version tag.

The ordered implementation of these gates is intentional: version/toolchain
policy and the automated local gate must exist before a clean demo becomes
release evidence; a version tag is the final human action, not a test result.

The reproducible generation entry point for item 3 is
`scripts/generate_formal_demos.py`. It requires the exact toolchain policy,
an unknown-free clean commit, an empty output directory outside the source
checkout, and explicit
`formal_comparison` execution for both project-owned L1 references. Its
generated evidence is limited to functional deterministic Python reference
behavior and must not be described as physical, thermal, switching, Ngspice,
hardware, or product validation.

## Responsibility boundary

The repository provides project-owned reference infrastructure and tools under
the MIT License. Each downstream user is solely responsible for reviewing the
code, dependencies, models, parameters, results, safety, compliance, and
engineering decisions for its own purpose. A release, a `QUALIFIED` run, a
`PUBLISHED` package, or a passing release check does not transfer that
responsibility to the maintainer and does not create an obligation of support
or response. See `README.md` and
`docs/09_license_and_git_baseline_scope_v0.md` for the full license and
responsibility statement.

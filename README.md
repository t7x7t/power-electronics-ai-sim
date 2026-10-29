# Power Electronics AI Simulation

The project-owned code in this repository is released under the MIT License;
see [LICENSE](LICENSE). This notice does not relicense unreviewed material
from `D:\PySpice` or any other third-party source. The approved v0 baseline
scope and migration exclusion are documented in
`docs/09_license_and_git_baseline_scope_v0.md`.

## Responsibility and support boundary

This repository provides project-owned infrastructure, reference examples, and
development tools. It is provided without a promise that a model, parameter,
controller, experiment, or result is suitable for a user's purpose. Anyone
obtaining or using the project from GitHub is responsible for reviewing the
code, dependencies, models, parameters, results, safety, compliance, and
engineering decisions for their own use. The maintainers do not provide a
result guarantee, hardware approval, or an obligation to provide technical
support or response. The MIT License and its warranty/liability terms apply.
Unreviewed material from `D:\PySpice` and other third-party sources is not
relicensed by this repository.

## Minimum local release scope

The first shareable release is a deliberately narrow, local, source-distributed
simulation-infrastructure slice. Its supported Python orchestration, the
PySpice-constructed and ngspice-executed Buck reference, deterministic averaged
test fixtures, read-only visualization contracts, the constrained headless
local visualization service, exclusions, run-status meanings, and release gates
are defined in
[docs/27_minimum_local_release_scope_v0.md](docs/27_minimum_local_release_scope_v0.md).
In particular, `QUALIFIED` is only an automated per-run qualification state; it
does not establish physical correctness, hardware safety, product readiness, or
maintainer approval.
The admitted version ranges, exact locally verified Python toolchain, and
clean-install policy are defined separately in
[docs/28_release_toolchain_policy_v0.md](docs/28_release_toolchain_policy_v0.md).
How a release is identified, recorded, and approved — the distribution
inventory, the `pe-sim.release-record.v1` approval record, and the
`check-identity` drift guards — is defined in
[docs/53_release_governance_v1.md](docs/53_release_governance_v1.md). Nothing in
that procedure signs an artifact or tag, and CI never approves or publishes.

## Capability boundary: what this kernel does not solve

The kernel can drive a **stepped, kernel-closed** loop: one solve per control
window, one scalar actuator (`duty`), and constraints evaluated at every window
boundary so they can block a run in flight. That is invariant I1. Loop closure,
however, is a declared route fact rather than a universal shape. Before choosing
this tool, check the current consequences:

- **Duty is constant inside each solved segment for kernel-driven adapters.**
  `ngspice-buck` runs one switching-level `.tran` per window and carries state
  through `IC=`; the waveform inside a window is sampled, not integrated across
  the run. `ngspice-buck` is *not* an averaged model — the averaged ones are
  `buck` / `boost` (pure Python RK4) and `whole-run-buck`.
- **The compensator can close in the kernel or inside the model.** A Type III
  compensator is a kernel `Controller` plugin; `model-control-buck`,
  `whole-run-switching-buck`, and `peak-current-buck` instead own a model-side PI
  or comparator loop and are capped at `limited`
  (`actuator_control_in_model`). These are model-side loops, not device-level
  SPICE netlist compensators.
- **The carrier phase is carried across solve segments.** Since 0.13.0,
  `control_period_s` does **not** need to be an integer multiple of the
  switching period; a segment may start at any carrier phase.
- **Schedules change at declared change points.** Every stepped route ends a
  solve segment at the next declared change and resolves the input there
  (`time_s`, `vref`, `vin_v`, `input_voltage_v`, `load_ohm`,
  `load_resistance_ohm`). `event_granularity=window` is a controller statement:
  observation and actuation still happen only at control-window boundaries, and
  the registration declares no cost for in-window events.
- **A switching-level whole-run trace exists, but only post-hoc.**
  `whole-run-switching-buck` returns one continuous trace at a declared cadence,
  capped at `limited` (`post_hoc_validation_only` / `in_run_enforcement_unavailable`);
  it is not real-time interception evidence.

If a task needs a true device-level in-netlist compensator, device/thermal
models, or in-run interception across a continuous run, the honest answer is to
drive ngspice directly — and to accept losing constraint enforcement,
qualification, evidence levels, the auditable package, and L1 attestation.
Supported ways to stay inside the framework are writing a `Controller`
(Type III included), a model-side adapter (T1: zero kernel edits), or a
`PlantRegistration` whose declared route carries its own evidence cap.

Full statement, including the three-backend comparison and the bypass criteria:
[docs/58_capability_boundary_v1.md](docs/58_capability_boundary_v1.md).

Scenario-level coverage — which operating conditions are covered, which are
expressible but unimplemented, which are registered kernel gaps, and which are
declared out of scope — is tracked in
[docs/60_simulation_coverage_matrix_v1.md](docs/60_simulation_coverage_matrix_v1.md).
That ledger is machine-checked: a row may not claim `covered` without naming the
test that proves it.

For maintainer handover — goals, stage history, evidence caps, known debt, and
the constraints that must survive the transfer — see
[docs/62_project_handover_v1.md](docs/62_project_handover_v1.md).

## Quick start (installed package)

A user who does not read source can run, view, and verify with three documented
commands. `pe-sim demo` writes a bundled reference experiment, runs it through
the same path as `pe-sim run`, and prints the read-only next steps:

```powershell
pe-sim demo .\my-demo
pe-sim inspect-run .\my-demo\runs\<run_id> --compact
pe-sim visualization-serve --runs-root .\my-demo\runs
```

`pe-sim capabilities` reports every optional capability (PySpice netlist builder,
external ngspice, the visualization service, Git anchoring), whether it is
usable in the current process, and how to enable it. `--require <id>` makes it a
scriptable preflight check. See
[docs/49_optional_capabilities_v1.md](docs/49_optional_capabilities_v1.md).

Before writing a configuration, `pe-sim describe <plant_id>` prints that
plant's declared `plant_config` surface (types, units, bounds, defaults), and
`pe-sim validate <config>` checks a document against those declarations, the
experiment schema, and the plant's schedule vocabulary without building a
plant, starting a solver, or creating a run directory. See
[docs/63_agent_facing_spec_validation_v1.md](docs/63_agent_facing_spec_validation_v1.md).

The other half of authoring a circuit is its structure, and it has the same
shape, plus an execution check:

```powershell
pe-sim describe-devices                       # what may I draw?
pe-sim validate-topology .\topology.json      # is this a well-formed circuit?
pe-sim compile-topology .\topology.json --duty 0.5 --output circuit.cir
pe-sim simulate-topology .\topology.json --duty 0.5 --duration 1e-3
```

If the circuit already exists as a SPICE netlist, `pe-sim import-spice` translates
the part of SPICE this vocabulary can express into the same descriptor document,
so the rest of the loop is unchanged:

```powershell
pe-sim import-spice .\circuit.cir --topology-id from_netlist --output .\topology.json
```

The importer drops directives that describe *how* a netlist is run (including a
whole `.control` block) and lists every drop; it refuses `.include`, `.subckt`
and parameter expressions, because those would make the circuit depend on text
it never read. It does not guess: a carrier's pulse width is a duty ratio, which
this project treats as a compile-time command, so it is reported as an observed
value rather than imported.

`describe-devices` prints the closed device vocabulary (ordered ports, allowed
net kinds, parameters with units, and whether a kind has an element card at
all). `validate-topology` reports pointer-addressed issues for unknown ports,
unknown parameters, dangling nets, a disconnected graph, a missing or duplicated
reference net, an undriven or multiply driven control net, and loops of ideal
voltage sources. `compile-topology` renders a deterministic netlist whose
SHA-256 is a stable identity for the compiled circuit; it refuses anything it
cannot render exactly instead of approximating. None of the first three builds
a plant, imports an adapter, starts a solver, or creates a run directory.
`simulate-topology` is the execution check: it solves one constant-duty window
through the same ngspice process boundary and reports bounded signal summaries
(and, with `--output-dir`, the full samples). Its report also carries a `metrics`
block measured from those samples: input and load power, the stored-energy rate,
the loss between them, efficiency, and the duty the circuit actually saw. The
measurement names no converter, so it answers for any circuit the vocabulary can
express; efficiency is withheld when the window is still a transient. See
[docs/67_waveform_metrics_v1.md](docs/67_waveform_metrics_v1.md). A compiled netlist is a
well-formed circuit, not a reviewed model: a topology an agent authors can
reach at most `limited` evidence. See
[docs/64_circuit_topology_authoring_v1.md](docs/64_circuit_topology_authoring_v1.md)
and [docs/65_circuit_execution_v1.md](docs/65_circuit_execution_v1.md); the
netlist importer is described in
[docs/66_spice_netlist_import_v1.md](docs/66_spice_netlist_import_v1.md).
Three complete descriptors live in
[examples/topologies](examples/topologies) as reference designs: an
asynchronous buck, a synchronous buck that declares its complementary gate
relation, an inverting buck-boost whose negative rail is expressed by
binding the load and the capacitor to the reference node, and a three-phase
interleaved buck whose one duty drives three evenly spread gates. They are
data, not plants, and a test validates and compiles each one.
`circuit-topology` is also a registered Plant, so `pe-sim run` accepts an
experiment document whose `plant_config.topology` is the descriptor and closes
the loop with the kernel controller.

The demo is an exploratory run of a project-owned averaged reference model. It
records `evidence_level: functional`; it is not a physical, formal-comparison,
or hardware claim. The consumer creates one configuration file and otherwise
only reads. See
[docs/48_user_workflow_v1.md](docs/48_user_workflow_v1.md).

## Generic Runner entry point

The low-friction Python entry point uses the standard audited runner defaults:

```python
from pe_sim import run_experiment

result = run_experiment(spec, plant, controller)
```

Advanced callers can compose `RunOptions` policies for timing, recovery, or
custom checks. The legacy `Runner().run(...)` API remains compatible. The
Runner orchestrates ordering, audit, recovery, and publication; circuit models
and control algorithms remain in Plant/Controller adapters.

这是一个面向 AI Agent 协作的电力电子仿真工程骨架。根目录的
`general_simulation_infrastructure_and_closed_loop_phases_20260902.md` 是
思路性总纲；本目录下的规范文件才是 v0 实现阶段的可执行依据。

## 文档层级

| 层级 | 文件 | 用途 |
|---|---|---|
| L0 | `general_simulation_infrastructure_and_closed_loop_phases_20260902.md` | 目标、边界和架构原则 |
| L1 | `docs/01_architecture_contracts_v0.md` | Plant、Controller、Runner 和时间因果契约 |
| L1 | `docs/02_run_artifacts_and_state_machine_v0.md` | 运行产物、状态机、Hash 和证据规则 |
| L1 | `docs/03_agent_operating_policy_v0.md` | Agent 权限、工作流、停止条件和人工审批 |
| L1 | `docs/04_mvp_implementation_plan_v0.md` | MVP 范围、任务、验收和迁移顺序 |
| L1 | `docs/43_kernel_roadmap_v1.md` | 内核阶段路线图：顺序、阶段门、不变量与待决事项 |
| L1 | `docs/05_current_workspace_migration_map_v0.md` | `D:\PySpice` 到本项目的迁移边界 |
| 机器规范 | `schemas/manifest.schema.json` | Manifest 的最小 JSON Schema |
| 机器规范 | `schemas/experiment.schema.json` | 实验配置的最小 JSON Schema |
| Agent 入口 | `agent/AGENTS.md` | 每次 Agent 任务必须遵循的规则 |

## v0 的明确范围

v0 以 **Buck 参考电路（PySpice 构建 + ngspice 进程执行）**、统一 Runner、
Manifest、受限后处理和只读可视化数据边界作为最小可发布切片。理想平均
Buck/Boost 模型仅保留为确定性测试夹具，不再作为参考电路。fake backend 仅用于
Runner/CLI 契约测试，不是任何 PSFB 电路模型。完整支持矩阵和明确排除项以
`docs/27_minimum_local_release_scope_v0.md` 为准；器件、电热、PSFB/LLC、学习和
硬件结论均不因该切片而被隐式纳入。

## 最小运行切片

在全新 Python 环境中安装当前骨架并检查 CLI：

```powershell
python -m pip install -e .
pe-sim --help
pe-sim run examples/buck_pi_step/config.json --backend buck
```

该命令使用内置的理想平均 Buck 测试夹具，不代表真实 PySpice/Ngspice 物理结果；
参考电路请使用 `examples/ngspice_buck_step/`。运行目录包含配置快照、事件、样本、
资格/安全记录和带 SHA-256 的 `manifest.json`。PSFB/LLC 不在当前项目中。

## Runner entry points

For Python users, the low-friction entry point is:

```python
from pe_sim import run_experiment

result = run_experiment(spec, plant, controller)
```

For configuration-driven use, select the reviewed Buck reference backend through
the CLI. PySpice 1.5 is installed separately and is used only to construct the
circuit; execution stays on the reviewed ngspice process adapter:

```powershell
# Python 3.10-3.12 verified pin set; use a dedicated environment
pip install -r requirements-pyspice-tested.txt
pe-sim run examples/ngspice_buck_step/config.json --backend ngspice-buck --model-builder pyspice
```

On Python 3.13, use an isolated environment and install `PySpice==1.5` without
the 1.26.4 NumPy pin, then run `pip check` there. The verified pin set is not
published as a package extra: `docs/28` keeps PySpice out of dependencies and
extras, so the requirements file remains the only supported installation path. The ideal averaged `buck_pi_step` and `boost_pi_step` examples are deterministic
test fixtures, not reference-circuit results. They remain available for runtime
and comparison tests:

```powershell
pe-sim run examples/buck_pi_step/config.json --backend buck
pe-sim run examples/boost_pi_step/config.json --backend boost
```

An engine that owns its own time axis uses the whole-run route instead of the
window-by-window contract. The shipped example is a project-owned deterministic
state-space engine with an ideal averaged Buck reference. It publishes a normal
package and declares the limits of its route:

```powershell
pe-sim run examples/whole_run_buck_step/config.json
pe-sim inspect-run runs/whole-run-buck-step --compact
```

The manifest records `time_model: "whole_run"`, `enforcement: "post_hoc_only"`,
and `route_limitations: [in_run_enforcement_unavailable, post_hoc_validation_only]`.
Formal comparison is refused with `formal_comparison_rejected`, and the
checkpoint/resume/interruption options are refused, because the kernel does not
own the engine's steps. The declared controller identity is recorded but not
driven: the engine owns its control loop. See
[docs/46_time_model_and_engine_boundary_v1.md](docs/46_time_model_and_engine_boundary_v1.md).

### CLI configuration contract

- `--backend` is optional only when it matches `plant_id`. A mismatch, an
  unknown `plant_id`, or an unknown backend is rejected before a run directory
  is created. The CLI output records the selected `backend`, `plant_id`, and
  `plant_class`.
- `controller_id` supports `pi` and `pid`. Both use the same reference
  controller with an explicit law:
  `u = kp*e + sum(ki*e*dt) + kd*de/dt + kff_v*vref`, with gains in duty per
  volt (and per volt-second for `ki`, per volt/second for `kd`). `pid` adds
  `kd`, `kd_filter_tau_s`, and `kff_v`; those keys are rejected under `pi`
  rather than silently ignored. A zero gain removes its term from the recorded
  identity, so `pi` runs stay comparable with earlier packages.
- `input_schedule` keys are backend-specific: `fake` accepts `time_s`/`vref`;
  `fake-load` additionally accepts `load`; `buck`/`boost`/`ngspice-buck`
  accept `vin_v`/`input_voltage_v` and `load_ohm`/`load_resistance_ohm`.
  Unknown or backend-inapplicable keys are rejected instead of being silently
  ignored.
- The ideal averaged Buck/Boost examples are test fixtures. The reviewed Buck
  reference uses PySpice construction plus ngspice execution.

## Buck reference: PySpice construction + ngspice execution

The reviewed Buck reference is an ideal switching asynchronous Buck circuit.
PySpice builds the circuit through its object API; the generated netlist is
executed by a separately installed ngspice executable through the reviewed
process boundary. Both external components are optional installations and are
never bundled:

```powershell
# Python 3.10-3.12 verified pin set; use a dedicated environment
pip install -r requirements-pyspice-tested.txt
pe-sim backend doctor --name ngspice
pe-sim run examples/ngspice_buck_step/config.json --backend ngspice-buck --model-builder pyspice
```

The pin set keeps NumPy on the project-verified 1.26.4 line while selecting
compatible `scipy`/`matplotlib`/`contourpy` releases, so `pip check` stays
clean. The native netlist builder remains available for offline and CI tests, but it is
not the reference construction path. The definition, failure semantics, and
acceptance matrix are in `docs/31`; the PySpice construction boundary is in
`docs/34`.

### If ngspice is not installed

```powershell
pe-sim backend list
pe-sim backend setup --name ngspice
```

`setup` detects the absence, prints platform-specific installation recipes
(conda, official archive, apt, brew) and the exact follow-up command. It does
not install anything and writes no configuration.

### If ngspice is already installed

```powershell
$env:PE_SIM_NGSPICE = "D:\tools\ngspice-44.2\Spice64\bin\ngspice_con.exe"
pe-sim backend doctor --name ngspice
pe-sim run examples/ngspice_buck_step/config.json --backend ngspice-buck --model-builder pyspice
```

To persist the path outside the repository:

```powershell
pe-sim backend setup --name ngspice --executable <path> --write-config
```

After that, `pe-sim run ... --backend ngspice-buck` needs no path argument.
The full onboarding flow is in
[docs/35_backend_onboarding_v0.md](docs/35_backend_onboarding_v0.md).

The repository never bundles a simulator. Resolution order is explicit
`--ngspice`, `PE_SIM_NGSPICE`, a user-level `backends.toml` outside the
checkout, then `PATH`. Use `pe-sim backend config-path`, `pe-sim backend
template`, and `pe-sim backend scan --root <dir>` to prepare the user-level
configuration without committing machine-local paths. The extension contract
for additional engines is in
[docs/33_backend_plugin_contract_v1.md](docs/33_backend_plugin_contract_v1.md).

The adapter generates a deterministic netlist, solves one zero-order-hold
control window per invocation, records the executable version, SHA-256, and
solver settings, and classifies timeout, process, and convergence failures.
Real execution is opt-in; ordinary CI does not require ngspice. The supported
surface, failure semantics, and acceptance matrix are defined in
[docs/31_ngspice_backend_scope_v1.md](docs/31_ngspice_backend_scope_v1.md).
The environment lock and installation policy are in
[docs/32_ngspice_environment_lock_v0.md](docs/32_ngspice_environment_lock_v0.md).

### PySpice circuit construction (reference path)

PySpice 1.5 builds the reference circuit through its object API, but its native
execution backends are not compatible with the locally verified ngspice 41.
The reference path therefore uses PySpice only for circuit construction and
keeps execution on the reviewed ngspice process adapter:

```powershell
# dedicated environment; Python 3.10-3.12 verified pin set
pip install -r requirements-pyspice-tested.txt
pip install -e .
python -m pip check
pe-sim run examples/ngspice_buck_step/config.json --backend ngspice-buck --model-builder pyspice
```

The builder records the PySpice version and explicitly marks that native
PySpice execution is not used. PySpice is GPLv3 and is never a core
dependency; the compatibility findings and license boundary are in
[docs/34_pyspice_optional_backend_v0.md](docs/34_pyspice_optional_backend_v0.md).

PySpice is not a dependency: ADR
[docs/adr/0002-ngspice-process-boundary.md](docs/adr/0002-ngspice-process-boundary.md)
records the license and maintenance reasons for using a process boundary.

The ngspice result is reference-model evidence only. It does not establish
device, thermal, hardware, product, cross-machine, or PSFB/LLC validity.

Both paths use the standard deterministic timing, audit, safety, qualification,
and artifact behavior. Advanced users can pass `RunOptions` with narrowly
scoped `TimingPolicy`, `RecoveryPolicy`, or `AuditPolicy` settings. Policy
objects are optional and are not required for ordinary experiments.

Existing code can migrate incrementally. The legacy form remains supported:

```python
from pe_sim import Runner

result = Runner().run(spec, plant, controller)
```

Move to `run_experiment` when a simpler default is preferred; keep the legacy
form when existing code relies on its keyword arguments. Do not add circuit
equations, controller algorithms, plotting, or engineering conclusions to the
Runner; those belong in adapters or later analysis layers.

## Exporting results

A published run can be projected into the three read-only visualization
contracts without giving the consumer access to simulation objects:

```powershell
pe-sim export-workbench runs/ngspice-buck-reference derived/ngspice-buck-reference
python examples/visualization_consumer/verify_contracts.py --directory derived/ngspice-buck-reference
```

The destination must be outside the immutable run directory. The exported
`topology.json`, `trace.json`, and `outputs.json` carry the run identity and
package hashes. The external consumer example uses only the Python standard
library. The browser workbench is temporarily withdrawn; this export and the
loopback read-only service are headless data interfaces, not a UI claim. The
contract and compatibility rules are in
[docs/26_stage11_consumer_and_service_v1.md](docs/26_stage11_consumer_and_service_v1.md).

## Verifying a run

Two read-only commands support circuit and evidence review:

```powershell
# Render the reviewed netlist without starting ngspice.
pe-sim preview-netlist examples/ngspice_buck_step/config.json --duty 0.5

# Re-verify a published package and summarize its samples and identity.
pe-sim inspect-run runs/ngspice-buck-reference

# See the recorded parameter set and the claim conclusion together.
pe-sim parameter-view runs/ngspice-buck-reference --compact
```

`parameter-view` projects `pe-sim.parameter-view.v1` from the verified
manifest: the plant, controller, timebase, action-policy, and solver groups with
their recorded values, the units and sources that a recorded parameter-provenance
finding names, the declared action bounds, the parameter-set digest, and the
embedded claim conclusion. It never executes a model, and `range` stays `null`
because no parameter range is declared. `pe-sim confirm-parameters` records a
detached `pe-sim.parameter-confirmation.v1` review outside the package, and
`pe-sim verify-parameter-confirmation` reports `current`, `stale`,
`mismatched_run`, `invalid`, or `unknown`. The contract is
[docs/54_parameter_view_and_confirmation_v1.md](docs/54_parameter_view_and_confirmation_v1.md).

`inspect-run` recomputes the Manifest digest, package digest, artifact index,
publication marker, and sample monotonicity. It reports the backend, plant
identity, `implementation.review.status`, first/last window digests, and any
recorded failure evidence. A package can be intact while its run failed:
`status` reports package integrity, while `run_status` and `run_ok` report the
execution outcome separately. It also reports a read-only `credibility`
assessment, the `qualification_level` (`assessed`, `partial`, or `failed`), the
recorded `evidence_level` (`diagnostic_only`, `functional`, `limited`, or
`comparable`), and a `compatibility` verdict (`current`, `read_only`, or
`incompatible`). Compatibility is a reading policy, not an integrity claim: an
older package can still verify while `read_only` says its cross-version
comparability is not assessed, and `may_rewrite` is always false.

To avoid combining those fields by hand, the summary includes one `claim`
object (`pe-sim.claim.v1`) answering "what may I claim?": a conclusion of
`no_claim`, `exploratory`, `bounded`, or `comparison_eligible`, every input
that forced a weaker conclusion, and explicit may-claim / may-not-claim
statements. The conclusion is reader-side and never stronger than any recorded
input. Its contract is
[docs/51_claim_conclusion_v1.md](docs/51_claim_conclusion_v1.md). The
inspection output is tagged `pe-sim.run-inspection.v1`; the compatibility
policy is in
[docs/47_compatibility_policy_v1.md](docs/47_compatibility_policy_v1.md).

To discover what exists before verifying anything:

```powershell
pe-sim list-runs runs --plant buck --published-only --limit 20
```

`list-runs` reads recorded metadata only and says so in `limitations`; it never
verifies a package and never writes. Use `inspect-run` for integrity and
`verify-attestation` for authenticity. Without a positional root, discovery uses
`PE_SIM_RUNS_ROOT` and then `./runs`; the same order applies to
`visualization-serve`.

To see what can be run before choosing a config:

```powershell
pe-sim list-plants
```

`list-plants` prints the registered plant declarations - id, backend, accepted
schedule and config keys, trust level, export eligibility - without importing
the plant implementations.

## Watching or cancelling a run

A client that did not write the simulation code can follow a run and stop it
through documented contracts only:

```powershell
# One JSON object per line on stderr: run.started / run.progress / run.finished.
pe-sim run examples/buck_pi_step/config.json --backend buck --progress

# Stop the run by creating this path from another shell or an agent.
pe-sim run examples/buck_pi_step/config.json --backend buck --cancel-file .cancel-buck
```

Progress is rate-limited observability, not evidence: it is never written into
the package, and a failing consumer cannot change the run. Cancellation is
polled once per control window, so it cannot interrupt an adapter mid-window.
A cancelled run is published as `INCOMPLETE` with a checkpoint and failure
category `cancelled`, which is distinct from an operator interrupt. The wire
contract is
[docs/44_run_interaction_contract_v1.md](docs/44_run_interaction_contract_v1.md).

A refused request is a `pe-sim.rejection.v1` object with a stable
`error_code` (`invalid_request`, `unknown_plant`, `backend_mismatch`,
`unknown_backend`, `formal_comparison_rejected`, `run_inspection_rejected`),
and a failed run exposes `failure_category` in the terminal result. Branch on
those codes, never on the `error` text. The vocabulary is in
[docs/45_error_and_rejection_codes_v1.md](docs/45_error_and_rejection_codes_v1.md);
every `pe-sim.<name>.vN` envelope tag the project emits is listed with its
producer in
[docs/68_envelope_registry_v1.md](docs/68_envelope_registry_v1.md), so a
client never has to guess which parser an envelope needs.

What the substrate deliberately does **not** express is written down too:
transformer/magnetic coupling, switch reverse recovery, nonlinear magnetics,
thermal networks, sinusoidal sources, three-phase systems, multi-leg
independent control, PSS/sweep analysis, built-in current-mode controllers, and
any UI. Each entry names the refusal an author will actually see and the
nearest honest substitute. See
[docs/69_scope_boundaries_v1.md](docs/69_scope_boundaries_v1.md).

## Applicability and authenticity

Runs now record what the model is declared to apply to, not only that the
numbers were finite:

```powershell
# Exploratory runs record envelope findings; formal comparison enforces them.
pe-sim run examples/buck_pi_step/config.json --backend buck --applicability default

# Keep only the baseline structural qualification checks.
pe-sim run examples/buck_pi_step/config.json --backend buck --no-standard-qualification
```

The `buck`/`boost` averaged fixtures declare equation domains, an integration
stability limit, and an explicit `uncheckable_rules` list (for example CCM
cannot be checked without a switching frequency). The `ngspice-buck` adapter
declares switching-period, sample-resolution, and pulse-width limits. Parameter
sources are recorded separately; an `assumption` parameter is visible as
`unreviewed` rather than silently treated as calibrated.

Authenticity Level 1 records the package identity outside the package and
anchors it in Git history:

```powershell
pe-sim attest-run runs/ngspice-buck-reference --repo .
# commit attestations/ with review, then:
pe-sim verify-attestation runs/ngspice-buck-reference `
  --attestation attestations/ngspice-buck-reference.attestation.json --repo .
```

`L1_anchored` means the exact attestation and anchor record were found in a
reachable Git commit. It proves visibility of the package identity in history;
it does not prove who produced the package, that a reviewer approved it, or
that a local administrator could not rewrite history. Signatures (Level 2) are
append-only and do not change the Level 1 payload. See
[docs/40_validity_and_authenticity_v1.md](docs/40_validity_and_authenticity_v1.md).

The reviewed solve path is pinned in `src/pe_sim/reviewed_identities.json` and
checked by the test suite. Any change to the circuit builder, process boundary,
solver settings, or waveform parser changes the digest and requires an explicit
review update. The pin covers `ngspice-buck` and, since 0.5.1, the native
`buck`/`boost` reference models; `runtime.py` is deliberately outside it and is
declared as an `implementation.runner_loop` rule. The pin is a review tripwire,
not a signature. The review
findings and residual risks are in
[docs/38_kernel_review_v0.md](docs/38_kernel_review_v0.md).
The applicability/authenticity design, acceptance matrix, and non-claims are in
[docs/40_validity_and_authenticity_v1.md](docs/40_validity_and_authenticity_v1.md).

## Kernel compatibility surface

Three properties are machine checked rather than left to convention:

- **Public API tiers.** `src/pe_sim/public_api.py` splits the top-level surface
  into `stable` (81 names an adapter author or evidence verifier depends on) and
  `experimental` (202 names reachable but not promised).
  `tests/fixtures/public_api.json` freezes the stable list, so adding or
  removing a stable name fails a test until the baseline is updated in the same
  commit.
- **Reference-plant review pins.** `buck` and `boost` now use the same reviewed
  identity mechanism as `ngspice-buck`, so the default example path is reviewed
  with the same strength as the simulator path.
- **Tolerance policy.** Comparison no longer forces one scalar on every signal:

  ```powershell
  pe-sim compare-runs runs/a runs/b --relative-tolerance 1e-4
  pe-sim compare-runs runs/a runs/b --signal-tolerance output_voltage_v:1e-6:1e-4
  ```

  The report records the policy and its digest, and defaults are unchanged:
  with no policy the historical absolute-only behaviour is reproduced exactly.
  Sample grid alignment is still positional and remains open.
- **One vocabulary per question.** The evidence ladder
  (`diagnostic_only` < `functional` < `limited` < `comparable`) and the
  qualification ladder (`assessed` / `partial` / `failed`) share no token.
  Manifest schema `0.2` records that separation; a schema `0.1` package is read
  through a registered reader-side mapping and is never rewritten.
- **Cross-release comparability.** `compare-runs` says whether two packages may
  be compared at all. Same schema and same source commit keeps the historical
  payload; a different commit or schema adds
  `compatibility.cross_version_comparison: not_assessed`, and an unreadable
  package adds `unsupported` with an `unknown` outcome.
  `scripts/upgrade_check.ps1` installs the checkout into an isolated
  environment and exercises this against a package produced by v0.5.9.

See
[docs/41_kernel_compatibility_surface_v1.md](docs/41_kernel_compatibility_surface_v1.md).

## How adapters and agents plug in

The kernel does not care which engine produced a window: a physical simulator
plugs in by implementing `PlantAdapter` (`capabilities`, `reset`, `observe`,
`advance`, `snapshot`, `restore`) and is passed to `Runner.run` or
`run_experiment`. Agents are clients of the CLI/API, not kernel plug-ins.

Since 0.5.4 the CLI also does not keep a plant list. A plant is a
`PlantRegistration` and comes from one of two places:

- the project-owned `pe_sim.builtin_plants` composition root, loaded by the CLI
  at start-up;
- the `pe_sim.plants` entry-point group, for a distribution that ships its own
  plant without touching this repository.

Entry-point registrations are forced to `trust_level = "third-party"` and
`export_eligible = False`, and the trust level is recorded at
`manifest.plant.trust_level`. Importing `pe_sim.plant_registry` never starts a
simulator and never imports a plugin.

A registration also declares how its packages may be displayed: a
`topology_exporter` callable projects the parameters a run recorded into the
reviewed `TopologyDescriptor`. `export-workbench` resolves the recorded plant
through the registry and requires `export_eligible`, a matching recorded class
name, and an exporter; an unregistered or mismatched plant fails closed before
anything is written. Adding a displayable topology is a registration, not an
edit to the exporter.

A registration also declares `time_model`. `stepped` (the default) means the
kernel owns the clock and can enforce constraints per window; a stepped run
with `control_location="kernel"` remains eligible for `comparable`.
`whole_run` means the engine owns its time axis and returns a trace that can
only be checked afterwards, so it is capped at `limited`. A separate
`control_location` declaration says where the loop closes: `kernel` uses a
ControllerAdapter; `model` closes the loop inside the model and is capped at
`limited` (`actuator_control_in_model`) even on the stepped route. Both time
models are executable: the whole-run route checks the trace after the fact,
records `enforcement: "post_hoc_only"`, and refuses formal comparison because
it cannot enforce constraints in-run. See
[docs/46_time_model_and_engine_boundary_v1.md](docs/46_time_model_and_engine_boundary_v1.md)
and [docs/adr/0004-solve-route-capabilities.md](docs/adr/0004-solve-route-capabilities.md).

A `manifest_identity()` record is producer-declared, so since 0.5.2 the kernel
recomputes it for any adapter that declares a `backend_id` and records the
verdict at `plant.verification`:

| Status | Meaning |
| --- | --- |
| `verified` | digest, review status, and declaring module all agree with the checkout |
| `digest_mismatch` / `review_status_mismatch` | the claim contradicts the pin |
| `module_outside_solve_path` | the running class is not covered by the pin |
| `unverifiable` | no reviewed solve path is declared |

Formal comparison rejects the contradicted statuses and `comparable` requires
`verified`. This is a consistency check, not a sandbox. The design assessment,
including why `EngineAdapter` and `AgentAdapter` are deliberately absent, is in
[docs/42_kernel_interface_topology_v1.md](docs/42_kernel_interface_topology_v1.md).

## Browser workbench status

The TypeScript/Vite browser workbench is temporarily withdrawn because its
rendered content was unusable. The three visualization contracts, the headless
exporter, the loopback read-only service, and the standard-library consumer
example remain. A future UI must consume only those contracts and receives no
Runner, Plant, Controller, or mutation authority.

## End-user acceptance

The repeatable acceptance harness simulates obtaining the project from GitHub:
it exports a clean Git snapshot, creates an isolated virtual environment,
installs the package, exercises both backend-onboarding scenarios, runs the
switching Buck experiment, verifies the published package, and validates the
exported contracts with the external consumer:

```powershell
python scripts/user_acceptance.py `
  --ngspice "D:\tools\ngspice-44.2\Spice64\bin\ngspice_con.exe"
```

Without a simulator the scenario-A guidance is still checked, simulator-
dependent steps are recorded as `skip`, and the report status is `partial`
(never a false pass). Use `--install-mode wheel` to verify a non-editable
install. The report is written to the acceptance workspace. Scope, evidence,
and limitations are documented in
[docs/36_user_acceptance_v0.md](docs/36_user_acceptance_v0.md).

## 基本约定

- `runs/` 只保存本地运行结果，默认不提交到 Git。
- 失败运行必须保留日志和状态，不得只保留成功结果。
- 仿真内部真值与 Controller 可见测量必须分离。
- 仿真证据不能自动升级为硬件或产品结论。
- 具体实现若与 L1 规范冲突，先更新决策记录和测试，再修改代码。

## 规范变更

L1 文档和 Schema 的变更必须带版本号、变更理由、迁移说明和至少一项回归测试。
探索性实现可以放在独立分支或工作目录，不能覆盖已锁定的参考基线。

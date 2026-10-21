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
test fixtures, read-only visualization contracts, constrained
local visualization service, reference workbench, exclusions, run-status
meanings, and release gates are defined in
[docs/27_minimum_local_release_scope_v0.md](docs/27_minimum_local_release_scope_v0.md).
In particular, `QUALIFIED` is only an automated per-run qualification state; it
does not establish physical correctness, hardware safety, product readiness, or
maintainer approval.
The admitted version ranges, exact locally verified Python/Node/npm toolchain,
and clean-install policy are defined separately in
[docs/28_release_toolchain_policy_v0.md](docs/28_release_toolchain_policy_v0.md).

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
| L1 | `docs/05_current_workspace_migration_map_v0.md` | `D:\PySpice` 到本项目的迁移边界 |
| 机器规范 | `schemas/manifest.schema.json` | Manifest 的最小 JSON Schema |
| 机器规范 | `schemas/experiment.schema.json` | 实验配置的最小 JSON Schema |
| Agent 入口 | `agent/AGENTS.md` | 每次 Agent 任务必须遵循的规则 |

## v0 的明确范围

v0 以 **Buck 参考电路（PySpice 构建 + ngspice 进程执行）**、统一 Runner、
Manifest、受限后处理和只读可视化数据边界作为最小可发布切片。理想平均
Buck/Boost 模型仅保留为确定性测试夹具，不再作为参考电路。`psfb-step` 仍保留为
fake backend 的契约 smoke fixture，而不是 PSFB 电路模型。完整支持矩阵和明确
排除项以 `docs/27_minimum_local_release_scope_v0.md` 为准；器件、电热、
PSFB/LLC、学习和硬件结论均不因该切片而被隐式纳入。

## 最小运行切片

在全新 Python 环境中安装当前骨架并检查 CLI：

```powershell
python -m pip install -e .
pe-sim --help
pe-sim psfb-step --output-dir runs --run-id demo
```

`psfb-step` 使用内置 fake backend 作为契约验收 fixture，不代表真实
PySpice/Ngspice 物理结果。运行目录包含配置快照、事件、样本、资格/安全记录和
带 SHA-256 的 `manifest.json`。真实 PSFB 适配器只有在 Gate 1/2 通过后才迁移。

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
the 1.26.4 NumPy pin, then run `pip check` there. The ideal averaged `buck_pi_step` and `boost_pi_step` examples are deterministic
test fixtures, not reference-circuit results. They remain available for runtime
and comparison tests:

```powershell
pe-sim run examples/buck_pi_step/config.json --backend buck
pe-sim run examples/boost_pi_step/config.json --backend boost
```

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
library. The contract and compatibility rules are in
[docs/26_stage11_external_consumer_extension_v1.md](docs/26_stage11_external_consumer_extension_v1.md).

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

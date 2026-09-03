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

v0 只承诺一个可运行的纵向切片：PSFB 理想 Plant、PI Controller、负载阶跃、
统一 Runner、Manifest 和最小可复现实验。物理、器件、电热、学习和第二种拓扑
属于后续扩展，不能在 v0 验收中隐式加入。

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

## 基本约定

- `runs/` 只保存本地运行结果，默认不提交到 Git。
- 失败运行必须保留日志和状态，不得只保留成功结果。
- 仿真内部真值与 Controller 可见测量必须分离。
- 仿真证据不能自动升级为硬件或产品结论。
- 具体实现若与 L1 规范冲突，先更新决策记录和测试，再修改代码。

## 规范变更

L1 文档和 Schema 的变更必须带版本号、变更理由、迁移说明和至少一项回归测试。
探索性实现可以放在独立分支或工作目录，不能覆盖已锁定的参考基线。

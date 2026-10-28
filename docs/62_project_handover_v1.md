# 项目移交文档（v1）

**文档性质：** 维护者交接说明。它不是发布批准、不是工程结论，也不改变任何运行时行为。

**适用基线：** 2026-10-28，`main` 提交 `ced25ea`（其父提交为 `6b55cb8`），版本
`0.17.0`，`origin/main` 已同步。

**当前阶段：** S0–S9 已交付；S10（收口与发布）尚未正式完成。

---

## 1. 项目定位与核心目标

### 1.1 这个项目要解决什么

这是一个**可审计、可扩展的电力电子仿真基础设施**，不是通用电路求解器，也不是
器件/热/硬件验证工具。核心目标按优先级排列：

1. **可审计的执行记录**：每次运行都产生可重算的 manifest、产物索引、摘要、
   资格/安全/有效性证据和 provenance；读取端必须调用同一套校验实现。
2. **闭环约束在运行期间生效**：内核能在控制窗口边界拦截动作、观测和资格判定；
   不满足这一点的路线必须显式降级证据上限。
3. **新能力通过注册扩展，零内核改动（T1）**：新植物、控制器、引擎、比较器都是
   `PlantRegistration` / `ControllerAdapter` / `PlantAdapter` / `WholeRunPlant` 的
   新实现，而不是 Runner 里的新分支或现有适配器上的隐藏模式开关。
4. **证据分级诚实**：`diagnostic_only < functional < limited < comparable`；
   上限只在 `time_model.py` 的单一派生点产生，适配器不能自我认证。
5. **稳定面与实验面分离**：`tests/fixtures/public_api.json` 的稳定名单冻结；
   新名字只能进入 `EXPERIMENTAL_API`。
6. **发布可追溯**：本地 gate、分发清单、release record、人工批准和 tag 是分开的
   工件；没有 gate 证据和人工批准时，不得声称已发布。

### 1.2 明确的非目标

- 物理正确的器件、磁性、损耗、热、EMI、电热网络或硬件安全结论；
- 通用 SPICE 网表编辑器、任意拓扑库、自动布线/布局；
- PSFB/LLC 等未实现拓扑的“参考结果”；
- 实时/HIL、远程多用户可视化、认证/TLS/密钥/SBOM 签名；
- 用 `QUALIFIED` 代替物理正确性、工程批准或硬件放行；
- 内置训练、在线自适应或自动替换 Controller/Plant 的学习闭环。

### 1.3 关键不变量

| 不变量 | 含义 |
| --- | --- |
| I1 | 约束必须在运行期间强制执行；事后重建不算 `comparable` |
| I2 | 内核重算身份/摘要，适配器只能声明；适配器不能自报证据等级 |
| I4 | 公共稳定面冻结；实验能力只能进入 `EXPERIMENTAL_API` |
| T1 | 新引擎/新模型是注册，不是内核分支；新增能力不得修改 Runner 协议 |
| 单一上限 | `RouteCapabilities` + `time_model.py` 是证据上限的唯一派生点 |

---

## 2. 当前基线

| 项 | 当前值 |
| --- | --- |
| 分支/提交 | `main` = `ced25ea`（本文所在提交；其父提交为 `6b55cb8`）= `origin/main` |
| 版本 | `0.17.0`（`src/pe_sim/_version.py` 与 `pyproject.toml` 一致） |
| 覆盖矩阵 | 35 行：34 `covered`，1 `out_of_scope`（C8 电热/热网络） |
| 开放行 | 无 `kernel_gap` / `expressible` |
| 当前 Python 环境 | `d:\power-electronics-ai-sim\.venv\Scripts\python.exe`（本地为 3.13.2） |
| 全量测试（含 ngspice 41） | `1720 passed / 2 skipped` |
| 全量测试（无 ngspice） | `1710 passed / 12 skipped` |
| 发布 gate / tag | **未完成**；仅通过 `release_record.py check-identity`，没有正式 gate 证据、release record 或 tag |

`pe-sim --version` 应输出 `0.17.0`。正式发布 gate 使用 `docs/29` 的隔离环境
（Python 3.12.7 + 固定 pin），与当前开发 venv 不是同一件事。

### 2.1 交接资产

| 资产 | 状态/位置 |
| --- | --- |
| 源码仓库 | `https://github.com/t7x7t/power-electronics-ai-sim.git`，`main` = `ced25ea` |
| 本地开发环境 | `d:\power-electronics-ai-sim\.venv`（Python 3.13.2）；venv 不入库，换机需按 `pyproject.toml` / `requirements-tested.txt` / `requirements-release.txt` 重建 |
| ngspice（可选） | 当前验证路径 `D:\Applications\Anaconda3\envs\pe-sim-spice\Library\bin\ngspice_con.exe`，通过 `PE_SIM_NGSPICE` 启用 |
| PySpice（可选） | 不是运行时依赖；未安装时相关能力报告为不可用，不是失败 |
| 发布证据/record/tag | **无 0.17.0 的正式 gate 证据、release record 或 tag**；只有 `check-identity` 通过 |
| 凭据/密钥 | 仓库与交接材料中不应包含任何凭据或私有数据 |

---

## 3. 架构与关键契约

### 3.1 执行链

```text
CLI / Python API
  -> ExperimentSpec / Timebase / contracts
  -> Runner（stepped / kernel 或 stepped/model）
     或 run_whole_run_route（whole_run/model）
  -> PlantAdapter.advance() 或 WholeRunPlant.run_whole()
  -> ControllerAdapter.observe()（仅 kernel 侧闭环）
  -> safety / validity / applicability / qualification
  -> ArtifactWriter（manifest、samples、audit、qualification、hash）
```

### 3.2 路线声明与证据上限

`PlantRegistration` 声明三个独立事实：

- `time_model`：`stepped`（内核掌握时钟）或 `whole_run`（引擎掌握时钟）；
- `control_location`：`kernel`（内核 Controller 闭环）或 `model`（模型内闭环）；
- `event_granularity`：`window`（控制窗口边界）或 `continuous`（声明变更点）。

`whole_run + kernel` 在构造期即拒绝。上限派生表以 `docs/adr/0004` 与
`src/pe_sim/time_model.py` 为准；不要在适配器或 CLI 里复制上限计算。

### 3.3 主要模块

| 模块 | 职责 |
| --- | --- |
| `src/pe_sim/runtime.py` | Runner、预检、窗口推进、审计、manifest 组装 |
| `src/pe_sim/whole_run_route.py` | whole_run 路线、事后 trace 校验、post-hoc 证据 |
| `src/pe_sim/contracts.py` | `ExperimentSpec`、`PlantAdapter`、`ControllerAdapter` 等协议 |
| `src/pe_sim/plant_registry.py` | 注册表、路线声明校验、catalog |
| `src/pe_sim/builtin_plants.py` | 项目内置植物的组合根（唯一“哪些植物存在”的地方） |
| `src/pe_sim/time_model.py` | 路线记录、证据上限、limitation 码的唯一派生点 |
| `src/pe_sim/artifacts.py` | canonical JSON、摘要、原子写入、包校验 |
| `src/pe_sim/study.py` / `study_runner.py` | study 计划、逐点执行、失败传播与聚合 |
| `src/pe_sim/capability_analysis.py` | `pe-sim capability` 的 AC/阻抗/损耗入口 |
| `src/pe_sim/model_capabilities.py` | 小信号模型、损耗模型、能力不可用原因 |
| `src/pe_sim/ac_analysis.py` / `impedance_analysis.py` | Bode、PM/GM、Zin/Zout、Middlebrook |
| `src/pe_sim/lossy_converter.py` | 声明损耗参数的 averaged buck 与损耗分解 |
| `src/pe_sim/backends/` | ngspice、同步整流、多相、模型内控/峰值电流适配器 |
| `src/pe_sim/cli.py` | 用户入口与拒绝信封 |
| `src/pe_sim/public_api.py` | `STABLE_API` / `EXPERIMENTAL_API` |

### 3.4 当前植物与适配器

| plant_id | 模型 | 路线 | 说明 |
| --- | --- | --- | --- |
| `buck` / `boost` | L1 理想 averaged | stepped/kernel | 确定性参考夹具 |
| `multiphase-buck` | L1 共享 duty 多相 | stepped/kernel | 只积分总电流，不建模逐相均流 |
| `ngspice-buck` | L2 异步开关 | stepped/kernel，continuous | 被审 ngspice solve path |
| `ngspice-sync-buck` | L2 同步整流 | stepped/kernel，continuous | 双开关、死区、无二极管 |
| `whole-run-buck` | averaged | whole_run/model | 引擎自有 PI，post-hoc |
| `model-control-buck` | L2 开关级 | stepped/model | 模型内 PI，内核 action 被记录但不驱动 |
| `whole-run-switching-buck` | L2 开关级 | whole_run/model | 整段开关轨迹 + 模型内 PI |
| `peak-current-buck` | L2 开关级 | stepped/model | 固定频率峰值电流、模型内比较器、可选二极管过零保持 |
| `fake` / `fake-load` | fixture | stepped/kernel | 仅测试，不得当参考结果 |

`lossy-buck` 是 capability-only 的能力植物，不是 runner 注册植物。

---

## 4. 阶段性结果（S0–S9）

| 阶段 | 版本 | 已交付 |
| --- | --- | --- |
| S0 | 0.9.8 | 缺口收敛计划、覆盖矩阵机器守卫 |
| S1 | 0.10.0 | 可表达性证明；5 行转 `covered`，无产品代码变更 |
| S2 | 0.10.1 | `time_model` / `control_location` / `event_granularity` 声明与单点上限派生 |
| S3 | 0.11.0 | 初始状态载荷（ADR 0006）；预偏置/热启动 |
| S4 | 0.12.0 | 受限派生指标与证据报告 |
| S5 | 0.13.0 / 0.13.1 | 相位连续、变更点求解；维护者 review pin；修复导通脉冲剩余时间丢失 |
| S6 | 0.14.0 / 0.14.1 | Type II/III 控制器 seam；保护/短路恢复（A7/B9） |
| S7 | 0.15.0 | study 计划层、逐点失败传播、聚合恒 `per-point` |
| S8 | 0.16.0 / 0.16.1 | `pe-sim capability`（AC/阻抗/损耗）；写失败映射 `output_error` |
| S9 | 0.17.0 | A8/A9/B7（同步整流、DCM/burst 检测、多相）；A11/B8/B3/D3（整段开关、模型内控、峰值电流/比较器） |
| S10 | 0.18.0 | **待完成：收口、发布说明汇总、正式 gate/release record/tag** |

### 4.1 S9 第二片的细节

`src/pe_sim/backends/model_control_buck.py` 提供一个共享的确定性开关核心：

- `model-control-buck`：`stepped + model`；内核仍观察每个窗口，但模型内 PI
  决定 duty；内核 action 写入 truth 的 `kernel_action_ignored`，不参与状态推进。
- `whole-run-switching-buck`：`whole_run + model`；一次 `run_whole()` 输出声明
  cadence 的开关纹波轨迹，只在事后校验。
- `peak-current-buck`：`stepped + model`；外层电压 PI 给出电流参考，模型内
  比较器计算每周期 on-time；`diode_emulation=True` 时记录二极管过零并保持零电流。

这三条路线的证据上限都由既有 `RouteCapabilities` 派生为 `limited`，限制码
`actuator_control_in_model` / `post_hoc_validation_only` / `events_quantized_to_control_windows`
由单点派生。新 solve path（`model-control-buck`、`whole-run-switching-buck`、
`peak-current-buck`）**尚未加入 `reviewed_identities.json`**，因此不能达到
`verified` / `comparable`，这是预期行为，不是回归。

---

## 5. 证据上限与不可越界表述

| 场景 | 上限 | 必须保留的限制码/理由 |
| --- | --- | --- |
| A7 保护/短路 | `limited` | `protection_latency_is_one_control_period` |
| A11 整段开关轨迹 | `limited` | `post_hoc_validation_only`；不能当实时拦截证据 |
| B3 峰值电流 | `limited` | `actuator_control_in_model`；比较器在模型内 |
| D3 比较器/器件事件 | `limited` | 与 B3 同源；事件由模型求解，不是内核逐周期保护 |
| B8 模型内闭环 | `limited` | `actuator_control_in_model`；模型侧 PI，**不是器件级 SPICE 网表补偿器** |
| B7 多相 | 仅共享 duty 可 `comparable` | `per_phase_current_sharing_not_modelled` |
| C4 AC / C5 阻抗 | `limited`（解析）或 `functional`（辨识） | `linearization_method_is_model_declared` |
| C6 损耗 | `inherit`（效率）/ `limited`（分解） | `loss_breakdown_is_model_declared` |
| A10/C9 聚合 | `per-point` | `aggregate_does_not_inherit_comparable` |

不得把 `QUALIFIED`、`functional` 或 `limited` 写成物理正确、硬件安全或正式比较结论。

---

## 6. 未完成工作

### 6.1 S10 收口清单

1. 在 `docs/61` 将 S10 标记完成并记录验收；`docs/60` 已无开放行（34 covered +
   1 out_of_scope）。
2. 新增 `docs/releases/v0.18.0.md`：汇总每个阶段的上限、未做事项和当前基线；
   将 `_version.py` / `pyproject.toml` 提升到 `0.18.0`（以实际计划为准）。
3. 同步 `docs/58` 能力边界；检查 README 不再声称已被删除的限制
   （如“控制周期必须是开关周期整数倍”）。
4. 运行：
   - `pytest tests/test_coverage_matrix.py -q`
   - 全量 `pytest -q`（有/无 ngspice 各一次）
   - `python scripts/release_record.py check-identity`
5. 按 `docs/29` / `docs/53` 完成正式本地 release gate、wheel/sdist、distribution
   inventory、release record、人工批准和 tag。没有 gate 证据或人工批准时，
   不得声称已发布或创建 tag。
6. 若正式 gate 需要 ngspice/PySpice，记录精确版本、SHA-256、平台和 solver
   settings；缺失时只能记 `not_run`，不能记 pass。

### 6.2 已知技术债/限制

| 项 | 说明 | 建议 |
| --- | --- | --- |
| 新 solve path 未 review-pinned | `model-control-buck`、`whole-run-switching-buck`、`peak-current-buck`、`ngspice-sync-buck` 等未写入 `reviewed_identities.json` | 需要维护者按 §5 流程审查并记录新摘要；在此之前保持 `limited` |
| 模型内控不是网表补偿器 | B8 由 Python 模型侧 PI 闭合，不是 SPICE 网表中的器件级 Type III | 文档/对外说明必须写“model-side PI”；真正的网表内补偿器仍是外部 ngspice 或后续 ADR |
| 峰值电流近似 | on-time 用周期起点线性电感斜率；无 blanking、传播延迟、slope compensation、电流采样滤波 | 不得作为逐周期保护或电流环稳定性证明；需要时新增适配器/ADR |
| 整段状态交接 | whole-run 的 `initial_state` 不携带 duty/phase；分段续跑与一次整段在 1e-4 量级内一致，但不是 bit-identical | 需要严格逐位复现时保存并声明 duty/phase，或使用 stepped 路线 |
| `middlebrook` 无频率轴回显 | 结果只有 `total_points` 与 `provenance.sweep`，父级 `impedance` 才有频率轴 | 消费端从父结果取轴；不要自行假设别名 |
| `report_path` 相对路径 | 文档给相对 `output.directory` 时，stdout 保留相对字符串 | 需要绝对路径时由调用方规范化 |
| `input_sha256` 覆盖整份输入文档 | 包含 `output.directory`，改变输出目录会改变哈希 | 比较“分析身份”时需先剔除 `output` |
| `verified_by` 只校验格式 | 目前校验 `tests/<file>.py::<test_name>` 形状，不证明该测试函数真实存在 | 提高证据等级前应加存在性校验或人工复核 |
| schema registry 覆盖不完整 | 新增的 AC/阻抗/能力报告 schema 未必都进入 `schema_registry.py` | 跨版本/迁移声明前先补齐并测试 |
| `ac_analysis` 重复 `_finite_number`（已修复） | 本地 helper 与 `frequency_domain.finite_number` 重复，遮蔽了导入；本轮删除本地定义、统一使用导入，`tests/test_ac_analysis.py` 通过 | 无需后续动作；新增数值校验请只改 `frequency_domain.finite_number` |
| 周期检测仍是指标 lag 型 | `evaluate_periodicity` 声明 `sampling_interval_s` 与索引 lag 近似 | 只作为声明的收敛检测，不是严格周期证明 |
| event_granularity 限制码语义 | `window` 注册实际按变更点求解，限制码只描述控制器侧；已由 `docs/58` + ADR 0004 + 守卫测试澄清 | 若未来要让 `window` 真正量化输入，需新 ADR 并重跑 S5 验收 |

---

## 7. 不可越过的边界（交接后仍有效）

逐条来自 `docs/61` §3，违反任一条都视为交付失败：

1. 不改 `tests/fixtures/public_api.json` 的 `stable` 名单/哈希；新名字只能进
   `EXPERIMENTAL_API`。
2. 不改 `docs/57` §3 的 N1–N8 冻结绕过面；不要新增对抗性检查。
3. `src/pe_sim/reviewed_identities.json` 只能由维护者在记录审查后更新。
4. 不给现有适配器加“模式开关”；新能力必须是新注册/适配器/控制器/计划层
   （ADR 0004 反补丁规则 1）。
5. 不在本地计算证据上限；只在 `time_model.py` 单一派生点产生。
6. 适配器不能声明证据等级；身份和摘要由内核重算。
7. `docs/60` 的状态只能在监督者验收后更新；不得先改台账后补测试。
8. 不做范围外重构；格式化、重命名、目录调整不属于任务。
9. 不新增第三方运行时依赖；派生指标使用标准库或已锁定 numpy。
10. 不降低既有断言；必须修改测试时逐条说明理由。
11. 全量测试运行期间不要编辑仓库（`test_full_flow_check` 会读源码/文档）。
12. 新增/修改适配器必须有失败测试先行、独立复算和至少一次对抗性复核；
    不能只凭“测试绿了”就更新 `docs/60`。

---

## 8. 本地操作手册

### 8.1 环境

```powershell
cd D:\power-electronics-ai-sim
.\.venv\Scripts\python.exe -m pytest -q
```

启用真实 ngspice 窗口：

```powershell
$env:PE_SIM_NGSPICE = 'D:\Applications\Anaconda3\envs\pe-sim-spice\Library\bin\ngspice_con.exe'
.\.venv\Scripts\python.exe -m pytest -q
```

正式发布 gate 使用 `docs/29` 的隔离 Python 3.12.7 环境；不要用当前 3.13 venv
替代 release evidence。

### 8.2 常用入口

```powershell
pe-sim demo .\my-demo
pe-sim run .\config.json --backend buck
pe-sim study .\study.json --backend buck
pe-sim capability .\capability.json
pe-sim inspect-run .\runs\<run_id> --compact
pe-sim review-run .\runs\<run_id>
pe-sim parameter-view .\runs\<run_id>
pe-sim export-workbench .\runs\<run_id> .\export
pe-sim visualization-serve --runs-root .\runs
pe-sim capabilities
```

### 8.3 发布相关

```powershell
python scripts/release_record.py check-identity
powershell -ExecutionPolicy Bypass -File scripts/release_check.ps1
```

正式发布流程以 `docs/29` / `docs/53` 为准：gate → wheel/sdist → inventory →
release record → 人工 approve → tag → verify `--require-tag`。没有人工批准不得
创建 tag，也不得把本地 gate 说成已发布。

---

## 9. 接手后第一周建议顺序

1. 阅读 `agent/AGENTS.md`、`docs/01`、`docs/02`、`docs/03`、`docs/60`、`docs/61`。
2. 确认基线：`git status`、`git log -1`、`pe-sim --version`、`git log -1 origin/main`。
3. 跑全量测试（有/无 ngspice），记录计数和跳过原因。
4. 跑 `release_record.py check-identity`，确认版本/发布说明一致。
5. 逐项核对 S10 清单与 §6.2 技术债；先完成 S10，再启动新的产品级功能
   （可视化/Workbench 扩展等）。
6. 任何新适配器：先写失败测试，再注册；不要改 Runner 分支或现有适配器开关。
7. 每次交付：独立复核（另起探针或子智能体）+ 全量测试 + 记录上限 + release note
   （若提升版本）。

---

## 10. 关键文件索引

- 架构/策略：`docs/01`、`docs/03`、`docs/43`、`docs/58`、`docs/60`、`docs/61`
- 产物/证据：`docs/02`、`docs/17`、`docs/22`、`docs/50`、`docs/57`
- Runner 契约：`src/pe_sim/runtime.py`、`src/pe_sim/contracts.py`、`src/pe_sim/time_model.py`
- 注册/组合根：`src/pe_sim/plant_registry.py`、`src/pe_sim/builtin_plants.py`
- 分析能力：`src/pe_sim/model_capabilities.py`、`src/pe_sim/ac_analysis.py`、
  `src/pe_sim/impedance_analysis.py`、`src/pe_sim/lossy_converter.py`
- 新适配器：`src/pe_sim/backends/model_control_buck.py`、`src/pe_sim/backends/sync_buck.py`、
  `src/pe_sim/multiphase_plants.py`
- 公共面：`src/pe_sim/public_api.py`、`tests/fixtures/public_api.json`
- 发布：`scripts/release_check.ps1`、`scripts/release_record.py`、`docs/29`、`docs/53`
- 交接基线测试：`tests/test_model_control_adapters.py`、`tests/test_sync_buck_plant.py`、
  `tests/test_multiphase_plant.py`、`tests/test_capability_cli.py`、`tests/test_coverage_matrix.py`

---

## 11. 术语与证据等级

| 术语 | 含义 |
| --- | --- |
| `RUN_OK` | Runner 正常完成；不代表资格/物理正确 |
| `QUALIFIED` | 单个运行通过配置的自动安全/有效性/资格检查 |
| `PUBLISHED` | 产物写入器完成事务/完整性检查；不代表人工批准 |
| `diagnostic_only` | 只能诊断；不能用于结论 |
| `functional` | 功能性证据；低于 `limited`/`comparable` |
| `limited` | 能力可用但路线/模型有声明上限 |
| `comparable` | 在 clean worktree、已验证身份、完整资格等条件下允许正式比较 |
| `formal_comparison` | 拒绝 dirty/unknown worktree 的正式比较模式；不是物理验证 |

---

## 12. 交接确认清单

- [ ] 新维护者已阅读本文与 §10 的关键文档
- [ ] 基线 commit/版本/远端已核对
- [ ] 全量测试（有/无 ngspice）已复跑并记录计数
- [ ] S10 收口清单已排期
- [ ] 新 solve path 的 review pin 计划已确认（若需要 `verified`/`comparable`）
- [ ] 已明确“模型内 PI/比较器不是网表器件级补偿器/内核逐周期保护”
- [ ] 已明确正式发布门禁与人工批准流程

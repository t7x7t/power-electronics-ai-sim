# 人向可视化：缺失接口需求文档（v1）

**Status:** 需求与验收定义。由 `docs/70` §12.6 的 D2 决定解锁"受控面板扩展点"。本文档不是范围批准以外的东西：它定义**缺什么接口、怎么验收**，不改变任何已有信封的语义。

**读者：** 接手实现的 agent。按 R1–R8 逐条实现；每条都有机械验收，未通过不得声称完成。

**实施状态（2026-10-30）：** R1 / R2 / R8 已落地，R3 / R4 / R6 已改判，R5 / R7 未实施。逐项状态、事实更正与验收证据见 §7。§2–§6 的原始论证保留不改，凡与 §7 冲突处以 §7 为准。

---

## 0. 一句话

现有界面回答"这份证据可不可信"；人要问的是"**发生了什么、改了什么、为什么、结果好不好**"。本文档只补齐回答后者所缺的最小接口。

## 1. 实现者必须遵守的五条

| 编号 | 规则 |
| --- | --- |
| P1 | **只增不改**：不得改变既有信封语义；加键不升版本（`docs/68` §4）。 |
| P2 | **不碰冻结面**：`src/pe_sim/runtime.py`、`contracts.py`、`runner/**`、`topology.py`、`public_api.py`、`src/pe_sim/reviewed_identities.json` 的改动必须先获得项目所有者批准。 |
| P3 | **每个界面元素都要能回答"来自哪个字段"**；回答不出的不许画。 |
| P4 | **缺数据时显式说明**（"未接入"/"未实现"），不得用相近字段代填。 |
| P5 | **探索性结果与可比证据不得同形呈现**。 |

## 2. 现状（已核实，可直接采信）

| 事实 | 证据 |
| --- | --- |
| 运行中有事件流：`pe-sim.run-event.v1`，含 `run.started` / `run.progress` / `run.finished`，已登记 | `src/pe_sim/run_events.py:41-43`；`docs/68` 第 55 行 |
| `run.progress` 载荷：`step_index, sample_count, steps_total, time_s, fraction, wall_time_s` | `src/pe_sim/runtime.py:1018-1030` |
| 只有 `run` 子命令有 `--progress`；`study` 硬编码 `progress=False` | `src/pe_sim/cli.py:693-697`、`:1493` |
| `--cancel-file` 已存在，可用于取消 | `src/pe_sim/cli.py:698-703` |
| 运行中的 run 写在隐藏 staging 目录；`.run-state.json` 在**正常发布路径**只有 3 次写入（RUNNING / READY_TO_PUBLISH / PUBLISHED），另 `abort()` 会写 `ABORTED`；**没有任何周期性心跳** | `src/pe_sim/artifacts.py:130`、`:159`、`:161-170`、`:176` |
| checkpoint 默认关闭，CLI 无开关 → 轮询 staging 只能判"存在"，判不出进度 | `src/pe_sim/runner/policies.py:47`；`cli.py` 无 `checkpoint` 参数 |
| 发现接口看不到运行中 run：`.xxx` 被剪枝、且无 `manifest.json` | `src/pe_sim/catalog.py:32`、`:224`、`:227`；`src/pe_sim/visualization_service.py:23`、`:70` |
| run 的配置**完整**保存在包内 `config.snapshot.json`（= `ExperimentSpec.to_dict()`） | `src/pe_sim/runtime.py:1133` |
| `manifest.json` **不含**完整配置，只有 `plant/controller.identity.run_configuration` | `src/pe_sim/runtime.py:1704-1705`、`:2114-2116` |
| manifest schema 允许附加键（`additionalProperties: true`）→ 加键不必迁移 | `schemas/manifest.schema.json:6`（`src/pe_sim/schemas/manifest.schema.json` 必须逐字节同改） |
| 分析类型词表已存在于文档：C1 时域瞬态 / C2 解析稳态 / C3 PSS / C4 AC 小信号与环路增益 / C5 阻抗 / C6 效率损耗 / C7 纹波频谱 / C8 电热 / C9 Monte Carlo / C10 波形导出 | `docs/60_simulation_coverage_matrix_v1.md:58-67` |
| 现有三套 kind 互不相通：`study`（parameter_sweep/monte_carlo/periodicity）、`capability`（ac/impedance/loss）、**run 无字段** | `src/pe_sim/study.py:52`；`src/pe_sim/capability_analysis.py:44`；`src/pe_sim/contracts.py:487-504` |
| `ExperimentSpec` 的 schema 是 `additionalProperties: false`，未知键被拒 | `schemas/experiment.schema.json:6` |
| 已实现但不在默认指标集：效率、纹波、观测占空比、相位裕度、增益裕度 | `waveform_metrics.py:526`、`derive.py:292`、`waveform_metrics.py:264`、`ac_analysis.py:753`、`:755` |
| 未实现为指标：超调量、调节时间、以 vref 为基准的稳态误差（`overshoot`/`settling` 全仓无指标实现；`steady_state` 仅指 `analytical_steady_state` 解析稳态） | 全仓无对应 `MetricDefinition` |
| 默认指标注册表（7 条，无物理指标） | `src/pe_sim/postprocess.py:200-213` |
| 血缘未实现（CRED-07）；唯一现成链是 resume 的 `recovery.source.run_id` | `docs/40:265`；`src/pe_sim/runtime.py:1937`（组装）、`:682-689`（注入）、`:1718`（写入 manifest） |
| 意图/自由文本：spec、experiment schema、manifest 三处都没有 | `contracts.py:487-504`；`schemas/experiment.schema.json:6` |
| 授权：设计阶段，**未实现**，内核不做沙箱 | `docs/52:3`、`:250-252` |
| 控制框图：无任何描述格式；拓扑描述符可作为"图→数据"的先例 | `src/pe_sim/topology.py:67-160` |

## 3. 需求清单

### R1 运行中可见（活动监看）

- **人的问题**：正在跑的那次到哪一步了？还剩多少？
- **最小接口**：示例启动器监管 `pe-sim run --progress` **子进程**，把 stderr 的 JSON Lines 缓存在内存，通过同源静态服务器暴露 `GET /live.json`（`Cache-Control: no-store`）。页面轮询该文件。
- **数据来源**：已有 `pe-sim.run-event.v1`，**不新增信封**。- **落点**：`examples/visualization_consumer/launch.py`（+ 页面）。`src/` 零改动。
- **验收**：
  1. run 期间 `live.json` 的 `fraction` 单调不减；
  2. `sequence` 单调递增，无乱序；
  3. 触发取消后最终事件为 `run.finished`，状态 `INCOMPLETE`；
  4. 页面在实时块上必须常显"未发布，不是证据"；
  5. 进程退出后 `live.json` 保留最后一次终态，不残留"运行中"。
- **不做**：不覆盖 `study`（无进度）；不做 SSE/WebSocket；不改服务。
- **注意**：`docs/70` §4.1 的"不得启动 Runner"指**进程内**取得 Runner；监管 CLI 子进程是 §7.3 已列出的路径，不违反。

### R2 分析类型可读（不改内核）

- **人的问题**：这次是瞬态、小信号、环路稳定性，还是参数扫描？
- **最小接口**：消费方用 `docs/60` 的 **C1–C10** 作为显示词表，按现有字段推导：
  - study 产物 → 读 `study-report.json` 的 `kind`；
  - capability 产物 → 读 `capability-report.json` 的 `kind`；
  - 普通 run → **无字段**，只能显示"未声明"，并列出推导依据（plant_id / controller_id / input_schedule 有无）。
- **落点**：消费方（`examples/`）。**不要**给 `ExperimentSpec` 加字段。
- **验收**：
  1. 三种来源各自的显示值可从文件字段逐字回溯；
  2. 普通 run 显示"未声明（run 文档无分析类型字段）"，不得显示任何猜测类型；
  3. 环路稳定性与小信号同属 C4，界面不得虚构成两类。
- **升级路径（需 owner 批准）**：若确定要给 run 加 `analysis`，必须同时改 `schemas/` 两份副本、`contracts.py`（**冻结面**）、`docs/12`，并给旧包兼容策略。本文档不授权该改动。

### R3 结果指标补齐（人真正要看的数）

- **人的问题**：这个结果算好还是不好？
- **最小接口**：在 `default_metric_registry()` 增加 4 个 `MetricDefinition`，对**已发布 run** 离线计算，不重跑仿真：
  | 指标 id | 定义 | 缺参照时 |
  | --- | --- | --- |
  | `series.vout.overshoot` | `100·(max(vout) − vout_final)/|vout_final|` | `{"value": null, "reason": "no_reference"}` |
  | `series.vout.settling_time` | 最后一次离开 ±2% 终值带的时刻到 `t_end` 的时长 | `not_settled` |
  | `series.vout.steady_state_error` | `vout_final − vref`；`vref` 按优先级取：`input_schedule[-1].vref` → `plant_config.default_vref_v` | `no_reference` |
  | `series.vout.ripple_pp` | 复用 `derive.ripple_statistics` | — |
- **落点**：`src/pe_sim/postprocess.py`（**非冻结面**）。`RunEvidence` 需新增读取 `config.snapshot.json`。
- **验收**：
  1. 对 `runs/demo-buck` 四个 id 均产出有限值或 `null`+`reason`，不得抛异常；
  2. 结果可重放：同一包两次计算逐字节相同；
  3. 指标标注为"派生"，不得与证据等级混用；
  4. **不重算** 效率与 PM/GM（它们在 capability 侧有权威实现，重算会产生第二口径）。

### R4 血缘与基线（"跟谁比"）

- **人的问题**：这次是相对哪一次改的？
- **最小接口**：manifest 增加三个**可选**键：
  - `parent_run_id`（resume 时自动取 `recovery.source.run_id`）；
  - `study_id` + `point_index`（study 点位）；
  - `baseline_commit`（用于"自基线以来改了什么"）。
- **落点**：`src/pe_sim/runtime.py` 的 `_build_run_manifest`（**冻结面，需批准**）；两份 `manifest.schema.json` 同步补定义。
- **验收**：
  1. 旧包（无这些键）仍可 `inspect-run` / 服务读取；
  2. resume 产生的新包自动带 `parent_run_id`；
  3. 键缺失时消费方显示"无血缘信息"，不得回退到"按时间猜"且不说明。

### R5 意图（"为什么做"）

- **人的问题**：这次是为验证什么？
- **最小接口**：manifest 可选 `intent: {text, source: "user"|"agent", recorded_at}`；CLI `run` 增加可选 `--intent`。
- **落点**：`runtime.py`（冻结面，需批准）+ `cli.py`（非冻结）。
- **验收**：
  1. 界面显示该文本时必须标注"作者陈述，不是证据"；
  2. 未提供时不显示空壳区块；
  3. 文本原样保留，不参与任何判定。

### R6 授权可见性（`docs/52` E1，只读）

- **人的问题**：这轮 agent 到底动了哪些文件？
- **最小接口**：只读报告"自基线以来被改动的文件"，并给出 component → path 的映射（若映射未定，只显示文件列表）。
- **落点**：新模块 + `runtime.py`（冻结面，需批准）；可复用 `src/pe_sim/dirty.py:86` 的 `analyze_git_worktree`。
- **验收**：
  1. 明确标注"改动可见 ≠ 有权改动"；
  2. 不新增拒绝逻辑、不新增命令（`docs/52` E1 要求）；
  3. 不显示"权限"字样，除非有对应字段。

### R7 控制框图描述符（只描述，不执行）

- **人的问题**：这个控制器由哪几个块组成，信号往哪走？
- **最小接口**：新增 `control-diagram-descriptor-v1`，结构与 `TopologyDescriptor` 平行：`components[]{id,kind,params}`、`nets[]{id,label,kind}`、`ports`、`fingerprint`。**只描述**：不进 `plant_config`、不参与编译、不做布局、不含图标库。
- **落点**：新模块 + 新 schema（两处副本）+ `docs/68` + `tests/test_full_module_scan.py::SCHEMAS`。
- **验收**：
  1. 往返序列化稳定，指纹对参数变化敏感；
  2. 若该描述符进入任何 plant 的输入或允许 agent 借此改电路语义，即触碰 `docs/24` §7 最后一项，必须先改文档；
  3. 界面在缺失时显示"未接入框图"。

### R8 面板扩展点（D2 的落地形态）

- **人的问题**：我要看的东西不在你们的面板里，能不能自己加？
- **最小接口**：
  - `panels.json`（新信封 `pe-sim.visualization-panels.v1`）：`{panels:[{id,title,needs[],entry}]}`；
  - 面板模块：`export default { id, title, needs, render(ctx) }`，返回 DOM 节点；
  - `ctx` 由宿主提供：`{run, catalog, discovery, topology, trace, outputs, parameters, review, change, live}`，缺项为 `null`。
- **宿主职责（且只有这些）**：取数、按 `needs` 分发、渲染容器、**单面板异常隔离**（一个面板抛错只坏自己一格）。
- **边界**：面板不得自行 `fetch`、不得引用外链、不得写 `runs/`、只能渲染。
- **落点**：`examples/visualization_consumer/`（`shell.html`、`host.js`、`panels.json`、`panels/*.js`、`verify_panels.py`）。
- **验收**：
  1. `panels.json` 已在 `docs/68` 与 `SCHEMAS` 登记；
  2. 删除任一 `panels/*.js` 后宿主仍能启动并显示其它面板；
  3. `verify_panels.py` 能断言：面板文件存在、只声明不请求、无 `http(s)://`、无 `method:`、无写盘 API；
  4. 面板数量从 1 增加到 N 不需要改宿主。

## 4. 需要项目所有者批准的冻结面改动

| 需求 | 冻结文件 | 批准前可做 | 批准后才能做 |
| --- | --- | --- | --- |
| R5 | `runtime.py` | 外置意图文件并标注"未入包" | 写 manifest 的 `intent` 键 |
| R2 | 无（已改判为显示层推导） | 显示"未声明" | 给 run 加 `analysis` 字段（**本期不做**） |
| R4 / R6 | 无（已改判为消费方推导） | 读 `manifest.recovery`、`dirty.DirtyAnalysis` | — |
| R7 | — | —（已撤回） | — |
| R1 / R3 / R8 | 无 | 全部 | — |

**更正（2026-10-30）：** 原表把 R4/R6 归入"需批准 `runtime.py`"。核实后两者都不需要：resume 的
`recovery.source.run_id` 已在 manifest 里（`runtime.py:1718`），`DirtyAnalysis` 已带 `paths` /
`paths_by_category`（`dirty.py:14-30`）。因此本期需要项目所有者批准的冻结面改动只剩 **R5 一项**。

**没有批准就不要动**：`runtime.py` 的任何写入体、`contracts.py` 的任何字段。

## 5. 总体验收（可直接执行）

```powershell
python -m pytest -q tests/test_visualization_consumer_demo.py
python -m pytest -q tests/test_full_module_scan.py
python -m pytest -q --basetemp .tmp\pytest-all
pe-sim list-runs
```

手工三步：

1. 打开宿主 → 任一 run → 六个内置面板均有内容或显式"未接入"；
2. 加一个自定义面板（只声明 `needs:["trace"]`）→ 刷新后出现，删掉文件后消失，宿主不报错；
3. 启动一次 `pe-sim run ... --progress`（由启动器监管）→ 实时块显示进度，取消后变为 `INCOMPLETE`。

## 6. 不要做（本期明确排除）

- 不做插件市场、不做远程加载、不做沙箱（`docs/24` §7、`docs/52`）。
- 不做 SSE/WebSocket 推送（`docs/24` §7）。
- 不做拓扑编辑、网表编写、图标目录、自动布局（`docs/24` §7）。
- 不把证据等级 / 包摘要放在首屏；它们退到详情页折叠区。
- 不用"看起来更好"的合成状态；每个显示值必须能指回字段。
- 不为了通过验收而修改既有测试；只能新增断言。

---

## 7. 修正与补齐记录（实施第 1 轮，2026-10-30）

### 7.1 状态

| 项 | 状态 | 落地形态 |
| --- | --- | --- |
| R1 活动监看 | **已落地** | `launch.py --run-config` 监管子进程；`GET /live.json`；页面实时块 |
| R2 分析类型 | **已落地（降级）** | 详情页一行「未声明」，不做独立面板 |
| R3 派生指标 | **未实施** | 理由见 §7.6；公式与验收已定义 |
| R4 血缘 | **改判 + 未实施** | 改由消费方读 `manifest.recovery`；缺 manifest 读取路径 |
| R5 意图 | **未实施** | 需批准（唯一剩下的冻结面改动） |
| R6 授权可见性 | **改判 + 未实施** | 改由消费方调 `analyze_git_worktree()`；缺数据路径 |
| R7 控制框图 | **撤回** | 无生产者，不进本期 |
| R8 面板扩展点 | **已落地** | `panels.json` + 宿主挂载/隔离 + `verify_panels.py` |

### 7.2 对 §2 事实表的三处更正

1. **R3 的资格门比原文估计更严。** `postprocess.load_run_evidence` 要求 `status == QUALIFIED`、
   工作区 clean、provenance known、qualification/safety 均 passed（`postprocess.py:258-300`）。
   因此「对已发布 run 计算」实际只对 qualified 的 run 成立；demo 的 `functional` 与失败 run 一律被拒。
   本条直接导致 R3 本期未实施（§7.6）。
2. **R4 的 `parent_run_id` 已经存在。** manifest 顶层 `recovery` 记录了 resume 来源（`runtime.py:1718`），
   消费者直接读即可；study 点位已体现在 `points/<label>/` 路径上；`baseline_commit` 无需存储，
   用 `manifest.source_commit` 与用户指定的基线做 diff 即可。
3. **R6 不需要动冻结面。** `DirtyAnalysis` 已带 `paths` 与 `paths_by_category`（`dirty.py:14-30`）。
   真正的风险不是实现，而是**归因**：Git 工作区描述的是「仓库现在有多脏」，不是「这次 run 期间改了什么」。
   若将来展示，必须同时显示 `working_tree_status` 与 `source_commit` 作为上下文。

### 7.3 R1 的实现约束（已写进代码与测试）

- 文档标记是 `demo-live-v1`，`is_evidence` 恒为 `false`，`launch.py` 全文不含 `pe-sim.` 前缀；
  未启用监管时 `/live.json` 返回 **404**，而不是伪造一个 `active: false` 的"观测结果"。
- 终态**只**来自 `run.finished`：事件没有 `status` 时记为 `finished_without_status`；
  子进程退出但没收到结束事件时记 `terminal: "process_exit"`，绝不推测状态。
- `sequence` 是排序键：重复/回放的行不得让进度条倒退；非法行计入 `skipped_lines` 且不改状态。
- **已知口径**：`ProgressPolicy.interval_s` 默认为 1 s（`run_events.py:131`），所以很短的运行只有
  `run.started` 与 `run.finished` 两条事件，界面必须说明这一点，不能看起来像卡住了。

### 7.4 R8 的实现形态（与 §3 R8 原文的两处差异）

1. **标记改为 `demo-panels-v1`**，不采用原文的 `pe-sim.visualization-panels.v1`，也不进 `docs/68`
   与 `SCHEMAS`。理由：它是示例宿主的配置，不是内核信封；登记一个既无生产者、也无包外消费者的标签，
   会变成「没人产出的承诺」（`docs/68` §4 的登记原则）。若将来宿主升级为受支持的产品面，再提升为契约。
2. **「恰好六个 section」的断言被替换，不是删除。** 新断言要求：`demo.html` 只有唯一挂载点
   `#panel-rack`、不得含任何硬编码面板；`panels.json` 是六个内置面板的唯一来源；宿主必须支持
   动态 `import()` 与逐格 `try/catch`；`verify_panels.py` 必须接受正确清单并被四类坏清单拒绝。
   旧断言只需复制六段 HTML 即可通过，比新断言弱。

### 7.5 R2 的降级形态

只在「来源与身份」面板加一行：`分析类型：未声明（run 文档无该字段）`，并注明 C1–C10 是 `docs/60`
的工况编号、其覆盖状态未随包导出。**不显示任何猜测类型**，也不把编号当成能力声明。

### 7.6 为什么 R3 没有在本轮实施

R3 不是显示层加法，而是**证据面的改动**：

- 它要给 `default_metric_registry()` 增加四个指标，从而改变 Stage 10 受限切片的导出内容与
  registry 指纹（`docs/21`），需要一次针对「派生指标进入导出」的评审，不适合混在 UI 轮里；
- 纹波适用性是真问题：kernel 路线每个控制窗口只有一个样本，`derive.py` 自己就声明
  `limits.boundary_samples_only = true`（`derive.py:609-610`）。用这种记录算峰峰值，得到的不是纹波而是混叠。

已确定的实施形态（下一轮直接照做）：新增 `src/pe_sim/step_response.py` 作为**唯一公式来源**；
`overshoot` 优先用 `vref` 并在结果里记 `basis: "vref"|"final"`；`settling_time` 定义写死为
「最后一次离开 ±2% 带至 t_end」并带 `band`；`ripple_pp` 在不满足 `2·fsample > fsw` 时返回
`value=null, reason="sampled_below_nyquist"`；所有未定义值返回 `reason` 而不是数字；
并用 `evidence_gate: "qualified_only"|"none"` 区分两个入口，避免第二口径。

### 7.7 R4 / R6 为什么改为消费方推导、且本轮未做

两者都不需要冻结面改动（§7.2），但都需要 demo 读到 **manifest** 或 **Git**；而当前只读服务只暴露
三契约 + 参数视图 + review。`docs/70` §12.4 的裁定明确禁止在 I1 里为服务新增路由。
因此它们留到「有 manifest 读取路径」的增量，形态是消费方推导，不是写回。

### 7.8 验收证据

```text
pytest -q tests/test_visualization_consumer_demo.py   -> 57 passed
pytest -q tests/test_full_module_scan.py              -> 15 passed
python examples/visualization_consumer/verify_panels.py -> OK ... 6 panels, marker demo-panels-v1
```

真实事件流校验（用 `launch.py` 同一子进程方式折叠）：`run.started` + `run.finished(QUALIFIED)`
→ `{status: "QUALIFIED", terminal: "event", skipped_lines: 0}`。

# 可视化工作移交（v1）

**Status:** 移交与工作令记录。它记录事实、边界、待决事项与验收要求；它不是范围批准，不是发布批准，也不是任何仿真结论。

**读者：** 接手可视化实现的 agent，以及监督它的人。

**写法约定：** 凡写「今天会发生什么」的地方，都是本人在 v0.26.1 基线上亲自核实过、并给出文件行号的行为；凡写「尚未决定」的地方，都明确标注，不允许接手者自行当作已批准。

---

## 1. 开工前先自己确认基线

不要相信本文件的数字，自己复现一遍：

```powershell
git log --oneline -1
git status --short --branch
pe-sim --version
python -m pytest -q --basetemp .tmp\pytest-baseline
```

- 本文件写于 **v0.26.1**（提交 `4330c48`，内容是修复一个只在 Python 3.11 上失败的 dataclass 默认值）。
- 如果 `origin/main` 仍是 `2d9238f`（v0.26.0），说明 `4330c48` **尚未推送**；以远端实际状态为准，并在你的开工记录里写清楚你基于哪个提交工作。
- 跳过项来自可选后端（ngspice / PySpice），数量随本机安装变化，不要写死：无 ngspice 时出现跳过属于健康基线，不是失败。参考实测（2026-10-30，Python 3.13.2，`.venv`，`python -m pytest -q --basetemp .tmp\pytest-baseline`）：`2060 passed, 31 skipped`。
- 本文件撰写时自身尚未提交（`git status` 里是未跟踪文件），工作树另有未提交改动（`docs/62_project_handover_v1.md`、`docs/releases/v0.21.0.md` 已修改）。开工记录里写清「基于哪个提交」与「树是否干净」两项。
- **不要在跑全量测试的同时编辑仓库**：`tests/test_full_flow_check.py` 会读源码与文档。

---

## 2. 现状：可视化面已经有什么、被撤回的是什么

已经存在并被测试覆盖：

| 面                 | 位置                                                                                                                                                                                | 说明                                                                                             |
| ------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------ |
| 三个渲染器中立契约 | `src/pe_sim/topology.py`、`traces.py`、`outputs.py`                                                                                                                           | 拓扑图、时间序列、结构化输出；都带版本与校验                                                     |
| 离线导出器         | `pe-sim export-workbench <run_dir> <destination>`                                                                                                                                 | 校验发布状态与包摘要后，把一次**已发布**运行投影成三份 JSON（离线产物；服务不依赖它）                                |
| 回环只读服务       | `pe-sim visualization-serve --runs-root <dir>`                                                                                                                                    | `GET /health`、`/v1/discovery`、`/v1/runs/<id>/{topology,trace,outputs,parameters,review}`；`--host` 缺省 `127.0.0.1`、`--port` 缺省 `8765`（`cli.py:1050-1051`）；响应由 run 包**现场构造**（`load_run_workbench_contracts()`），**不读**导出器写的三份 JSON |
| 示例消费者         | `examples/visualization_consumer/`                                                                                                                                                | `index.html`（无依赖浏览器页）+ `verify_contracts.py`（stdlib 校验脚本）                     |
| 测试               | `tests/test_visualization_service*.py`、`test_workbench_export*.py`、`test_stage11_minimum_core.py`、`test_topology_descriptor.py`、`test_traces.py`、`test_outputs.py` | 覆盖完整性、路径穿越、CORS、只读、篡改拒绝                                                       |

**已被撤回、不要偷偷恢复的东西**：`workbench/`（TypeScript/Vite 应用）、`tests/test_workbench_browser_e2e.py`、与浏览器相关的发布门、`docs/30`。撤回原因是"渲染出来不可用且明显损坏"，而且**当时没有任何测试能发现这一点**（`docs/38_kernel_review_v0.md` §1）。恢复其中任何一项都需要先有新的决定，不是顺手加回来。

---

## 3. 需求：核心（C）与扩展（E）

需求来源是项目所有者 2026-10-30 的口述：**核心**是"agent 大量介入、产生海量待交互信息时，人能快速确认 agent 在干什么、仿真关键信息与进度、结果关键波形"；**扩展**是"类似 Simulink 的可视化仿真交互工作台"。

### 3.1 核心（优先，且必须可复核）

| 编号         | 需求                                                                  | 判断依据                                                                                |
| ------------ | --------------------------------------------------------------------- | --------------------------------------------------------------------------------------- |
| **C1** | 运行舰队视图：列出全部 run 及其状态，按"可引用 / 仅探索 / 不可用"分组 | 数据源 `pe-sim list-runs`（`pe-sim.run-catalog.v1`）已存在，含失败与 `INCOMPLETE` |
| **C2** | 单次运行详情：证据等级、参数来源、检查结论、拓扑、波形、校验/警告历史 | `review-run` 的八段、`parameter-view`、`claim`、三个契约已存在                    |
| **C3** | 活动监看：正在跑的 run 的进度与取消                                   | 官方进度源是 stderr 上的 `pe-sim.run-event.v1`；隐藏 staging 目录里另有可读的中间态（`.run-state.json=RUNNING`、`checkpoints/step-*.json`），但 `list-runs` 与 discovery 都看不到（§7.3）                      |
| **R1** | 通用 `samples.json` 读取器：兼容多套命名，缺单位时显式标"单位未知"  | 多套命名来源见 §7.2                                                                        |
| **R2** | `pe-sim.topology-samples.v1` 读取器：作者电路的开关级波形           | 该信封已存在（`cli.py:97`），但没有单位、未进服务                                     |
| **U1** | 让 `simulate-topology --output-dir` 的 `samples.json` 带上单位    | 写出体在 `cli.py`（`src/pe_sim/cli.py:619-632`）：只改这里不动冻结面；要让 Runner 把单位写进样本行就动 `runtime.py`（冻结面，见 §7.1）；新增键不提升版本（`docs/68` §4）                          |

**核心的一句话判据**：界面上每个颜色、徽章、数字，都必须能回答"它来自哪个契约字段"。回答不出来的，不许画。

### 3.2 扩展（按代价排序，不是按吸引力排序）

| 编号         | 项                                                                       | 状态                                                                                          |
| ------------ | ------------------------------------------------------------------------ | --------------------------------------------------------------------------------------------- |
| **E1** | 参数面板（表单 → 生成/编辑 `config.json` → `validate` → `run`） | 今天 `docs/48` §5 明确"没有交互式参数编辑器"；需先改文档                                   |
| **E3** | 启停控制                                                                 | 启停已有 80%：G-B 已达成（start / observe progress / cancel）。**没有 pause**           |
| **E2** | 可视化搭建（拖拽生成拓扑）                                               | `docs/24` §7 把 topology editing / 图标库 / 自动布局列为非目标；需先改文档并自带符号资产   |
| **E4** | 内嵌 agent 对话窗口                                                      | **属于另一个产品**：仓库只承认 agent 是外部契约消费者，`src/` 里没有任何 agent 运行时 |
| **E5** | 写回 / 变更授权面板                                                      | `docs/52` "Not implemented"、T7 仍 planned、沙箱被明确排除在内核职责外                      |
| **E6** | 实时流（SSE/WebSocket）                                                  | `docs/24` §7 列为非目标；stderr 事件流已经够用，不建议做                                   |

**建议的替代**：E4 不做内嵌窗口，改为"交接卡片"——把当前 run 的契约摘要生成一段可粘贴给外部 agent 的 prompt。成本近乎为零，且不越过任何边界。

---

## 4. 边界（五类）

### 4.1 权限

- 只消费三个契约与只读摘要；**不得持有 Runner、Plant、Controller 或变更权限**（`docs/38` §1）。
- 不得写 `runs/<run_id>`、不得**在进程内**启动或持有 Runner、不得反射内部对象、不得把显示当作硬件批准（`docs/26` §5）。监管 `pe-sim run --progress` **子进程**并只读其 stderr 事件是允许的（§7.3 已列为 C3 的一条路）。
- 服务只绑回环、只 GET、无凭据；写方法返回 `405 read_only`（`visualization_service.py:138-162`）。

### 4.2 范围

- 不做拓扑编辑、网表编写、图标库、自动布局（`docs/24` §7）。
- 不做 REST/SSE/WebSocket 实时投递、不做远程/多用户 API、不做插件市场或任意第三方代码执行（`docs/24` §7、`docs/26` §5）。**受控面板扩展点**已由 D2 批准，边界见 §12.6 与 `docs/71`。
- **不得承诺三相逆变器视图**：作者层还表达不了逐腿占空比与任意栅极时序（`docs/69` §4.1/§4.5）。界面补不了内核缺口。

### 4.3 工具链

- 不引入 Node/npm/Playwright。引入它们会触发发布工具链文档与发布门重跑（`docs/28` §…、`docs/29`）。
- 目标形态：**stdlib + 静态 HTML/JS，零新增运行时依赖**。

### 4.4 治理

- 新增信封锁必须同时进 `docs/68` 与 `tests/test_full_module_scan.py` 的 `SCHEMAS`；新增能力 id 必须进 `docs/49`；新增公共名字只进 `EXPERIMENTAL_API`。
- 任何"让界面改变机器状态"的能力都属于产品阶段（`docs/52`、`docs/57` §3 N8），内核不做沙箱。

### 4.5 证据

- 图不是电气真值；`symbol_id` 与摆放不得改变组件身份、端口、净、参数（`docs/26` §5）。
- 探索性证据不得因为"画得好看"变成 comparable（`docs/24` §5、`docs/51`）。
- **停止并询问**：任何需要修改 `runtime.py` / `contracts.py` / `runner/**` / `topology.py` / `public_api.py` / `src/pe_sim/reviewed_identities.json` 的方案。

---

## 5. 必须优先完成的四份文档决定

这四件事都在写代码之前，因为其中三件是"把非目标改成目标"，第四件决定界面能不能被信任。

### D1（必须）可视化范围与验收文档

- **为什么**：`docs/24` §7 原文要求"未来的浏览器/桌面 workbench 仍需自己的范围与验收评审"；`docs/27` 要求把被排除项加回来时提供来源/许可审查、接口与验收证据、显式修订矩阵。
- **写在哪**：建议独立成文（例如 `docs/71_visualization_scope_v1.md`），在 `docs/24` §7 加一行指针。理由：`docs/24` 是"契约 + 非目标"记录，验收细节塞进去会让它变长且难被单独执行。
- **必须回答**：消费什么/不消费什么；每个界面元素映射到哪个契约字段；机械验收清单（见 §8）；错误态与空态；规模上限与抽稀声明；只读保证如何被证明；失败 run 如何呈现。
- **谁批准**：项目所有者（人）。接手 agent 不得自行宣布范围。

### D2（仅当要做 E1/E2）解除两条非目标

- `docs/24` §7 的 "topology editing, netlist authoring, an icon catalog, automatic layout"；
- `docs/69` §6.2 的"项目不提供浏览器界面或桌面 UI"（该文件结尾自己写明：某项被实现后应从文件里删掉，而不是留成过时说法）。
- **修订时必须同时写死**：编辑器只产出新的描述符 JSON，不写回内核对象、不写回已发布包；符号资产由消费方提供；`symbol_id` 是提示不是真理；AI 不得直接改电路语义（`docs/24` §6）。

### D3（仅当要做 E1）修订参数编辑的表述

- `docs/48` §5 原文："No interactive parameter editor. Editing `config.json` and re-running `pe-sim run <config.json>` is the supported path."
- **修订时必须保留**：编辑不产生证据；不修改已发布包；不替代 `validate`；`parameter_confirmation` 仍只是"某个具名审阅者看过这些值"的留痕，不是批准。

### D4（建议写进 D1）一条产品原则

> 界面上任何颜色、徽章、状态文本，都必须映射到一个已存在的契约字段；不得新增"看起来更好"的合成状态。

这条把 §7.4 那五个误读陷阱变成可测规则。出处：`docs/26` §5、`docs/40`、`docs/51`、`docs/24` §5。

---

## 6. 可消费的数据面（开发主料）

| 信封标签                            | 产出者                                                  | 回答什么问题                                                                                                                  | 限制 / 门                                                    |
| ----------------------------------- | ------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------ |
| `pe-sim.run-catalog.v1`           | `pe-sim list-runs`                                    | 有哪些 run、各自状态/证据等级/装置                                                                                            | 看不到"正在跑"的 run（见 §7.3）                             |
| `pe-sim.run-inspection.v1`        | `pe-sim inspect-run`                                  | 包完整性、样本统计、claim、可信度                                                                                             | 只要包完整即可读，**不要求 run 成功**                  |
| `pe-sim.run-review.v1`            | `pe-sim review-run` / `GET /v1/runs/<id>/review`    | 八段固定结构：integrity / claim / parameters / checks / signals / provenance / artifacts / limitations；有 text/markdown/json | 需要 `inspect_run().status == "ok"`                        |
| `pe-sim.parameter-view.v1`        | `pe-sim parameter-view` / 服务 `parameters`         | 参数值、单位、来源、区间                                                                                                      | `range` 恒为 `null`（`parameter_view.py:139`）；`unit` 只来自 applicability findings 或 `timebase` 单位（`:99`）；测试夹具 19 个参数中 12 个无单位、17 个无 source（`tests/test_parameter_view.py:412-415`）                                    |
| `pe-sim.claim.v1`                 | 无 `claim` 子命令；纯函数 `derive_claim`（`src/pe_sim/claim.py:198`），经 `inspect-run`/`review-run` 摘要的 `claim` 字段暴露（`src/pe_sim/inspection.py:339`）                              | 这次运行允许/不允许声称什么                                                                                                   | 无门，最容易接                                               |
| `topology-descriptor-v1`          | 三个契约之一                                            | 元件、端口、净、参数、指纹                                                                                                    | **无坐标**；作者文档 `symbol_id` 为空                |
| trace `1.0`                       | `export-workbench` → `trace.json`；服务 `trace` **不读**导出物，由 run 包现场构造（`src/pe_sim/workbench_export.py:358`）——服务不需要先导出 | 带单位的时间序列                                                                                                              | 只有 3 个逻辑信号；只有成功且可导出的 run                    |
| outputs `1.0`                     | 同上 → `outputs.json`；服务 `outputs` 同样现场构造，不读 `outputs.json`             | scalar / series / **table**（table 目前无人产出）                                                                       | 同上                                                         |
| `pe-sim.topology-validation.v1`   | `pe-sim validate-topology`                            | `ok`、错误/警告计数、`issues[].{code,path,message,expected,suggestion,severity}`                                          | **最适合渲染成"修复指引"**；`ok:true` 只代表零 error |
| `pe-sim.topology-simulation.v1`   | `pe-sim simulate-topology --output-dir`               | 编译摘要、网表摘要、`gate_first_rise_s`、每信号 min/max/last、`waveform_metrics`、`evidence_level: limited`             | 单次恒 duty，只做 inspection                                 |
| `pe-sim.topology-samples.v1`      | 同上 → `samples.json`                                | `time_s` + `signals{v(..),i(..)}`，开关级波形                                                                             | **无单位**；2 000 000 点上限                           |
| `pe-sim.waveform-metrics.v1`      | 由 `simulate-topology` 内嵌                           | 每信号 min/max/mean/rms/`peak_to_peak`/last（键名是 `peak_to_peak`，不是 `pp`，`waveform_metrics.py:95-104`）、功率与效率、观测占空比                                                                       | 需要内存里的描述符与波形表，只有该命令能给；`efficiency` 在窗口不是工作点或输入功率不完整时为 `{"value": null, "reason": ...}`（`waveform_metrics.py:493-527`）——`null` 是设计行为，不是缺陷                   |
| `pe-sim.run-event.v1`             | `pe-sim run --progress`（stderr JSON Lines）          | `run.started` / `run.progress` / `run.finished`                                                                         | **只走 stderr**；`study` 目前不发进度                |
| `pe-sim.rejection.v1`             | 所有拒绝路径                                            | `error_code` + `error` + 附加字段                                                                                         | 只有 6 个通用码；细节在额外字段里                            |
| `pe-sim.visualization-service.v1` | 服务自身                                                | 错误信封、discovery                                                                                                           | discovery 每项只有 `run_id`/两个摘要/`planes`            |

---

## 7. 已核实的坑（不要重新踩）

### 7.1 单位在 run 包里被丢掉

`CircuitPlant.observe()` 明明给出了 `measurement_units={"vout": "V", "inductor_current": "A"}`（`src/pe_sim/backends/circuit_plant.py:448`），但 Runner 组装样本行时只把 `obs.measurement` 摊平进 `row`（`src/pe_sim/runtime.py:1002-1004`），`measurement_units` 从未进入样本。

补单位的地方**不止一处**，说"全仓唯一"不成立：

- 导出器把 3 个逻辑信号的单位硬编码在 `SignalDefinition(..., "V"/"A"/"1")`（`src/pe_sim/workbench_export.py:245-247`），另有 5 个 `OutputDefinition(unit=...)`（`:282-286`）；
- Stage 10 指标注册表给 `vout`/`action` 赋单位（`src/pe_sim/postprocess.py:208-211`）；
- 各 plant 自己声明 `measurement_units`：`backends/circuit_plant.py:448`、`backends/ngspice_plants.py:247`、`backends/model_control_buck.py:381`、`lossy_converter.py:282`、`reference_plants.py:284`。

**风险**：这些来源之间没有任何交叉校验。未来给 `samples.json` 补单位，必须先统一到一个来源，而不是再硬编码第四处。

**结论**：画一条波形，画布占 10%，单位与命名归一化占 80%。是否触及冻结面只看单位写在哪一层：只改 `simulate-topology` 的写出体（`src/pe_sim/cli.py:619-632`，当前只写 `schema`/`time_s`/`signals`）加单位键，不动冻结面；要让 Runner 把单位写进样本行，就要动 `runtime.py`（冻结面），必须先问。

### 7.2 至少五处波形命名来源

- `v(<net>)` / `i(<device>)`：编译电路；`v(<net>)` 在 `src/pe_sim/circuit_compile.py:1685`，`i(<device>)` 在 `:1590`/`:1618`/`:1628`，编译函数 `compile_topology` 为 `:1081-1739`；名字小写化；
- `vout` / `inductor_current`：kernel 路线样本行（`src/pe_sim/runtime.py:1002-1004`）；
- `time_s` / `vout` / `inductor_current_a` / `duty` / `vref`：whole-run 路线（`src/pe_sim/backends/whole_run_engine.py:525-531`）；
- `v(vout)` / `i(l1)` / `l1#branch` / `v(sw)`：ngspice 适配器的候选列名（`src/pe_sim/backends/ngspice_plants.py:47-49`）；
- `vout.final` / `inductor_current.response` 等：导出器写出的 outputs 名字（`src/pe_sim/workbench_export.py:282-286`）。

`workbench_export._TRACE_SOURCE_COLUMNS` 只映射其中 3 个逻辑信号，**其余列被静默忽略**。R1 的目标就是"不再静默"——命名来源比原先估计的"三套"更多，归一化层因此更必要，不能靠一张列名映射表收尾。

### 7.3 运行中的 run 对发现接口不可见，但磁盘上有可读状态

所有产物写在隐藏 temp 目录，发布时整目录 `os.replace`：`tempfile.mkdtemp(prefix=f".{run_id}.", dir=self.root)`（`src/pe_sim/artifacts.py:127`）建 staging；`finalize()` 先写 `READY_TO_PUBLISH`（`:159`）再 `os.replace(self.tmp, target)`（`:160`）后另写 `PUBLISHED`。`:140` 只是 state 落盘，**不是发布点**。

因此发现接口看不到它，原因有两条（都成立）：

- 目录名以 `.` 开头：`list-runs` 在 `src/pe_sim/catalog.py:224` 剪枝（`_HIDDEN_PREFIX = "."`，`:32`）；服务的 `_published_runs` 只遍历匹配 `_RUN_ID` 的子目录（`visualization_service.py:23`、`:70`），`.` 开头的名字不匹配；
- 运行期间 staging 里没有 `manifest.json`（`runtime.py:1251` 写入，`:1252` 立即 `finalize()`），`catalog.py:227` 也会跳过它。

**但"发现不了"不等于"读不到"**：运行中的 staging 目录里确实有可读状态——`.run-state.json` 内容为 `RUNNING`、`checkpoints/step-<NNNNNN>.json`（`runtime.py:1878`）、`logs/`。`scan_abandoned_runs()`（`artifacts.py:218`）正是按这个事实工作：它读 staging 的状态并报 `INCOMPLETE`，但不发布、不删除。

C3 因此有**三条**路，其中两条零后端改动：**监管 `pe-sim run --progress` 子进程读 stderr**（官方事件源）；**直接轮询隐藏 staging 目录的 `.run-state.json` / `checkpoints/`**（读的是未经完整性校验的中间态，界面必须显式标注"尚未发布、不得作为证据"）；或新增一个"故意不原子"的运行中状态写入点（必须解释它为什么不算证据，`run_events.py` 开头已有这类措辞的先例）。

### 7.4 五个"看起来绿了"的陷阱

1. `ok: true` 只代表 `error_count == 0`（`src/pe_sim/topology_validation.py:1557`）；warning 不影响它。`checked` 会因文档残缺而变短：文档形状不符时提前返回并写入 "the rest of the document was not examined"（`:381`），`checked` 的每一项要到 `:388`、`:478`、`:556`、`:945`、`:1141`、`:1336`、`:1392` 才登记。
2. 编译成功 ≠ 求解过：`compile_topology` 只渲染网表不求解（`src/pe_sim/circuit_compile.py:1081-1739`；CLI 文案 "without executing it"，`src/pe_sim/cli.py:308-310`）；真正调求解器的是 backend 的 `_solve_window`（`src/pe_sim/backends/circuit_plant.py:809`）（`docs/03`）。
3. run 完成 ≠ 可比证据：非 `RUN_OK`/`QUALIFIED` 时证据等级降为 `diagnostic_only`（`src/pe_sim/runtime.py:1587-1588`；另一分支见 `:1610`）。
4. `inspect-run` 的 `status: ok` 只说明包**自洽**，不等于运行成功：`problems` 还包含非 `PUBLISHED` 状态、artifact 索引与目录不符、样本含非有限值、`time_s` 非单调、metrics 的样本计数与实际不符（`src/pe_sim/inspection.py:335`、`:174-210`）；是否成功另由 `run_status` 报（`docs/48`）。
5. `simulate-topology` 的结果硬编码 `evidence_level: limited`（`cli.py:591-595`）。

### 7.5 服务与导出器的两个盲区

- 失败/未完成的 run **不会**出现在服务里：`_PUBLISHED_STATUSES`（`src/pe_sim/workbench_export.py:28`）先过滤，状态判定在 `:116`，异常被 `continue` 吞掉（`visualization_service.py:72-75`）。恰是人最需要看的时候看不到。
- **严重（不需要界面就能踩到）**：`export-workbench` 只拒绝"目标 = 源 run 目录或在其内部"（`workbench_export.py:330-331`），**不拒绝目标指向另一个已发布 run 目录**。`artifacts.package_digest()`（`src/pe_sim/artifacts.py:286`）把目录内除 `manifest.json`/`.run-state.json` 之外的所有文件纳入摘要，所以目标若是另一个已发布 run，写进去的三份 JSON 会让对方的 `package_sha256` 重算不一致、artifact 索引也对不上——对方随即在 `inspect-run`、服务、导出三处全部被判为篡改包。一条 `pe-sim export-workbench runs/A runs/B` 就够，与是否做界面无关。任何"导出到…"的入口都必须先补这条保护，而且**目前没有任何测试覆盖它**。

### 7.6 两个已确认的小缺陷

- `examples/visualization_consumer/index.html:29` 的默认 `directory` 兜底值仍是 `../../workbench/public/fixtures/run-demo`，该目录已不存在；全包扫描只校验 Markdown 链接，查不到它。
- 文档漂移：`docs/24_stage11_contracts_v1.md:255` 与 `docs/26_stage11_consumer_and_service_v1.md:154` 仍写"只有 buck/boost 可导出"，实际有 9 个注册装置声明了 `topology_exporter`（`src/pe_sim/builtin_plants.py:697-837`）。本条已在 2026-10-30 勘误中就地更正。

---

## 8. 验收：机械断言，不是"看着还行"

上一版 workbench 就是因为"渲染不可用"被撤回，而没有任何测试能发现。D1 文档必须把下面六类断言写成可执行测试（放 `tests/`，随全量套件跑）：

1. **载入与版本**：三份契约的 schema/version 必须匹配；错误版本进入错误态，不得半渲染。
2. **跨面板身份**：trace 与 outputs 的 `run_id`、`manifest_sha256`、`package_sha256` 必须一致；与 discovery 返回值一致。
3. **摘要显示**：拓扑 `fingerprint`、outputs `fingerprint`、两个包摘要必须与源 JSON **精确相等**（可截断显示，但完整值必须可取）。
4. **DOM 结构**：provenance / topology / trace / outputs / error 五个区域必须存在；成功载入后不得残留 loading 或 error 文案。
5. **几何与数值**：每个组件必须有有限坐标且非零尺寸；界面显示的最新值、标量输出、样本数、组件数必须与契约 JSON 精确相等。
6. **只读**：界面发出的请求只能是 GET；不得出现 POST/PUT/DELETE；不得写 `runs/`。

外加负例：**失败 run、篡改包、无单位信号、多相/多腿信号**都必须显式呈现（"不适用"/"单位未知"/逐相位分开），不得静默画成单条曲线。

**注记：本节六条断言保持不变，缺口在可测前提，不在验收标准。**

- 第 5 类要求"每个组件必须有有限坐标且非零尺寸"，但 `TopologyComponent` 只有 `component_id`/`kind`/`ports`/`parameters`/`symbol_id`/`model_id`（`src/pe_sim/topology.py:111-119`），契约里没有坐标；自动布局本身又是 `docs/24` §7 的非目标。这条断言今天没有可断言的输入。
- 第 4 类要靠一次真实渲染才能检查，而本仓库禁用 Node/Playwright（§4.3），没有浏览器就没有可断言的像素级结果。
- 这两条的可测形态须由 D1 重新定义后才可实现，形态本身尚未决定。见 §12.2、§12.3 记录的技术裁定：方向是"确定性生成物 + 结构断言"，不承诺像素级正确。

---

## 9. 建议增量与每档验收

| 增量         | 内容                                                                                        | 验收                                                            |
| ------------ | ------------------------------------------------------------------------------------------- | --------------------------------------------------------------- |
| **I1** | C1 + C2（只读证据阅读器，放 `examples/`），修 §7.6 两个缺陷                              | §8 的 1–6 类断言；同一 fixture 重复渲染结果一致               |
| **I2** | R1 + R2 + U1 + 失败 run 的只读入口                                                          | §7.2 各命名来源各有 fixture 用例；缺单位时显式标注；多相信号逐相位呈现 |
| **I3** | C3 活动监看 + `study` 进度透传（`cli.py:1493` 目前硬编码 `progress=False`，非冻结面） | 取消后 run 变为 `INCOMPLETE`；事件序列不乱序                  |
| **I4** | E1 → E3 → E2（每档先完成 D2/D3）                                                          | 各自的范围文档条目 + 机械断言                                   |

命令：

```powershell
python -m pytest -q --basetemp .tmp\pytest-ui
python -m pytest -q tests/test_full_module_scan.py
pe-sim list-runs --compact
pe-sim review-run .\runs\<run_id> --format text
python examples/visualization_consumer/verify_contracts.py --directory .tmp\visualization-demo
```

---

## 10. 交接给 agent 时的禁止动作

- 不得自行宣布 D1–D4 已决定；不得把"我实现了一个页面"写成"项目提供了 UI"。
- 不得修改冻结面（见 §4.5）而不停下询问。
- 不得新建信封而不登记 `docs/68` + `SCHEMAS`；不得新增 CLI 子命令而不更新文档（全包扫描会失败）。
- 不得把探索性运行的图与可比证据的图用同一种视觉呈现。
- 不得引入 Node/Vite/Playwright 或任何浏览器自动化依赖。
- 不得删改已有测试来让新功能通过；只能新增断言。

---

## 11. 术语速查

| 术语       | 含义                                                                                            |
| ---------- | ----------------------------------------------------------------------------------------------- |
| 三个契约   | `TopologyDescriptor`（拓扑图）、`TraceDataset`（时间序列）、`OutputDataset`（结构化输出） |
| 已发布 run | 目录内 `manifest.json` + `.run-state.json` 均为 PUBLISHED 状态，且摘要可重算一致            |
| 证据等级   | `diagnostic_only` < `functional` < `limited` < `comparable`；不可跳级                   |
| inspection | `simulate-topology` 这类一次性检查，**不是**可比证据                                    |
| 舰队视图   | 一屏看全部 run 及其可引用性（本文件 C1）                                                        |
| 交接卡片   | 把当前 run 的契约摘要生成一段可粘贴给外部 agent 的 prompt（E4 的替代）                          |

---

## 12. 勘误与三项技术裁定（2026-10-30）

本节记录一次内容校验的结果，以及项目所有者就该校验提出的三项技术问题所作的**授权裁定**（所有者于 2026-10-30 明确委托本 agent 代裁）。本节**不是**范围批准：D1–D4 仍未决定，§5 的批准权仍在项目所有者。

### 12.1 勘误范围

只更正事实与行号：§1 的基线数据、§2 的服务参数与数据来源、§3.1 的 C3/R1/U1 措辞、§4.5 的冻结面路径、§6 数据面表的 `claim`/`trace`/`outputs`/`parameter-view`/`waveform-metrics` 五行、§7.1–§7.6 的行号与"唯一"类绝对判断、§8 追加的可测前提注记、§9 的 I2 验收措辞。§4 的边界、§5 的四份文档决定、§10 的禁止动作、§11 的术语表未改动。

相邻漂移一并更正：`docs/24` §7/§9 与 `docs/26` §3.2 的"仅 Buck/Boost 可导出"，以及两处把已撤回的 workbench 写成"仍是独立消费者"或"临时撤回"的过时句。

### 12.2 裁定一：渲染正确性如何被机械验证

**裁定**：不引入 Node/npm/Playwright。渲染结果必须是**确定性生成物**——同一输入产生逐字节相同的 SVG 或 HTML 片段——并以该生成物为断言对象：结构断言用标准库解析，数值断言用字符串精确比对。不承诺像素级正确，也不承诺跨浏览器视觉一致。

**依据**：

1. 工具链约束是硬约束（§4.3、`docs/28`、`docs/29`）；引入浏览器自动化会重开一个已被撤回的决定（`docs/38`），代价大于收益。
2. 项目目的是**读证据**，不是做 UI 工艺。上一版 workbench 的失败模式是"数据映射错了却没人发现"，不是"CSS 难看"；把渲染拆成纯函数后，这个失败模式可被机械捕获。
3. 纯函数渲染同时化解 §8 第 5 类的不可测问题：断言对象从"屏幕像素"变成"函数输出"。

**代价与残留风险**：无法发现纯视觉回归（元素重叠、文字溢出、对比度不足）。这些只能靠人眼抽查；D1 必须把"人眼抽查"写成一次性验收动作并记录，不得声称已被自动覆盖。

### 12.3 裁定二：坐标与布局归谁

**裁定**：坐标**不进契约**。位置由消费者一侧的**确定性展示放置**函数产生：输入是拓扑的有序组件列表与指纹，输出是每个组件的有限坐标与非零尺寸；同一拓扑必须得到同一结果。该函数不参与电气语义，不回写任何契约字段。

**依据**：

1. `TopologyComponent` 没有坐标字段（`src/pe_sim/topology.py:111-119`）；加坐标要动 `topology.py`（冻结面），且会让 `fingerprint` 依赖审美摆放，直接违反"摆放不得改变组件身份、端口、净、参数"（§4.5、`docs/26` §5）。
2. `docs/24` §7 的非目标 "automatic layout" 指自动布线、避让、力导向这类布局求解；固定的行列式放置不属于此列。**边界必须写死**：一旦放置算法引入避让、正交布线或力导向，就落到该非目标上，必须先做 D2。
3. 让布局成为纯函数，才使 §8 第 5 类断言可执行（与裁定一同理）。

### 12.4 裁定三：失败 run 从哪来

**裁定**：I1 不为服务新增路由、不新增信封。舰队视图的**清单来源**是 catalog，**详情来源**是服务：

- 清单：`pe-sim list-runs`（`pe-sim.run-catalog.v1`），它包含失败与 `INCOMPLETE`；
- 详情：`visualization-serve` 的 discovery 与服务路由，只覆盖"可展开"的 run；
- 三分组（可引用 / 仅探索 / 不可用）必须是 catalog 字段的**确定性函数**，映射规则写进 D1；界面不得引入 catalog 里没有的状态词。

**依据**：

1. 服务侧"失败 run 不可见"来自 `_PUBLISHED_STATUSES` 过滤加 `except: continue`（§7.5）。要改就得在服务里加一条不过完整性校验的代码路径，等于给只读服务开第二个信任等级；这需要单独的范围评审，不适合放进 I1。
2. catalog 已经是权威清单来源（§6 第一行），同时承担"哪些 run 存在"与"各自状态"，用它做清单不新增任何契约。
3. 零登记成本：不新增信封、不改 `SCHEMAS`、不改 `docs/68`、不改 `docs/26` 路由表。

**已知残留**：catalog 看不到正在跑的 run（§7.3），所以本裁定不解决 C3；C3 仍是 I3 的独立问题。

### 12.5 仍未决定

- D1（可视化范围与验收文档）：未决定，仍需项目所有者批准。上述三项裁定只固定了 D1 里三个技术选择的实现方向，不构成范围批准。
- D2（解除 `docs/24` §7 / `docs/69` §6.2 的非目标）：**已批准**（见 §12.6），并按 `docs/71` §7.4 落地为本地只读宿主 + `panels.json` 面板清单。
- D3（修订 `docs/48` §5 参数编辑表述）：未决定。
- D4（"每个颜色/徽章映射到契约字段"的产品原则）：未决定是否写入 D1。
- E4 替代方案（交接卡片）：未决定。
- **R3（派生指标进入导出）**：未决定。它会改变 Stage 10 受限切片的导出内容与 registry 指纹（`docs/21`），需要单独评审；形态见 `docs/71` §7.6。
- **R5（manifest `intent`）**：未决定，是 R1–R8 中唯一仍需冻结面批准的项。

### 12.7 第 1 轮实施状态（2026-10-30）

逐项状态、三处事实更正与验收证据见 `docs/71` §7。摘要：R1 / R2 / R8 已落地；R3 未实施（证据面改动，需单独评审）；
R4 / R6 改判为消费方推导且待有 manifest/Git 读取路径；R5 需批准；R7 撤回（无生产者）。

### 12.6 决定 D2（2026-10-30）：受控面板扩展点

项目所有者批准 D2：允许**本地只读宿主 + 操作者提供的面板模块**。规格与逐条验收见 `docs/71_human_visualization_requirements_v1.md`。

**批准了什么**

- `docs/24` §7 不再把"加载本地显示模块"列为非目标（保留"插件市场"与"任意第三方代码执行"为非目标）；
- `docs/69` §6.2 从"不提供任何浏览器/桌面 UI"改为"不提供桌面应用/编辑器/实时推送/插件市场"，同时明确允许本地只读宿主。

**没有批准什么**

- 插件市场、远程加载、沙箱、任意第三方代码执行（仍是非目标）；
- SSE/WebSocket 推送、远程/多用户 API（仍是非目标）；
- 任何冻结面改动。`docs/71` §4 列出的 `runtime.py` / `contracts.py` 改动**仍需单独批准**。

**面板的权限上限**：与外部消费者相同——不持有 Runner/Plant/Controller，不写 `runs/`，不自行发起请求，只能渲染宿主交给它的数据；宿主必须做单面板异常隔离。

**残留冲突已处理**：§4.1"不得启动 Runner"与 §7.3"监管子进程"两句冲突，已改为"不得在进程内启动或持有 Runner；监管 CLI 子进程并只读其 stderr 事件是允许的"。

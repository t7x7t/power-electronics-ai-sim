# 缺口收敛执行计划（v1）

**状态：** `docs/60` 中 26 个未闭合工况的**唯一施工顺序权威**。`docs/43` 仍然决定
架构阶段（A–E）与不变量；本文件只决定"先关哪个缺口、什么时候算关上"。
`docs/60` 是台账（哪些行存在、每行什么状态），不是顺序。
`tests/test_coverage_matrix.py` 同时校验两份文件：计划不能漏掉任何未闭合行，
台账不能在被证明之前变成 `covered`。

## 1. 判定：什么叫"补齐"

1. 每一行最终只有两种归宿：`covered` 或 `out_of_scope`（后者必须已有文档决定）。
   不允许长期停留在 `expressible` / `kernel_gap`——那只是"没有排期"的委婉说法。
2. `covered` 的判据沿用 `docs/60` §1：必须给出 `tests/...` 引用，且该文件真的包含测试。
3. 达不到 `comparable` 的行必须在 §2.3 登记**证据上限**，且落地测试必须断言上限被记录，
   而不只是断言功能可用。一个"能跑但没说清楚自己有多可信"的工况不算补齐。
4. 在本计划 S9 收口前，**不新增产品级功能面**（可视化扩展、Workbench 新面板、
   浏览器端新交互）。已存在的可视化与导出保持可用，但不作为交付重点。
5. 任何一行被关闭时，`docs/60` 的状态、证据列与 §2.2 闭包分类必须同时更新；
   不允许"代码合了、台账没动"。

## 2. 阶段与顺序

| 阶段 | 目标版本 | 关闭的行 | 关闭层 | 依赖 | 门禁 |
| --- | --- | --- | --- | --- | --- |
| S0 | 0.9.8 | —（建立计划本身与守卫） | 文档 + 测试 | — | 本文件与 `test_coverage_matrix.py` 一致 |
| S1 | 0.10.0 | —（已完成：可表达性证明） | `test-only` | S0 | 已交付：5 行变 `covered`，无产品代码变更 |
| S2 | 0.10.1 | —（已完成：路由声明与单一派生） | 声明 + 派生 | S1 | 已交付：全量测试只增不减，无已有断言被放宽 |
| S3 | 0.11.0 | A6 | `contract`（ADR 0006） | S2 | 初始状态载荷可验证；schema 迁移向后兼容 |
| S4 | 0.12.0 | A4 D2 D3 | `kernel-impl` | S3 | **维护者 pin 复核**（§5）+ ADR 0004 步骤 2 的两条验收 |
| S5 | 0.12.1 | C7 C10 | `postprocess` | S0 | 派生工件有父链接、原始包不变、尺寸有上限 |
| S6 | 0.13.0 | B2 B9 A7 | `controller` seam | S4（A7 依赖亚窗口变更点） | pi/pid 行为被现有测试钉死；JSON 可选新控制器；保护上限已登记 |
| S7 | 0.14.0 | A10 C3 C9 | `study` seam | S1 | 聚合不自动继承 `comparable`；每点保留自己的证据 |
| S8 | 0.15.0 | C4 C5 C6 | `capability` seam | S7 | 缺失能力 = 不可用（不是 `limited`）；AC/阻抗方法必须自报 |
| S9 | 0.16.0 | A8 A9 A11 B7 B8 B3 | `adapter` 扩面 | S4 | T1 仍然成立：新能力是注册，不是内核分支 |
| S10 | 0.17.0 | —（收口与发布） | 发布 | S1–S9 | `docs/60` 无 `kernel_gap` / `expressible` 残留 |

### 2.1 为什么是这个顺序

- **S1 先做**：它用最低成本验证"架构可表达"这一判断。如果 test-only 行里有任何一行
  其实需要产品代码，说明 `docs/60` 的分类错了，后面的排期都要改。它是计划的自检。
  S1 的第一次执行已经发现 A6 属此类，因此把它改判为契约行并单独拆出 S3。
- **S2 早于 S4**：`control_location` / `event_granularity` 必须先成为声明，S4 才能删掉
  整数倍限制而不制造隐式行为。S2 是行为中性的，可以安全地单独发布。
- **S3 不依赖 pin 门**：A6 是纯契约扩展（初始状态载荷），不碰被审 solve path，也不改
  证据上限。把它放在 pin 门之前，保证"维护者未授权期间仍有一件确定能交付的事"。
- **S4 早于 S6/S9**：A4（窗口内负载阶跃）是 `docs/60` §3 登记的静默缺口，也是 A7（短路）
  与 B3（逐周期比较器）的前置。它排在 study/capability 之前，因为它是正确性问题，
  不是能力扩展问题。
- **S5 可在 S4 阻塞期间并行**：它只消费已有样本，不改求解路径，不碰 pin。
- **S6 早于 S7**：控制器 seam 关闭 3 行且不依赖 study；study 需要一批可运行的运行，
  控制器先稳定可以让 study 的验收更简单。
- **S7 早于 S8**：C4/C5 的 AC 与阻抗是"在若干运行/工作点上提问"，先有 study 编排，
  能力 seam 才有调用者；否则会先写一个没有入口的能力。
- **S9 最后**：适配器扩面依赖 S4 的连续事件才能诚实声明 `continuous`；它关闭的行最多，
  但每一行都能独立验收，适合在协议稳定后批量交付。

### 2.2 每阶段的交付物与验收

**S0 — 计划与守卫**

- 交付：本文件；`docs/60` §4 改为指向本文件；`tests/test_coverage_matrix.py` 新增
  "计划覆盖每一个未闭合行、且每个行只出现一次"的检查。
- 验收：`pytest tests/test_coverage_matrix.py` 通过；全量测试计数不变。

**S1 — 可表达性证明（A3 A5 B4 B5 B6）**

- 交付：Runner 级验收测试（不是适配器单元测试）：
  - A3 负载阶跃在窗口边界由 `input_schedule` 调度；
  - A5 vref 斜坡逐窗口调度；
  - B4 采样/ZOH/计算延迟在控制周期上可观测；
  - B5 前馈使用 `command` 中的 `vref`；
  - B6 级联双环在控制器内部状态中可表达。
- 验收：每条测试断言一个可观测的运行结果（`vout` 轨迹、duty 序列或 manifest 字段），
  不是只断言"没有异常"。

**S2 — 路由声明（ADR 0004 步骤 1）**

- 交付：`PlantRegistration.control_location` / `event_granularity`（带默认值）；
  `time_model` 中唯一派生函数（吃整个能力记录）；四个内置植物的现状声明；
  manifest 上报；`whole_run + kernel` 构造期拒绝。
- 验收：全量测试不变；新增测试证明**没有任何消费者在本地计算 cap**。

**S3 — 初始状态载荷（A6）**

- 交付：ADR 0006；`InitialState` 增加可选状态载荷（有限数、按字母序、条数上限），
  `to_dict()` 与 schema 一并承载，运行前传给 `plant.reset`，manifest 记录其摘要；
  schema 版本按既有迁移注册表升级，旧文档行为不变。
- 验收：预偏置 5 V 启动的 `vout` 首点接近 5 V 并观测到向目标收敛；不带载荷的旧文档
  产生逐字节相同的 manifest 字段（除版本号）；非法载荷（NaN/inf/非数字）在构造期拒绝。

**S4 — 相位连续与变更点边界（A4 D2 D3）**

- 交付：载波相位参数化（`TD = window_start mod Tsw`）、求解段在下一个变更点结束、
  删除整数倍限制与对应 `uncheckable_rules`；`ngspice-buck` 声明 `continuous`。
- 验收：(a) `control_period_s = 1.5 × Tsw` 可构造并运行，窗口边界处 `vout` 与电感电流
  无跳变；(b) 窗口中点 10 Ω → 5 Ω 负载阶跃的波形与单次直跑 ngspice 在声明容差内一致。
- 门禁：改动落在被审 solve path 上，**必须先有维护者复核记录**（§5）。

**S5 — 派生指标（C7 C10）**

- 交付：单次运行样本上的派生分析（纹波统计、Hann 加窗频谱、THD/PF），
  独立工件 + 父包链接；采样/抽取策略与体积上限。
- 验收：原始包字节不变；派生工件声明父 `package_digest` 与自身证据继承关系；
  超限时失败关闭而不是截断。

**S6 — 控制器 seam（B2 B9 A7）**

- 交付：ADR 0007（控制器注册表 + `controller_id` 从枚举改为模式，schema 迁移）；
  内置 `type2` / `type3`（按控制周期离散，系数记录在 manifest）；
  保护状态机（OCP/OVP/UVLO/hiccup/latch）作为可组合包装；
  故障注入（短路/负载突卸）落到 `input_schedule` 的变更点。
- 验收：JSON 可选 `type2`/`type3` 并发布带控制器身份的 `QUALIFIED` 包；
  `pi`/`pid` 的现有测试逐字不变；保护动作及其延迟出现在 manifest。
- 上限：A7 登记 `protection_latency_is_one_control_period`（§2.3）。

**S7 — Study seam（A10 C3 C9）**

- 交付：`Study` 计划层（`parameter_sweep` / `monte_carlo` / `periodicity`），
  每个点是一次普通运行，结果带每点清单链接；`pe-sim study` 子命令。
- 验收：duty 扫掠生成效率曲线；固定 seed 的 Monte Carlo 可复现；
  buck 的 periodicity 收敛判定可达；聚合结果不自动获得 `comparable`。

**S8 — 模型能力 seam（C4 C5 C6）**

- 交付：可选 `small_signal_model(op)` 与 `loss_model()`；AC 扫频 / Bode / PM / GM；
  输入/输出阻抗与 Middlebrook 判据；效率与损耗分解。
- 验收：未实现能力的植物报告"不可用"而非 `limited`；每个结果记录线性化方法与验证方式；
  实现方式（解析 vs 辨识）出现在工件里。

**S9 — 适配器扩面（A8 A9 A11 B7 B8 B3）**

- 交付：死区/同步整流网表模板；开关级 whole-run 适配器；多相（共享 duty + 内部相位）
  模型；`control_location=model` 的两个适配器（各对应一个 `time_model`）；
  开关级电流模式（比较器在模型内）。
- 验收：每一行一个独立验收；T1 测试仍然通过（新能力零内核改动）。

**S10 — 收口**

- 交付：`docs/60` 全部行落到 `covered` 或 `out_of_scope`；发布说明汇总每个阶段的上限；
  `docs/58` 能力边界同步。
- 验收：`test_coverage_matrix.py` 在 `expressible`/`kernel_gap` 为空时通过；
  全量测试与发布检查通过。

### 2.3 证据上限登记

以下行即使关闭，也不得被当作 `comparable` 证据使用。这是本计划的诚实性约束，
与 `docs/58` 的能力边界同源。

| 行 | 关闭后的上限 | 必须记录的限制码 / 理由 |
| --- | --- | --- |
| A7 | `limited` | `protection_latency_is_one_control_period`：内核侧保护在控制周期上动作 |
| A11 | `limited` | 路线上限 `whole_run`（`post_hoc_validation_only`） |
| B3 | `limited` | 比较器在模型内（`actuator_control_in_model`）；模型自报的逐周期行为未经内核拦截 |
| B8 | `limited` | ADR 0004 派生表：`stepped/model` 与 `whole_run/model` 均为 `limited` |
| B7 | `comparable`（仅共享 duty 共相）；逐相电流环不主张 | `per_phase_current_sharing_not_modelled` |
| C3 | `per-point`；聚合为派生 | `pss_convergence_is_tolerance_based` |
| C4 | `limited`（解析）/ `functional`（辨识） | `linearization_method_is_model_declared` |
| C5 | 同 C4（`limited` / `functional`） | 阻抗由同一线性化导出，不得高于 C4 的上限 |
| C6 | `inherit`（效率）/ `limited`（分解） | `loss_breakdown_is_model_declared` |
| C7 | `inherit` | 派生工件不是新的证据级别 |
| C9 | `per-point`；聚合为派生 | `aggregate_does_not_inherit_comparable` |
| C10 | `inherit` | 同一规则 |
| A10 | `per-point`；聚合为派生 | 同一规则 |

未列出的行（A4 A6 A8 A9 B2 B9 D2 D3）预期可达到各自路线的上限
（`stepped + kernel` 为 `comparable`）。已在本计划中关闭的行仍保留上限要求：
关闭不等于可以超出上限使用。

## 3. 边界约束（实施者不得越过的线）

这些约束对每个子任务都有效，违反其一即视为交付失败，无论测试是否通过。

1. **不改公共稳定面。** `tests/fixtures/public_api.json` 的 `stable` 名单与 `sha256`
   在本计划内保持不变；新能力只能进入 `EXPERIMENTAL_API`（ADR 0005 / I4）。
   任何"顺手"把实验名字提升为稳定名的改动一律拒绝。
2. **不改冻结的绕过面。** `docs/57` §3 的 N1–N8 不新增检查，也不删除声明；
   对抗性绕过报告按 `docs/03` §8 分诊，不在本计划内处理。
3. **不改复核 pin。** `src/pe_sim/reviewed_identities.json` 只能由维护者在 §5 的流程中更新。
4. **不新增模式。** 新能力必须是新的注册、适配器、控制器或计划层；
   不允许给现有适配器加"模式开关"来绕过声明（ADR 0004 反补丁规则 1）。
5. **不本地计算证据上限。** 上限只在 `time_model` 的单一派生点产生（ADR 0004 规则 2）。
6. **不让适配器自我认证。** 适配器可以声明身份与能力，不能声明证据级别（I2 / T5）。
7. **不改台账。** `docs/60` 的状态行由监督者在验收后更新；实施者不得先改状态再写代码。
8. **不做范围外重构。** 每个阶段只允许改动与所关行直接相关的文件；
   格式化、重命名、目录调整不属于任何阶段。
9. **不新增第三方运行时依赖。** 派生指标用标准库或已锁定的 `numpy`。
10. **不降低既有断言。** 现有测试只能增加；如果必须修改，必须在阶段说明里逐条给出理由。

## 4. 委托与监督协议

每个阶段按同一流程执行，监督者与实施者职责分离：

1. **派发**：给出阶段号、要关闭的行、允许改动的文件白名单、必须新增的测试、
   必须更新的文档、以及 §3 的约束清单。
2. **先红后绿**：实施者先写会失败的验收测试并报告失败信息，再实现到通过。
3. **自检**：实施者运行受影响模块的测试与全量测试，报告计数。
4. **交付**：实施者给出改动文件清单、每个行的验收证据、以及任何偏离计划的说明。
5. **复核（监督者）**：检查 `git diff` 是否越出白名单、公共稳定面是否未变、
   记录是否只在 `EXPERIMENTAL_API`、`docs/57` 是否未动、pin 是否未动。
6. **验收（监督者）**：亲自运行全量测试；只有此时才更新 `docs/60` 的状态与证据列，
   并运行 `tests/test_coverage_matrix.py`。
7. **发布**：写 release note（含上限与未做事项）、提升版本、提交、推送、核对远端。
8. **拒绝条件**：任何一条 §3 约束被违反、任何"先改台账后补测试"、任何把
   `limited` 写成 `comparable` 的表述，都应整体退回，而不是局部修补。

## 5. 维护者门（唯一需要人工授权的步骤）

S4 修改 `backends/netlist.py` 与 `backends/ngspice_plants.py`，二者在被审 solve path 上。
身份摘要变化会使 `ngspice-buck` 的 `review_status` 从 `reviewed` 变为 `unreviewed`，
从而把所有 ngspice 运行封顶在 `limited`。因此：

1. 实施者完成代码与验收后，监督者给出改动摘要与新的 `implementation_sha256`。
2. **维护者**确认该摘要并授权更新 `src/pe_sim/reviewed_identities.json`；
   没有这次授权，S4 不得合并，S6 的 A7 与 S9 的 `continuous` 声明也不得开始。
3. 该授权是一次性的、按提交记录的；它不构成对后续改动的预先批准。
4. 如果维护者拒绝或要求拆分，S4 回到"S2/S3 已发布、S4 待定"状态，
   S5/S7/S8 以及 S6 的 B2/B9 可以继续，但 A7 与 S9 阻塞。

## 6. 回退与失败处理

- 每个阶段一个提交；失败即回退到上一提交，不在同一提交里"再试一次"。
- 如果某个阶段发现 `docs/60` 的分类错误（例如 `test-only` 实际需要内核改动），
  停止该阶段，先更新分类与本计划的阶段表，再按新顺序执行。
- 如果某个行无法在预期上限内关闭，只有两个合法选择：降低上限并登记理由，
  或改为 `out_of_scope` 并给出文档决定。不允许静默降级。

## 7. 变更规则

- 本文件的阶段表是唯一顺序来源；`docs/60` §4 不再复制顺序。
- 新增行必须同时进入 `docs/60` 与某个阶段；不允许"待定"阶段。
- 阶段的增删改必须同步 `tests/test_coverage_matrix.py` 中的计划一致性检查。
- 版本号在阶段完成时提升；S0–S10 的版本号是计划值，实际发布以 release note 为准。

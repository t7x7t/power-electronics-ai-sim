# 仿真需求覆盖矩阵（v1）

**状态:** 需求台账，面向使用者与维护者。它不是路线图，也不是承诺；它回答三个问题：
**哪些工况已覆盖、哪些能表达但缺实现、哪些明确不做**，并且每一行都必须能被机器检查。

**为什么需要它:** 本仓库的架构门禁是 `T1–T7`（`docs/43`），它们验证的是**架构性质**，
不是**工况覆盖**。于是"应该覆盖却没人想到"的工况只能靠人读文档发现——`UD-05`（任意
控制/开关比下的相位连续 PWM）长期躺在 backlog 里就是实例。本文件把工况变成可核对的行，
并用 `tests/test_coverage_matrix.py` 保证：**没有测试就不许标 `covered`**。

## 1. 状态词表（封闭）

| 状态 | 含义 | 硬性要求 |
| --- | --- | --- |
| `covered` | 今天可用，且有测试 | 必须给出 `tests/...` 引用，且文件存在并包含测试 |
| `expressible` | 内核机制已够，缺适配器/控制器/测试 | 必须写清缺什么 |
| `kernel_gap` | 需要新的内核能力（声明或分析） | 必须有 backlog id 或 ADR 小节 |
| `out_of_scope` | 已声明不做 | 必须给出文档引用 |

频率取自领域清单：`very-common` / `common` / `specialized`。

## 2. 矩阵

### A. 时域工况与瞬态

| ID | 工况 | 频率 | 内核可表达 | 现状 | 责任方 | 证据 / 缺什么 |
| --- | --- | --- | --- | --- | --- | --- |
| A1 | 恒定 duty/vref 冷启动到稳态 | very-common | yes | covered | adapter | `tests/test_stage15_buck_boost_acceptance.py`, `tests/test_ngspice_real_backend.py` |
| A2 | 输入电压阶跃/跌落（窗口边界） | very-common | yes | covered | adapter | `tests/test_stage15_buck_boost_acceptance.py`（调度 Vin 12 V→9 V） |
| A3 | 负载阶跃/突卸（窗口边界） | very-common | yes | covered | adapter | `tests/test_stage16_expressible_scenarios.py`（Runner 级：command 与 truth 在边界窗口后更新） |
| A4 | 负载阶跃落在控制窗口内部 / 亚开关周期 | very-common | yes | covered | kernel | `tests/test_event_boundaries.py`（窗口中点阶跃；导通区内 0.01% 变更对照；与单窗口分段一致） |
| A5 | 软启动（vref 斜坡） | very-common | yes | covered | controller/adapter | `tests/test_stage16_expressible_scenarios.py`（逐窗口 vref 序列与 duty 单调） |
| A6 | 预偏置启动（输出已预充） | very-common | partial | covered | adapter | `tests/test_initial_state_payload.py`（首个采样为预充值并向目标收敛；未接受键在首次 advance 前失败）；ADR 0006 |
| A7 | 短路/逐周期限流/打嗝恢复 | very-common | partial | covered | controller/adapter | `tests/test_protection.py`（0.2 Ω 短路落在窗口中点；越界后一个控制周期内 trip；打嗝重试）；上限 `protection_latency_is_one_control_period` |
| A8 | 轻载 / DCM / burst | very-common | partial | expressible | adapter | 开关级网表物理上可表现 DCM；平均模型声明 CCM 假设（`uncheckable_rules`）；无检测 |
| A9 | 死区与同步整流时序 | common | yes | expressible | adapter | 当前模板无死区/SR/栅驱动（`docs/31`）；需新网表模板，零内核改动 |
| A10 | 输入/负载/duty 参数扫掠 | very-common | yes | covered | kernel | `tests/test_study_plan.py`（笛卡尔展开、路径白名单、单点文档仍是合法实验文档）、`tests/test_study_cli.py`（端到端扫掠 `controller_config.kp` 与 `plant_config.load_resistance_ohm`，任意数值叶子可为轴）；上限 `per-point`，聚合为派生（`aggregate_does_not_inherit_comparable`） |
| A11 | 长时程高分辨率连续波形（放弃窗口拦截） | common | partial | expressible | adapter | `whole_run` 路线已有（`limited`）；缺开关级整段适配器，见 ADR 0004 §Implementation 3 |

### B. 控制与环路

| ID | 工况 | 频率 | 内核可表达 | 现状 | 责任方 | 证据 / 缺什么 |
| --- | --- | --- | --- | --- | --- | --- |
| B1 | 电压模式 PI/PID（内核控制器） | very-common | yes | covered | kernel | `tests/test_pi_controller.py`, `tests/test_pid_controller.py` |
| B2 | Type II/III 补偿器（内核侧、按控制周期离散） | very-common | yes | covered | kernel | `tests/test_controller_registry.py`（注册表解析、Tustin 系数独立对照、钳位/快照、CLI 端到端）；ADR 0007 |
| B3 | 峰值/平均电流模式（逐周期比较器） | very-common | no | kernel_gap | kernel | 需比较器级事件（A4 的超集）；`UD-01` |
| B4 | 数字环路的采样/ZOH/计算延迟 | very-common | yes | covered | controller | `tests/test_stage16_expressible_scenarios.py`（`sample_offset_s`/`control_period_s`/观测时刻一致） |
| B5 | 前馈（line/load） | common | yes | covered | controller | `tests/test_stage16_expressible_scenarios.py`（零误差时 duty 仅由 `kff_v` 决定，含零前馈对照） |
| B6 | 多环级联（电压外环+电流内环） | very-common | yes | covered | controller | `tests/test_stage16_expressible_scenarios.py`（`state_update` 在下一步可见） |
| B7 | 多相/均流/相位增减 | common | partial | expressible | adapter | 无多相模型；共享 duty + 相位内部管理可覆盖常见共相；逐相电流环属 specialized（ADR 0005） |
| B8 | 控制器放进网表 | common | no | kernel_gap | kernel | ADR 0004（`control_location=model`，上限 `limited`） |
| B9 | 保护状态机（OCP/OVP/UVLO/hiccup/latch） | very-common | yes | covered | controller | `tests/test_protection.py`（OVP/OCP/UVLO、启动掩码、hiccup/latch、快照恢复） |

### C. 分析类型

| ID | 工况 | 频率 | 内核可表达 | 现状 | 责任方 | 证据 / 缺什么 |
| --- | --- | --- | --- | --- | --- | --- |
| C1 | 时域瞬态（开关级/平均） | very-common | yes | covered | kernel+adapter | `tests/test_ngspice_plant_contract.py`, `tests/test_stage15_buck_boost_acceptance.py` |
| C2 | 解析稳态/平衡点判定 | very-common | yes | covered | kernel | `tests/test_stage15_buck_boost_acceptance.py`（`analytical_steady_state` + 稳态规则） |
| C3 | 周期稳态解（PSS） | very-common | yes | covered | kernel | `tests/test_study_cli.py`（averaged buck 上时长扩展至收敛；对末次运行 `samples.json` 独立复算与报告数值一致）；上限 `per-point`、`pss_convergence_is_tolerance_based` |
| C4 | AC 小信号 / 环路增益（Bode、PM、GM） | very-common | yes | covered | kernel | `pe-sim capability`（`pe_sim/capability_analysis.py`）+ `tests/test_capability_cli.py`、`tests/test_ac_analysis.py`；解析 buck/boost 小信号、Tustin 等效回路增益、PM/GM，±180° 平坦相位诚实报 `None`；`GAP-01` 已关闭 |
| C5 | 阻抗扇帚（输入/输出、Middlebrook） | common | yes | covered | kernel | `pe-sim capability`（`impedance` kind）+ `tests/test_capability_cli.py`、`tests/test_impedance_analysis.py`；`Zin`/`Zout` 扇帚与 Middlebrook 幅度筛子（仍**不是**稳定性证明）；`GAP-02` 已关闭 |
| C6 | 效率与损耗分解 | very-common | yes | covered | adapter+kernel | `pe-sim capability`（`loss` kind）+ `tests/test_capability_cli.py`、`tests/test_lossy_converter.py`；声明 `Rds/DCR/Vf/Qoss/Qg` 的 averaged buck + `loss_model()`；内置理想 buck/boost **不声明** loss provider（效率 1.0 会是假声明）；`GAP-03` 已关闭 |
| C7 | 纹波频谱 / THD / PF | very-common | partial | covered | kernel | `tests/test_derived_metrics.py`（Hann 频谱、THD、PF；边界样本上限已声明） |
| C8 | 电热 / 热网络 | common | no | out_of_scope | adapter | `docs/43`（器件/热模型非内核工作）；`no_thermal_state_modelled` |
| C9 | Monte Carlo / 容差 / 最坏情况 | very-common | yes | covered | kernel | `tests/test_study_plan.py`、`tests/test_study_cli.py`（同 seed 逐点可复现；点参数逐点等于 `draws`，且 `draws_sha256` 与独立复算一致）；上限 `per-point`、`aggregate_does_not_inherit_comparable` |
| C10 | 波形导出与派生分析工件 | very-common | partial | covered | kernel | `tests/test_derived_metrics.py`（父包链接、原包不变、体积上限）；窗口内波形仍属 `UD-03` |

### D. 数值与时间尺度

| ID | 工况 | 频率 | 内核可表达 | 现状 | 责任方 | 证据 / 缺什么 |
| --- | --- | --- | --- | --- | --- | --- |
| D1 | 窗口内子步（plant_step_s） | very-common | yes | covered | kernel | `tests/test_runtime_preflight_contracts.py`（窗口端点校验） |
| D2 | 任意控制/开关周期比下的相位连续 PWM | common | yes | covered | kernel | `tests/test_event_boundaries.py`（1.5× 与 1.2×Tsw 连跑均与单次直跑一致）；`UD-05` 关闭 |
| D3 | 事件精确时刻（比较器/二极管过零） | very-common | partial | kernel_gap | kernel | ADR 0004 §3：外部输入变更点已按声明时刻求解（`tests/test_event_boundaries.py`）；比较器/器件级事件随 B3 在 `docs/61` S9 处理 |
| D4 | 中断/恢复、快照等价 | common | yes | covered | kernel | `tests/test_stage5_runner.py`, `tests/test_stage8_failure_recovery.py` |
| D5 | 确定性/可复现与包可复核 | common | yes | covered | kernel | `tests/test_stage9_reproducibility.py`, `tests/test_inspection.py` |

### 2.1 本文件自持的新增缺口登记

以下缺口在 `docs/00` 与 `docs/37` 里**没有**对应条目，先在此登记，避免再次"只在讨论里存在"。
后续应迁入 `docs/37_user_discovery_backlog_v1.md` 并保留编号。

| GAP | 缺口 | 影响行 | 建议归属 |
| --- | --- | --- | --- |
| GAP-01 | AC 小信号 / 环路增益：无 `.ac`、无注入源、无 Bode/相位裕度 | C4 | 内核（新 ADR：频域分析路线） |
| GAP-02 | 阻抗扫描：无输入/输出阻抗与 Middlebrook 判据 | C5 | 内核（依赖 GAP-01） |
| GAP-03 | 效率与损耗分解：无导通/开关/磁性损耗模型与派生指标 | C6 | 适配器提供损耗模型 + 内核提供派生指标 |

**GAP-01..03 已在 0.16.0 由 `pe-sim capability` 关闭**：AC、阻抗/Middlebrook、损耗/效率三类分析均有文档入口、JSON 工件与端到端测试。

### 2.2 按"关闭它需要动哪一层"分类

这决定了架构是否适配：**能否在不改 `PlantAdapter` / `ControllerAdapter` / Runner 循环的前提下
补上这个工况**。词表见 ADR 0005。

| 类别 | 行 | 含义 |
| --- | --- | --- |
| `closed` | A1, A2, A3, A4, A5, A6, A7, A10, B1, B2, B4, B5, B6, B9, C1, C2, C3, C4, C5, C6, C7, C9, C10, D1, D2, D4, D5 | 今天可用且已有测试 |
| `test-only` | —（当前为空：可表达性行已在 0.10.0 闭合） | 今天已能表达，只缺用例 |
| `postprocess` | —（当前为空：派生指标已在 0.12.0 闭合） | 单次运行样本上的派生指标 |
| `adapter` | A8, A9, A11, B7 | 新模型或新网表模板，零内核改动（T1） |
| `controller` | —（当前为空：保护在 0.14.1 闭合） | 新控制器或保护状态机 |
| `study` | —（当前为空：study 计划层已在 0.15.0 闭合） | 需要在单次运行之上编排（ADR 0005 Seam 1） |
| `capability` | —（0.16.0 已由 `pe-sim capability` 闭合） | 需要模型回答 `advance()` 答不了的问题（ADR 0005 Seam 2） |
| `kernel-impl` | B3, D3 | 内核实现改动，**不改协议** |
| `contract` | B8 | 真正需要改契约，且已由 ADR 决定（ADR 0004）；A6（ADR 0006）与 B2（ADR 0007）曾属此类，已关闭并转入 `closed` |
| `never` | C8 | 已声明不做 |

**读数说明:** 35 行里曾有 **3 行**需要改契约（A6、B2 已关闭，B8 待做），2 行需要内核实现但不改协议（B3、D3）；
其余行是适配器、控制器、study、后处理或补测试。这说明 `plant + controller + runner` 本身是适配的，
缺的是 Runner **之上**的一层编排与 Plant **之下**的一层可选能力声明（ADR 0005），而不是重写。

**预算规则:** `contract` 类不得超过 3 行，且每行必须引用 ADR。想扩大契约，先写决策。

## 3. 静默缺口（最危险的一类）

以下工况今天**不会报错**，只会给出"看起来正常"的结果，因此必须在下一版前消除其一：
要么支持，要么在 manifest 的 `route_limitations` 或 `uncheckable_rules` 里显式声明。

| 工况 | 今天的行为 | 处置 |
| --- | --- | --- |
| 用平均模型判断 DCM/纹波（A8） | `uncheckable_rules` 已声明，但报告里不显眼 | 保持声明；在 `docs/58` 增加对应行 |
| 把 `whole-run` 结果当实时拦截证据（A11） | 已由证据上限 `limited` 阻断 | 已闭合 |
| 需要 AC 环路裕度却只看瞬态（C4） | 已由 `pe-sim capability` 的 AC 入口闭合；仍不得把瞬态结果当 AC 裕度 | 用 `pe-sim capability` 明确声明工作点与扫频；结果不携带证据等级 |
| 需要比较器级事件（B3/D3） | 外部输入变更点已按声明时刻求解；比较器/器件事件仍只在求解段边界 | 保持为 `kernel_gap`；由 `docs/61` S9 处理，落地前文档不得暗示逐周期保护 |

已消除的两个静默缺口（保留在此备查）：A4 窗口内负载阶跃（ADR 0004 步骤 2，0.13.0）与
D2 非整数控制比（同一提交删除构造期限制）。

## 4. 落地顺序

顺序不在本文件维护。唯一施工顺序权威是 `docs/61_gap_closure_plan_v1.md`：它把本文件
每个未闭合行分配到且仅分配到一个阶段，`tests/test_coverage_matrix.py` 检查这一点。

本文件只保留两条纪律：

1. `expressible` 行优先补测试，而不是补功能——它们声称的正是"内核已够"。
2. 任何行从 `kernel_gap` 升级前，必须先有声明（ADR 0004）或计划层（ADR 0005）的落点，
   不允许用"先加一个开关"的方式绕过。

## 5. 变更规则

- 新增行必须同时给出状态；`covered` 必须有测试引用；`kernel_gap` 必须有 backlog id 或 ADR 小节；`out_of_scope` 必须有文档引用。
- 状态只能按 `kernel_gap → expressible → covered` 升级；不允许从 `kernel_gap` 直接跳到 `covered` 而不补 ADR 或测试。
- `tests/test_coverage_matrix.py` 校验上表的结构与引用；它失败即表示本文件与仓库不一致。

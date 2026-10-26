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
| A3 | 负载阶跃/突卸（窗口边界） | very-common | yes | expressible | adapter | 适配器级已被 `tests/test_stage4_reference_plants.py` 覆盖；无 Runner 级用例 |
| A4 | 负载阶跃落在控制窗口内部 / 亚开关周期 | very-common | no | kernel_gap | kernel | ADR 0004 §3（求解段边界改为变更点）；`UD-05` 的近亲 |
| A5 | 软启动（vref 斜坡） | very-common | yes | expressible | controller/adapter | 逐窗口 vref 调度即可；缺测试 |
| A6 | 预偏置启动（输出已预充） | very-common | partial | kernel_gap | adapter | ADR 0006（`InitialState.state` 承载预充电状态）；现状只能给定 `mode`，`vout` 无法进入运行 |
| A7 | 短路/逐周期限流/打嗝恢复 | very-common | partial | expressible | controller/adapter | duty 限幅已有；保护状态机与故障注入缺测试；亚窗口事件依赖 A4 |
| A8 | 轻载 / DCM / burst | very-common | partial | expressible | adapter | 开关级网表物理上可表现 DCM；平均模型声明 CCM 假设（`uncheckable_rules`）；无检测 |
| A9 | 死区与同步整流时序 | common | yes | expressible | adapter | 当前模板无死区/SR/栅驱动（`docs/31`）；需新网表模板，零内核改动 |
| A10 | 输入/负载/duty 参数扫掠 | very-common | no | kernel_gap | kernel | `docs/00` S4B（拓扑级扫掠）；无批量入口 |
| A11 | 长时程高分辨率连续波形（放弃窗口拦截） | common | partial | expressible | adapter | `whole_run` 路线已有（`limited`）；缺开关级整段适配器，见 ADR 0004 §Implementation 3 |

### B. 控制与环路

| ID | 工况 | 频率 | 内核可表达 | 现状 | 责任方 | 证据 / 缺什么 |
| --- | --- | --- | --- | --- | --- | --- |
| B1 | 电压模式 PI/PID（内核控制器） | very-common | yes | covered | kernel | `tests/test_pi_controller.py`, `tests/test_pid_controller.py` |
| B2 | Type II/III 补偿器（内核侧、按控制周期离散） | very-common | partial | kernel_gap | kernel | `controller_config` 是封闭键集（仅 kp/ki/kd/kff）；`controller_id` 仅 `pi`/`pid`；`UD-01` |
| B3 | 峰值/平均电流模式（逐周期比较器） | very-common | no | kernel_gap | kernel | 需比较器级事件（A4 的超集）；`UD-01` |
| B4 | 数字环路的采样/ZOH/计算延迟 | very-common | yes | expressible | controller | 控制器按控制周期调用、`timing` 已给 `control_period_s`/`sample_offset_s`；缺测试 |
| B5 | 前馈（line/load） | common | yes | expressible | controller | `command` 已传入控制器；`UD-01` |
| B6 | 多环级联（电压外环+电流内环） | very-common | yes | expressible | controller | 控制器内部状态可承载；缺测试 |
| B7 | 多相/均流/相位增减 | common | partial | expressible | adapter | 无多相模型；共享 duty + 相位内部管理可覆盖常见共相；逐相电流环属 specialized（ADR 0005） |
| B8 | 控制器放进网表 | common | no | kernel_gap | kernel | ADR 0004（`control_location=model`，上限 `limited`） |
| B9 | 保护状态机（OCP/OVP/UVLO/hiccup/latch） | very-common | partial | expressible | controller | 可用控制器状态机表达；缺故障注入与测试 |

### C. 分析类型

| ID | 工况 | 频率 | 内核可表达 | 现状 | 责任方 | 证据 / 缺什么 |
| --- | --- | --- | --- | --- | --- | --- |
| C1 | 时域瞬态（开关级/平均） | very-common | yes | covered | kernel+adapter | `tests/test_ngspice_plant_contract.py`, `tests/test_stage15_buck_boost_acceptance.py` |
| C2 | 解析稳态/平衡点判定 | very-common | yes | covered | kernel | `tests/test_stage15_buck_boost_acceptance.py`（`analytical_steady_state` + 稳态规则） |
| C3 | 周期稳态解（PSS） | very-common | no | kernel_gap | kernel | 无求解器；`docs/00` S3/S10 |
| C4 | AC 小信号 / 环路增益（Bode、PM、GM） | very-common | no | kernel_gap | kernel | 全域无 `.ac`/Bode/相位裕度；`GAP-01` |
| C5 | 阻抗扇帚（输入/输出、Middlebrook） | common | no | kernel_gap | kernel | 无扇频能力；依赖 C4；`GAP-02` |
| C6 | 效率与损耗分解 | very-common | no | kernel_gap | adapter+kernel | 无损耗模型，也无派生指标；`docs/00` S10；`GAP-03` |
| C7 | 纹波频谱 / THD / PF | very-common | no | kernel_gap | kernel | 无 FFT/加窗；`docs/00` S10 |
| C8 | 电热 / 热网络 | common | no | out_of_scope | adapter | `docs/43`（器件/热模型非内核工作）；`no_thermal_state_modelled` |
| C9 | Monte Carlo / 容差 / 最坏情况 | very-common | no | kernel_gap | kernel | 无采样与批量；`docs/00` S4B |
| C10 | 波形导出与派生分析工件 | very-common | partial | expressible | kernel | `samples.json`/`npz` 已存在；`UD-03`（窗口内波形访问/抽取上限） |

### D. 数值与时间尺度

| ID | 工况 | 频率 | 内核可表达 | 现状 | 责任方 | 证据 / 缺什么 |
| --- | --- | --- | --- | --- | --- | --- |
| D1 | 窗口内子步（plant_step_s） | very-common | yes | covered | kernel | `tests/test_runtime_preflight_contracts.py`（窗口端点校验） |
| D2 | 任意控制/开关周期比下的相位连续 PWM | common | no | kernel_gap | kernel | 构造期强制整数倍；`UD-05`；ADR 0004 §3 |
| D3 | 事件精确时刻（比较器/二极管过零） | very-common | no | kernel_gap | kernel | 事件只在窗口边界；ADR 0004 §3 |
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

### 2.2 按"关闭它需要动哪一层"分类

这决定了架构是否适配：**能否在不改 `PlantAdapter` / `ControllerAdapter` / Runner 循环的前提下
补上这个工况**。词表见 ADR 0005。

| 类别 | 行 | 含义 |
| --- | --- | --- |
| `closed` | A1, A2, B1, C1, C2, D1, D4, D5 | 今天可用且已有测试 |
| `test-only` | A3, A5, B4, B5, B6 | 今天已能表达，只缺用例 |
| `postprocess` | C7, C10 | 单次运行样本上的派生指标 |
| `adapter` | A8, A9, A11, B7 | 新模型或新网表模板，零内核改动（T1） |
| `controller` | A7, B9 | 新控制器或保护状态机 |
| `study` | A10, C3, C9 | 需要在单次运行之上编排（ADR 0005 Seam 1） |
| `capability` | C4, C5, C6 | 需要模型回答 `advance()` 答不了的问题（ADR 0005 Seam 2） |
| `kernel-impl` | A4, B2, B3, D2, D3 | 内核实现改动，**不改协议** |
| `contract` | A6, B8 | 真正需要改契约，且已由 ADR 决定（ADR 0006、ADR 0004） |
| `never` | C8 | 已声明不做 |

**读数说明:** 35 行里只有 **2 行**需要改契约，5 行需要内核实现但不改协议；其余 28 行是
适配器、控制器、study、后处理或补测试。这说明 `plant + controller + runner` 本身是适配的，
缺的是 Runner **之上**的一层编排与 Plant **之下**的一层可选能力声明（ADR 0005），而不是重写。

**预算规则:** `contract` 类不得超过 3 行，且每行必须引用 ADR。想扩大契约，先写决策。

## 3. 静默缺口（最危险的一类）

以下工况今天**不会报错**，只会给出"看起来正常"的结果，因此必须在下一版前消除其一：
要么支持，要么在 manifest 的 `route_limitations` 或 `uncheckable_rules` 里显式声明。

| 工况 | 今天的行为 | 处置 |
| --- | --- | --- |
| 负载阶跃落在窗口内部（A4） | 阶跃被推到下一个窗口边界，`vout` 曲线仍平滑合理 | ADR 0004 步骤 2；在此之前文档必须提示 |
| 控制周期非开关周期整数倍（D2） | 构造期报错（不静默，可接受） | ADR 0004 步骤 2 解除 |
| 用平均模型判断 DCM/纹波（A8） | `uncheckable_rules` 已声明，但报告里不显眼 | 保持声明；在 `docs/58` 增加对应行 |
| 把 `whole-run` 结果当实时拦截证据（A11） | 已由证据上限 `limited` 阻断 | 已闭合 |
| 需要 AC 环路裕度却只看瞬态（C4） | 不会报错，用户可能误判稳定性 | 在 `docs/58` 明确"无 AC 分析"；C4 落地前不得暗示 |

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

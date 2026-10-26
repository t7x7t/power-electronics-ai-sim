# 能力边界（v1）

**状态:** 面向使用者的范围声明，不是缺陷清单。它回答一个问题：**这个内核能求解什么、
不能求解什么，以及不能求解时应当怎么做**。判定词表与字段名保留英文原文。

## 1. 一句话

内核求解的是「**每个控制窗口一次求解、单一标量执行器（`duty`）、约束在窗口边界实时
判定**」的闭环。它不是通用电路仿真器，也不是瞬态波形记录仪。

## 2. 三个内置 Buck 后端不是同一类东西

| 后端 | 模型 | 时间归属 | ngspice 调用 | 证据上限 |
| --- | --- | --- | --- | --- |
| `buck` / `boost` | **理想平均**模型（Python RK4） | 内核步进 | 无 | `comparable`（需 `verified` + `formal_comparison`） |
| `ngspice-buck` | **开关级**异步 Buck：`PULSE` 驱动 + `SW(Ron/Roff)` + Shockley 二极管 + L/C，`IC=` 携带状态 | 内核步进 | **每个控制窗口一次** `.tran` | `comparable`（同上） |
| `whole-run-buck` | 理想平均 + 引擎内 PI | 引擎自持整段 | 自行积分 | `limited`（post-hoc only） |

**常见误判：** `ngspice-buck` **不是**理想平均模型。理想平均的是 `buck`/`boost`
（纯 Python）与 `whole-run-buck`（引擎内 PI）。"理想"在 `ngspice-buck` 里指元件理想
（R/L/C 无寄生、无器件与温度模型），指的不是平均化。

## 3. 三个直接后果（是设计后果，不是缺陷）

1. **窗口内 duty 恒定。** Plant 契约是 `advance(action, duration)`，一次传入一个标量
   执行器值；因此窗口内的占空比必须视为零阶保持。窗口内部的波形是被采样出来的旁产物，
   不是被整段积分的对象。（`backends/ngspice_plants.py` 模块 docstring）
2. **补偿器必须在内核侧。** 闭环由内核的 Controller 每个控制窗口给出 `duty`；网表里没有
   控制器。Type III 也是 Controller——输入 `vout`/`vref`，输出 `duty`——但它是插件，
   不是网表内容。
3. **每个窗口重启 PWM 载波。** 为了相位连续，`control_period_s` 必须是开关周期的
   **整数倍**；否则构造期直接报错（`control_period_s must be an integer multiple of the
   switching period for phase-continuous window solves`）。这条限制写在声明的适用域里
   （`switch_node` 相关的 `uncheckable_rules` / assumptions）。
4. **输入变化只在窗口边界生效。** 允许的调度键是 `time_s`、`vref`、`vin_v`、
   `input_voltage_v`、`load_ohm`、`load_resistance_ohm`；窗口内部没有阶跃通道。

**为什么这样设计：** 不变量 I1——约束必须在运行**期间**强制执行，而不是事后重建。
只有能逐窗口拦截的路线才配 `comparable`；一次性整段求解无法在飞行中拦截，因此
`whole-run` 路线被封顶为 `limited`，并在 `route_limitations` 里自报
`in_run_enforcement_unavailable` / `post_hoc_validation_only`。

## 4. 因此不适合直接承载的任务

| 任务形态 | 为什么不适合 | 应当怎么做 |
| --- | --- | --- |
| 网表内闭环（Type III 等） | 控制器不在网表里；每个窗口只有一次求解 | 把补偿器写成 Controller 插件；或另写 Adapter |
| 贯穿整个运行的连续瞬态（软启动全程、密集开关波形） | 窗口化求解 + 每窗口重启载波 | 直接用 ngspice 生成并运行网表 |
| 微秒级全波形长时导出 | 采样分辨率有界，且成本随窗口数线性增长（每窗口一个子进程） | 直接 ngspice，或降低时长/提高采样步长后接受证据降级 |
| 器件级、电热、磁饱和、EMI | 非本仓库范围 | 走外部工具链 |

## 5. 什么时候应当绕过 `pe-sim`

任一条成立即可绕过：需要网表内控制器；需要跨窗口连续积分；需要器件/温度模型；
**不需要**证据分级与约束强制。

绕过时你会失去：约束的实时判定、`qualification`、`evidence_level`、可复核的
`manifest`/工件索引、审计流、以及 L1 attestation。这不是"框架不够强"，而是交换。

## 6. 想留在框架内的两条正路

1. **写 Controller。** Type III、前馈、饱和与抗饱和都属于 Controller 的职责；Plant
   只暴露一个标量执行器。写完后同一份运行仍可拿到窗口级约束判定与证据等级。
2. **写 Adapter。** 新引擎、新时间模型都应实现为适配器并注册；`docs/43` 的 T1 要求
   新增物理引擎**零内核改动**。适配器必须诚实声明 `time_model` 与 `route_limitations`，
   证据上限由路线决定，不由声明决定。

## 7. 相关文件

- `docs/13_stage4_reference_plants_v0.md` —— L1 平均参考模型
- `docs/23_stage15_l1_buck_boost_acceptance_v1.md` —— L1 验收
- `docs/31_ngspice_backend_scope_v1.md` —— ngspice 后端范围
- `docs/40_validity_and_authenticity_v1.md` —— 适用域声明与检查阶段
- `docs/43_kernel_roadmap_v1.md` —— I1 不变量、T1、证据等级
- `docs/60_simulation_coverage_matrix_v1.md` —— 工况级覆盖矩阵与已登记缺口
- `src/pe_sim/backends/ngspice_plants.py` —— 窗口化实现与适用域原文
- `src/pe_sim/backends/whole_run_engine.py` —— 整段路线的自我降级声明

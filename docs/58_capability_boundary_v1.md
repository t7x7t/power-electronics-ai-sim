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
| `ngspice-buck` | **开关级**异步 Buck：`PULSE` 驱动 + `SW(Ron/Roff)` + Shockley 二极管 + L/C，`IC=` 携带状态 | 内核步进 | **每个求解段一次** `.tran`（段在下一个输入变更点结束） | `comparable`（同上） |
| `whole-run-buck` | 理想平均 + 引擎内 PI | 引擎自持整段 | 自行积分 | `limited`（post-hoc only） |

**常见误判：** `ngspice-buck` **不是**理想平均模型。理想平均的是 `buck`/`boost`
（纯 Python）与 `whole-run-buck`（引擎内 PI）。"理想"在 `ngspice-buck` 里指元件理想
（R/L/C 无寄生、无器件与温度模型），指的不是平均化。

## 3. 直接后果（是设计后果，不是缺陷）

1. **段内 duty 恒定。** Plant 契约是 `advance(action, duration)`，一次传入一个标量
   执行器值；因此一个求解段内的占空比必须视为零阶保持。段内部的高频波形是被采样出来
   的旁产物，不是被整段积分的对象。（`backends/ngspice_plants.py` 模块 docstring）
2. **补偿器与保护都在内核侧（`control_location=kernel`）。** 闭环由内核的 Controller
   每个控制窗口给出 `duty`；网表里没有控制器。Type III 也是 Controller——输入
   `vout`/`vref`，输出 `duty`——但它是插件，不是网表内容。保护状态机（OVP/OCP/UVLO/
   hiccup）同样是 Controller 包装，它在每个控制采样点评估，因此故障最迟在下一个
   控制周期执行；这不是逐周期比较器保护的声明。
3. **载波相位是求解段参数，不再需要整数倍。** 段起点相位由 `window_start mod Tsw`
   给出，网表把 PULSE 延迟渲染为到下一个载波起始的时长 `(-phase) mod Tsw`，因此任意
   `control_period_s` 都能保持相位连续；如果段起点落在导通脉冲内部，网表还用一段
   有限 PWL 尾脉冲补回剩余导通时间再与周期源相加，避免静默丢失能量。旧的整数倍限制
   已在 `docs/adr/0004` 步骤 2 删除。
4. **输入变更点按声明时刻求解。** 允许的调度键是 `time_s`、`vref`、`vin_v`、
   `input_voltage_v`、`load_ohm`、`load_resistance_ohm`；内核在下一个变更点结束当前
   求解段，并在新段起点重新解析输入。控制器的观测与动作仍然只发生在窗口边界。
5. **study 层是运行之上的编排，不是新的证据等级。** `pe-sim study` 把每个点展开成一份
   普通实验文档（`<output>/points/pNN/config.json`）并各自发布运行包；报告里每点的
   `status`/`evidence_level`/`manifest_sha256` 都从该点自己的 `manifest.json` 读出。
   聚合恒报 `per-point`，并单列 `inherited_ceiling`（各点**已发布**等级的最弱者，
   不看资格）与 `cross_point_comparability`（只有全部点 `QUALIFIED` 且都 `comparable`、
   同一非空 `run_mode`、同一非空 `plant.id`、同一路线三元组、且 `run_id`/`index`
   互不重复时才 `claimed`）。`periodicity` 只声称“在声明的相对容差内
   重复”，不声称物理周期稳态；统计量只描述样本，不构成效率或损耗结论——那是
   C6/S8 的可选 `loss_model`。
6. **AC 与能力结果不是运行，也不携带证据等级。** `pe_sim.ac_analysis` 在**声明的**
   工作点上做解析线性化（CCM 平均、理想元件），回路增益用控制器系数的 Tustin 连续等效，
   **不含**采样延迟、零阶保持相位与混叠；结果自报 `method`/`assumptions`/`verified_by`
   与完整 `provenance`。`phase_margin_deg`/`gain_margin_db` 只在给定工作点与**扫描分辨率**
   下有意义（同一回路 241/401/512 点的增益裕度实测为 26.79/25.46/25.81 dB），
   且 `margin_positive=None` 意味着“无法评估”，不得读作“稳定”。相位平坦在 ±180° 时
   `gain_margin_db=None` 并带 `phase_flat_at_minus_180`，不得用第一个采样点伪造增益裕度；
   margins 的 `verified_by` 必须包含验证 margins 自身的测试，而不是只复制 loop-gain 的测试。
   `LossBreakdown.to_dict()` 同样带 `provenance`（含工作点、`capability_origin` 与
   `plant_verified_by`）；`impedance_response`/`middlebrook_ratio` 顶层与 AC 结果同形。
7. **阻抗与 Middlebrook 共用同一线性化。** `SmallSignalModel` 的端口是具名的
   （`duty`/`vin`/`load_current` → `vout`/`input_current`），`Zin = v_in/i_in`、
   `Zout = v_out/i_load`（`load_current` 为**注入**输出节点的电流）。
   `middlebrook_ratio` 只回答“在采样频点上 |Zin/Zsrc| 或 |Zload/Zout| 是否大于 1”，
   外部阻抗由调用方声明，`criterion_meaning` 明确写了“必要筛子，不是稳定性证明”，
   且结果里没有 `stable` 字段。零阻抗是**确定答案**而不是“未知”：分子为零即违反、
   比值无穷即通过；由于 −∞ dB 无法 JSON 序列化，用 `min_ratio_db_bound` 标记这个边界，
   `not_evaluable` 只留给真正未定义的 0/0 或非有限点。
8. **损耗模型必须由模型声明。** 内置理想 buck/boost **不声明** loss provider：
   它们的效率在数学上恒为 1.0，把那个数字当“效率”就是编造。带损耗的
   `LossyBuckPlant` 必须显式声明 `Rds`/`DCR`/`Vf`/`Qoss`/`Qg`，并按固定的五项分项报；
   它按平均 `IL²` 计导通损耗（无纹波修正）、低边用固定压降、开关损耗只含 `Qoss·Vin·fsw`，
   不建模磁芯损耗与温度。全零损耗时必顶 `declared_lossless=True`，效率 1.0 只能是“声明”而不是推断。

**为什么这样设计：** 不变量 I1——约束必须在运行**期间**强制执行，而不是事后重建。
只有能逐窗口拦截的路线才配 `comparable`；一次性整段求解无法在飞行中拦截，因此
`whole-run` 路线被封顶为 `limited`，并在 `route_limitations` 里自报
`in_run_enforcement_unavailable` / `post_hoc_validation_only`。

## 4. 因此不适合直接承载的任务

| 任务形态 | 为什么不适合 | 应当怎么做 |
| --- | --- | --- |
| 网表内闭环（Type III 等） | 控制器不在网表里；每个求解段只有一次求解 | 把补偿器写成 Controller 插件；或另写 `control_location=model` 的 Adapter（ADR 0004 步骤 3） |
| 需要单次不可分割的整段开关级积分 | 求解段仍按窗口与变更点切分，每段重启一次 ngspice | 使用开关级 whole-run 适配器（ADR 0004 步骤 3 / `docs/61` S9），或直接用 ngspice |
| 逐周期比较器保护 / 峰值电流模式 | 控制器只在控制采样点看测量，保护最迟在下一个控制周期执行 | 尚未提供；见 `docs/60` B3 与 `docs/61` S9（模型内比较器） |
| 微秒级全波形长时导出 | 采样分辨率有界，且成本随窗口数线性增长（每窗口一个子进程） | 直接 ngspice，或降低时长/提高采样步长后接受证据降级 |
| 把 study 聚合结果当成 `comparable` 证据 | 聚合只报 `per-point`，并把继承上限与跨点可比性单列 | 逐点引用该点的运行包；需要跨点可比时，先让每点都达到 `comparable` 并声明同一路线 |
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
- `docs/adr/0004-solve-route-capabilities.md` —— 路线声明、载波相位与变更点分段
- `docs/adr/0005-studies-and-model-capabilities.md` —— study 编排与可选模型能力的两个 seam
- `docs/60_simulation_coverage_matrix_v1.md` —— 工况级覆盖矩阵与已登记缺口
- `src/pe_sim/backends/ngspice_plants.py` —— 窗口化实现与适用域原文
- `src/pe_sim/backends/whole_run_engine.py` —— 整段路线的自我降级声明
- `src/pe_sim/study.py` —— study 计划层（展开、抽样、容差判定、聚合规则）
- `src/pe_sim/study_runner.py` —— study 编排（逐点文档落盘、读回发布包、写报告）

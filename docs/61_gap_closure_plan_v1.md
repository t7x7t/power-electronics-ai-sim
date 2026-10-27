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
| S3 | 0.11.0 | —（已完成：初始状态载荷） | `contract`（ADR 0006） | S2 | 已交付：载荷可验证；无载荷文档行为不变 |
| S4 | 0.12.0 | —（已完成：派生指标） | `postprocess` | S0 | 已交付：父包链接、原包不变、体积上限 |
| S5 | 0.13.0 | —（已完成：相位连续与变更点） | `kernel-impl` | S3 | 已交付：pin 记录已更新；ADR 0004 两条验收通过 |
| S6 | 0.14.1 | —（已完成：控制器 seam） | `controller` seam | S5 | 已交付：B2 注册表/Type II-III（0.14.0）；B9/A7 保护与短路恢复（0.14.1） |
| S7 | 0.15.0 | —（已完成：study 计划层） | `study` seam | S1 | 已交付：A10 C3 C9 闭合；聚合恒为 `per-point`，不自动继承 `comparable` |
| S8 | 0.16.0 | —（已完成：capability 入口） | `capability` seam | S7 | 已交付：C4 C5 C6 闭合；缺失能力 = 不可用（不是 `limited`）；AC/阻抗方法必须自报 |
| S9 | 0.17.0 | A11 B8 B3 D3 | `adapter` 扩面 + 比较器事件 | S5 | T1 仍然成立：新能力是注册，不是内核分支；A8/A9/B7 已交付 |
| S10 | 0.18.0 | —（收口与发布） | 发布 | S1–S9 | `docs/60` 无 `kernel_gap` / `expressible` 残留 |

### 2.1 为什么是这个顺序

- **S1 先做**：它用最低成本验证"架构可表达"这一判断。如果 test-only 行里有任何一行
  其实需要产品代码，说明 `docs/60` 的分类错了，后面的排期都要改。它是计划的自检。
  S1 的第一次执行已经发现 A6 属此类，因此把它改判为契约行并单独拆出 S3。
- **S2 早于 S5**：`control_location` / `event_granularity` 必须先成为声明，S5 才能删掉
  整数倍限制而不创造隐式行为。S2 是行为中性的，可以安全地单独发布。
- **S3 不依赖 pin 门**：A6 是纯契约扩展（初始状态载荷），不碰被审 solve path，也不改
  证据上限。把它放在 pin 门之前，保证"维护者未授权期间仍有一件确定能交付的事"。
- **S4 不依赖 pin 门**：它只消费已有样本，不改求解路径，不碰 pin。因此在 S5 等待
  维护者授权期间，它是可以确定交付的那一件。
- **S5 早于 S6/S9**：A4（窗口内负载阶跃）是 `docs/60` §3 登记的静默缺口，也是 A7（短路）
  与 B3（逐周期比较器）的前置。它排在 study/capability 之前，因为它是正确性问题，
  不是能力扩展问题。
- **S6 早于 S7**：控制器 seam 关闭 3 行且不依赖 study；study 需要一批可运行的运行，
  控制器先稳定可以让 study 的验收更简单。
- **S7 早于 S8**：C4/C5 的 AC 与阻抗是"在若干运行/工作点上提问"，先有 study 编排，
  能力 seam 才有调用者；否则会先写一个没有入口的能力。
- **S9 最后**：适配器扩面依赖 S5 的连续事件才能诚实声明 `continuous`；它关闭的行最多，
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

**S4 — 派生指标（C7 C10）**

- 交付：单次运行样本上的派生分析（纹波统计、Hann 加窗频谱、THD/PF），
  独立工件 + 父包链接；采样/抽取策略与体积上限。
- 验收：原始包字节不变；派生工件声明父 `package_digest` 与自身证据继承关系；
  超限时失败关闭而不是截断。

**S5 — 相位连续与变更点边界（A4 D2）**

- 交付：载波相位参数化（PULSE 延迟 = `(-phase) mod Tsw`）、求解段在下一个变更点结束、
  删除整数倍限制与对应 `uncheckable_rules`；`ngspice-buck` 声明 `continuous`。
- 验收：(a) `control_period_s = 1.5 × Tsw` 可构造并运行，且与单次直跑在声明容差内一致；
  (b) 窗口中点 10 Ω → 5 Ω 负载阶跃与边界反事实、与单窗口分段均一致。
- 门禁：改动落在被审 solve path 上，**必须先有维护者复核记录**（§5）。
- 说明：D3 的比较器/器件级事件不属于本阶段，随 B3 在 S9 处理。

**S6 — 控制器 seam（B2 B9 A7）**

- 已交付（0.14.0，B2）：ADR 0007；`controller_id` 由枚举改为安全标识符 + 控制器注册表；
  `type2`/`type3`/`biquad`（Tustin 离散、系数记录在身份中）；`pi`/`pid` 行为不变。
- 已交付（0.14.1，B9/A7）：`ProtectedController` 包装（OVP/OCP/UVLO、hiccup/latch）；
  0.2 Ω 短路在窗口中点触发，越界后一个控制周期内输出 trip 动作，故障移除后打嗝重试。
  上限 `protection_latency_is_one_control_period` 已由包装声明。
- 验收：JSON 可选 `type2`/`type3` 并发布带控制器身份的 `QUALIFIED` 包；
  `pi`/`pid` 的现有测试逐字不变；保护动作及其延迟出现在 manifest。
- 上限：A7 登记 `protection_latency_is_one_control_period`（§2.3）。

**S7 — Study seam（A10 C3 C9）**

- 已交付（0.15.0）：`src/pe_sim/study.py`（纯计划层：文档解析、扫掠/采样展开、描述统计、
  容差式周期性判定、聚合规则、报告）与 `src/pe_sim/study_runner.py`（编排：执行每个点时
  才落盘 `points/pNN/config.json` → 调用注入的执行器 → 读回该点 `manifest.json`/`samples.json`）；
  `pe-sim study` 子命令（与 `run` 相同的六个选项，不含 `--progress`/`--cancel-file`）。
- 验收：duty/kp 扫掠生成参数曲线；固定 seed 的 Monte Carlo 可复现（同时记录 `draws_sha256`）；
  buck 的 periodicity 收敛判定可达（`kp=0.02`、时长 2 ms → 16 ms → 128 ms 时第三轮收敛）；
  聚合结果不自动获得 `comparable`：`aggregate.evidence_level` 恒为 `per-point`，
  `inherited_ceiling` 取最弱点，`cross_point_comparability` 默认 `not_claimed`。
- 偏差（登记，非静默降级）：本阶段验收原写“效率曲线”，但效率与损耗分解属 **C6**，
  按 §2.1 的顺序纪律属 S8，S7 不主张任何功率/损耗结论；扫掠输出的是**样本描述统计**
  （`mean`/`min`/`max`/`rms`/`peak_to_peak`/`last`/`ratio_of_means`），并登记
  `descriptive_statistics_only_no_loss_claim`。
- 实施教训（供后续阶段参考）：比例通道 + `duty∈[0,1]` 限幅会产生继电式极限环
  （`kp=0.2`、`vref=5 V` 时 1.02 s 后相对差实测约 `1.4e-2`，索引 lag=1），因此“收敛”验收
  必须选择执行器全程不撞限幅的工况；`periodicity` 判定只声称容差内重复，不声称物理稳态。
- 独立校验（只读子智能体）判定 FAIL 并已修复：① 重复参数 `path` 会静默丢轴并产出重复点，
  使聚合可 `claimed` → 计划层拒绝 `duplicate_parameter_path`，聚合要求 `run_id`/`index` 唯一、
  身份字段为非空字符串（新增理由码 `duplicate_points`/`point_identity_incomplete`/`empty_study`，
  空点不再用 `single_point_study`）；② 坏或缺失 `manifest.json`/`samples.json` 曾静默 exit 0 →
  现在记入 `details.manifest_error`/`statistics_error` 并失败（报告仍写）；大小写外异常也按点
  记录（`executor_error`）不中断其余点；③ 有限但过大的 `sigma` 可溢出为非有限 draw 并在写盘后
  裸崩 → 计划阶段即 `invalid_draw`，且 `config.json` 改为逐点、序列化先行；④ 附带修正：
  `write_study_report` 临时名加 pid、`run_study` 删除 `output_dir` 覆盖参数（唯一输出根是
  `plan.output_dir`）、`evaluate_periodicity` 增加 `sampling_interval_s` 与索引 lag 说明。

**S8 — 模型能力 seam（C4 C5 C6）**

- 已交付（第一片，未提升版本）：`src/pe_sim/model_capabilities.py`（能力注册表、
  `OperatingPoint`/`SmallSignalModel`/`LossBreakdown`、buck/boost 解析模型）与
  `src/pe_sim/ac_analysis.py`（Bode、Tustin 连续等效回路增益、PM/GM）；结果只自报
  `method`/`assumptions`/`verified_by` 与完整 `provenance`，不携带证据等级。
  独立校验后加固：`origin` 区分内置/调用方注册、`verified_by` 必须指向真实测试 id、
  `assumptions` 禁用证据等级词、`plant_id` 与查找键必须一致、margins 保留扫频与插值方法、
  `LossBreakdown` 强制功率/损耗/效率一致性。三片库级实现（AC、阻抗/Middlebrook、
  损耗/效率）均已交付；**C4/C5/C6 已在 0.16.0 由 `pe-sim capability` 入口关闭**。
- 已交付（第二片）：`SmallSignalModel` 端口具名化（`duty`/`vin`/`load_current` →
  `vout`/`input_current`，旧单端口视图保持不变）、`src/pe_sim/impedance_analysis.py`
  （`Zin`/`Zout` 扇帚与 Middlebrook 幅度判据）、`src/pe_sim/frequency_domain.py`
  （扫频/幅值/相位助手单一实现）。实现中修正了一处真实符号错误：buck 的输入电流
  状态行必须乘电感电流（`i_in = D·iL`），最初的 `(0, D)` 会让 `Zin(0)` 差 10 倍。
- 第二片独立物理审计：状态空间、`load_current` 符号、`Zin(0)=R/D²`、
  `Zout(f0)=R`、与 `9ab9920` 的 `transfer_function` 回归全部 PASS；Middlebrook 判 FAIL：
  ① 幅值数组长度不一致被 `zip` 静默截断，可把 `violated` 报成 `satisfied`；② 零阻抗被当成
  “不可评估”，导致 `first_violation_hz` 跳过真实违反点。已修：长度必须三元一致；零分子判违反、
  零分母（正分子）判通过、0/0 才 `not_evaluable`；`min_ratio_scope`/`total_points`/`evaluated_points`
  显式化；−∞ dB 用 `min_ratio_db_bound` 表达以保持 JSON 有限；外部阻抗来源记入 `external`；
  `verified_by` 修正为扁平字符串列表（曾误为嵌套列表）。
- 已交付（第三片）：`src/pe_sim/lossy_converter.py`——带声明损耗参数的 averaged
  CCM buck（`Rds`/`DCR`/`Vf`/`Qoss`/`Qg`）+ 解析 `analytical_steady_state` + `loss_model()`
  返回 `LossBreakdown`；损耗分项固定为开关导通、电感导通、低边导通、`Qoss` 开关、栅驱动。
  全零损耗时必须显式 `declared_lossless=True`（否则 `LossBreakdown` 拒绝），这条已单独测试。
  该模型**不注册为 runner 植物**，只注册 loss provider；身份摘要通过既有 `register_solve_path`
  取得，`review_status` 为 `not_pinned`，未碰 `reviewed_identities.json`。
- 已知近似（已写进 `assumptions`）：按平均 `IL²` 计导通损耗（无纹波修正）、低边用固定 `Vf`
  （同步整流应改用低边 `Rds`）、开关损耗仅含 `Qoss·Vin·fsw`（无交越/软开关）、不建模磁芯损耗与温度。
- 交付：可选 `small_signal_model_for(plant, op)` 与 `loss_model()`；AC 扫频 / Bode / PM / GM；
  输入/输出阻抗与 Middlebrook 判据；效率与损耗分解。
- 验收：未实现能力的植物报告"不可用"而非 `limited`；每个结果记录线性化方法与验证方式；
  实现方式（解析 vs 辨识）出现在工件里。
- 已交付（入口，0.16.0）：`src/pe_sim/capability_analysis.py` + `pe-sim capability <config.json>`。
  独立文档 `pe-sim.capability-analysis.v1`（不伪装成 study 的 `base_experiment`）：声明
  `plant`（注册植物，或能力专用的 `lossy-buck`）、`operating_point`、`sweep`、可选
  `controller`（loop gain）、`impedance.kind`/`external`；输出 `pe-sim.capability-report.v1`，
  可原子写入 `output.directory/capability-report.json`。三类结果分别复用 AC/阻抗/损耗的
  既有实现，不复制数学；`CapabilityUnavailable` 与文档错误映射到既有拒绝码。
  入口测试 `tests/test_capability_cli.py`（AC、阻抗/Middlebrook、损耗、CLI 工件与拒绝框）。
- **入口前独立评审与加固（2026-10-27，未提升版本）**：4 个独立子智能体分别审查
  架构/需求、buck-boost 与阻抗、Middlebrook/裕度、损耗，监督者逐条复现。已修：
  ① `run_study` 把 `exit_code`/执行失败写入 `StudyPointRecord`，`aggregate_study` 仅当
  全部点 `execution_ok` 且 `analysis_ok` 才允许 `cross_point_comparability="claimed"`；
  ② periodicity 前点错误不再被后点收敛掩盖；③ `study_runner` 校验 manifest `run_id`、
  `plant_id` 与 `manifest_sha256`（存在 `package_sha256` 时调用既有 `verify_package`），
  且 `samples.json` 无论是否声明统计量都必须存在且只含有限数；
  ④ `stability_margins` 把 ±180° 视为同角，相位平坦在 −180° 时 GM 不猜值，并拒绝
  `status="failed"`/`schema` 不符/sweep 点数不符的输入；⑤ `middlebrook_ratio` 拒绝负幅值、
  非有限/非递增频率与非法 provenance；⑥ `LossBreakdown` 拒绝负输出功率、负效率与
  “声明无损但有损耗项”；⑦ `CapabilityUnavailable` 理由码封闭并映射 CLI `invalid_request`；
  ⑧ `origin="builtin"` 只能由内置工厂声明；⑨ S7/S8 新名字以 experimental 身份顶层可达。
  相关 10 个测试文件 **415 passed**；入口测试 `tests/test_capability_cli.py` **9 passed**；
  全量 **1665 passed / 11 skipped**（无 ngspice）。`docs/60` 的 C4/C5/C6 已改为 `covered`。
- **发布后真实工况端到端复核（2026-10-27，0.16.1）**：三路独立子智能体只读执行，
  监督者独立复现关键值。正向 54 项断言中 51 项 PASS，3 项经查为验证脚本期望值设定错误
  （开路 buck 的 `Zin(0)=+R/D²`、`lossy-buck` 自洽工作点非理想 `D·Vin`、`input_sha256`
  覆盖含输出路径的整份文档），不是实现缺陷；拒绝矩阵 26 项全部 PASS。
  发现并修复一个真实契约缺口：`write_capability_report` 只包装 `mkdir` 的 `OSError`，
  `open`/`fsync`/`os.replace` 失败会逸出为无 `reason` 的通用拒绝；现统一映射为
  `CapabilityAnalysisError("output_error")`，并增加回归测试。新增真实工况 E2E
  （48 V→12 V/5 A；AC、输入阻抗、损耗三种 kind 同点串联）与写失败回归后，
  `tests/test_capability_cli.py` **11 passed**。

**S9 — 适配器扩面（A11 B8 B3 D3；A8/A9/B7 已交付）**

- 交付：死区/同步整流网表模板；开关级 whole-run 适配器；多相（共享 duty + 内部相位）
  模型；`control_location=model` 的两个适配器（各对应一个 `time_model`）；
  开关级电流模式（比较器在模型内）；比较器/器件级事件时刻（D3 的剩余部分）。
- 验收：每一行一个独立验收；T1 测试仍然通过（新能力零内核改动）。
- 已交付（第一片，未提升版本）：A9 `ngspice-sync-buck`（`backends/sync_buck.py`，
  模板 `ngspice-sync-buck-v1`：高/低边双开关、两侧死区、PWL 任意载波相位、无二极管）与
  A8 导通模式检测（`classify_conduction_regime`：`ccm`/`dcm`/`forced_ccm`/`idle`/`burst`）；
  B7 `multiphase-buck`（`multiphase_plants.py`：2–8 相共享 duty、内部相位偏移、总电流平均
  模型、`L/N` 等效电感；逐相均流/增减相不建模）。测试：
  `tests/test_sync_buck_plant.py` **15 passed**（含真实 ngspice 窗口）、
  `tests/test_multiphase_plant.py` **14 passed**；全量 **1705 passed / 2 skipped**。
  `docs/60` 的 A8/A9/B7 已置 `covered`。新 solve path 尚未获得维护者 review pin，
  正式比较仍封顶 `limited`；A11/B8/B3/D3 尚未开始。

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
| D3 | `limited` | 与 B3 同一限制码；比较器/器件事件在模型内求解 |
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

S5 修改 `backends/netlist.py` 与 `backends/ngspice_plants.py`，二者在被审 solve path 上。
身份摘要变化会使 `ngspice-buck` 的 `review_status` 从 `reviewed` 变为 `unreviewed`，
从而把所有 ngspice 运行封顶在 `limited`。因此：

1. 实施者完成代码与验收后，监督者给出改动摘要与新的 `implementation_sha256`。
2. **维护者**确认该摘要并授权更新 `src/pe_sim/reviewed_identities.json`；
   没有这次授权，S5 不得合并，S6 的 A7 与 S9 的 `continuous` 声明也不得开始。
3. 该授权是一次性的、按提交记录的；它不构成对后续改动的预先批准。
4. 如果维护者拒绝或要求拆分，S5 回到"S2/S3 已发布、S5 待定"状态，
   S4/S7/S8 以及 S6 的 B2/B9 可以继续，但 A7 与 S9 阻塞。5. **执行记录（0.13.0/0.13.1）：**新摘要 `1af1fcdbd1b311410639e5328b03275b22f5bf3a586a7880519f875c3e347a2e`，
   验收为 `tests/test_event_boundaries.py`（D2 1.5× 与 1.2×Tsw 对照、A4 关断区阶跃与导通区
   0.01% 变更对照、D3 非零差异、路线声明）；0.13.1 修复了“段起点在导通脉冲内丢失剩余
   导通时间”的静默缺陷，已写入 `reviewed_identities.json` 的 `acceptance_ids` 与 `review_note`。
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

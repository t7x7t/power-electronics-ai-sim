# v0 约束保障架构：机器强制约束、人工边界审查与证据留痕

**状态**：方案说明（未作为实现承诺）  
**适用范围**：当前工作区的 L0 目标、v0 公共运行骨架，以及后续真实 PySpice/Ngspice 接入。  
**本文目的**：解释为什么需要三层保障，并定义后续实现可以遵循的技术边界、检查时机、失败语义和验收证据。

## 1. 为什么采用三层保障

电力电子仿真同时具有软件执行风险和工程解释风险。单靠人工检查无法稳定捕获每一步的时间因果、非有限值、Hash 不一致和产物缺失；单靠自动化又无法判断模型是否足以代表真实硬件、指标是否足以支持工程结论。因此采用三层互补架构：

```text
机器强制约束       防止运行违反可机械判定的规则
人工边界审查       判断模型、用途、证据和责任边界
证据留痕           让运行事实、检查结果和审批决定可追溯
```

三层的职责不能互换：机器检查通过不等于工程结论获批；人工批准也不能绕过运行时安全检查；没有证据的判断不能被当作已完成的验收。

## 2. 分层架构

```text
ExperimentSpec / 基线 / 审批记录
              |
      Preflight Gate（运行前）
              |
Runner -> Contract Guard -> Safety Guard -> Plant/Controller
   |          |                 |
   +------ Event/Audit Sink -----+
              |
      Qualification / Metrics
              |
 Artifact Writer -> Manifest -> Post-run / Human Review
```

### 2.1 机器控制平面

机器控制平面由以下逻辑检查器组成。实现时可以是模块、策略对象或独立命令，但每个检查器都必须有稳定的规则 ID、版本、输入、输出和失败动作。

| 检查器 | 责任 | 当前 v0 | 后续目标 |
|---|---|---|---|
| Schema/Config Guard | 配置字段、类型、单位、枚举和 Hash 格式 | Experiment/Manifest Schema 已有；运行时校验部分字段 | 统一 preflight，拒绝隐式默认值和不兼容版本 |
| Capability Guard | Plant/Controller 能力声明与实验要求匹配 | Plant `capabilities()` 存在性有检查 | 对每个 capability 做声明、协商和负向测试 |
| Contract Guard | Measurement 白名单、truth 隔离、ActionRequest 类型 | Controller 使用 `controller_measurement()`；有隔离测试 | 受限对象、字段白名单和访问审计 |
| Causality/Time Guard | 时间单调、采样年龄、动作目标时刻和可见窗口 | 时间戳和 Plant 推进有基础检查 | 完整多速率、延迟、preview 和事件冻结语义 |
| Safety Guard | 非有限值、动作限幅、停止条件 | `check_observation`/`project_action` 已有基础实现 | 可版本化规则、分级阈值和故障注入覆盖 |
| Lifecycle Guard | 状态转换和终态合法性 | Runner 生成基础状态 | 显式状态机、转换矩阵和不可绕过的 API |
| Provenance/Hash Guard | 源码、环境、模型、契约和产物来源 | 产物 SHA-256；部分 provenance 为占位值 | Git/环境锁定、组件 Hash 和 dirty 基线策略 |
| Artifact Guard | 必需产物、原子发布、不可覆盖和索引完整性 | 临时目录、原子发布、必需文件检查已实现 | 失败中断恢复、包 Hash 和完整性验证 |

### 2.2 人工边界审查层

人工审查只处理机器不能可靠判定或风险过高的事项：需求归属、模型代表性、额定参数、证据适用范围、第三方资料许可、是否可以进入正式基线或可选学习流程。审查结果必须结构化记录，不能只留在聊天或口头会议中。学习/自适应本身不是核心基础设施的必选能力，只有接入独立适配器时才启用相应审查。

### 2.3 证据层

所有机器检查、人工决定和运行结果都应关联到同一个 `run_id`、`experiment_id`、规则版本和来源 Hash。证据层的目标是回答：发生了什么、用的是什么、检查了什么、谁批准了什么、结论不能超出哪里。

## 3. 约束分类与机器执行方式

约束应先拆成原子规则，再映射到检查时机、失败状态和证据产物。推荐最小字段如下：

```text
constraint_id
rule_version
scope                 # preflight / runtime / postrun / release
machine_or_human      # machine / human / both
input_evidence
pass_condition
failure_state
required_artifact
owner
```

### 3.1 结构、Schema 与配置

机器应检查：必填字段、类型、单位、枚举值、标识符格式、Hash 格式、初始状态条件、输入 schedule 顺序和输出策略。Schema 不允许用运行环境、当前目录或全局变量静默补齐关键字段；缺失信息应拒绝启动或标记 `INCOMPLETE`。

### 3.2 接口、信息和权限

Controller 的输入必须是测量合同允许的字段映射，而不是 Plant 对象或可反向访问 truth 的引用。建议在运行时构造只读、白名单化的 measurement view；Plant truth 只能流向审计和指标路径。机器检查应覆盖：

- Controller 参数类型和返回值必须符合 `ActionRequest`；
- 测量字段不包含 truth、内部状态、真实负载或未来数据；
- Plant 能力缺失不会静默降级；
- 外部输入只能通过声明的 schedule/capability 进入 Plant。

当前 v0 已有对象分离和负向测试，但尚无通用访问审计或沙箱，因此只能证明接口层隔离，不能宣称对任意第三方代码完全封闭。

### 3.3 时间与因果

每次控制周期至少记录：`sample_time`、`action_produced_time`、`action_target_time`、`next_advance_end`、`age_steps` 和可见输入窗口。机器规则包括：

```text
sample_time <= action_produced_time <= action_target_time <= next_advance_end
plant_time_next > plant_time_current
future Plant output 不得进入当前 Controller 输入
```

任何时间倒退、未来数据泄漏或声明外延迟都应 fail-closed。多速率和事件控制器必须在配置中声明周期、采样年龄和允许的 preview 窗口。

### 3.4 安全、数值和资格

安全检查在指标计算前执行，并独立于性能指标。非有限观测、非有限动作、越界动作、收敛失败和 Plant 异常应停止后续动作，保留已产生的波形和失败原因。资格检查回答“数据是否可用于指定实验”，不能被“指标看起来很好”覆盖。

### 3.5 生命周期状态

状态只能通过状态机 API 转换，不能直接编辑 Manifest 字符串。最小转换为：

```text
CREATED -> RUNNING -> RUN_OK | RUN_FAILED | INCOMPLETE
RUN_OK -> QUALIFIED | DISQUALIFIED
QUALIFIED -> COMPARABLE | LEARNING_ELIGIBLE
LEARNING_ELIGIBLE -> LEARNING_UPDATED | LEARNING_REJECTED
```

每次转换应记录触发事件、检查结果、规则版本、时间戳和失败原因。安全失败、契约不匹配、来源未知和人工未批准不得进入 `LEARNING_ELIGIBLE`。

### 3.6 来源、版本、Hash 与产物

正式运行前检查源码提交、工作树状态、环境锁定、Plant/Controller/Contract Hash、配置规范化 Hash 和输出目录冲突。运行后检查必需产物、相对路径、SHA-256、Manifest Schema 和跨文件引用。当前 v0 已实现规范化 JSON、原子发布和产物索引，但 `source_commit`、环境版本和组件 Hash 仍可能为 `unknown/unavailable` 或占位值；这类运行只能是探索性或不完整证据。

## 4. 检查时机与失败动作

| 时机 | 必查项目 | 失败动作 | 证据 |
|---|---|---|---|
| Preflight | Schema、能力、初始状态、schedule、来源 Hash、审批状态、输出冲突 | 拒绝启动；生成 `preflight.json` | 配置快照、环境快照、检查明细 |
| 每周期 | 观测有限性、字段白名单、时间因果、动作类型/限幅、Plant 推进 | 立即停止后续动作；`RUN_FAILED` 或 `INCOMPLETE` | `events.json`、`safety.json`、失败日志、已产生样本 |
| 运行结束 | 资格、必需产物、Manifest、Hash、状态转换 | `DISQUALIFIED`、`INCOMPLETE` 或保留 `RUN_FAILED` | qualification、artifact index、状态事件 |
| 比较前 | 基线契约、初始状态、时间网格、输入一致性 | 禁止标记 `COMPARABLE` | comparison check |
| 学习/发布前 | 人工批准、证据等级、适用范围、来源完整性 | 禁止进入学习或正式发布 | approval record、release decision |

失败处理必须区分“运行失败”“数据不合格”“信息不足”和“人工拒绝”，避免用一个成功指标覆盖不同风险。

## 5. 人工边界审查与审批记录

### 5.1 必须人工审查的边界

- 需求拥有者、最终批准人和责任矩阵；
- Plant 是否代表目标电路，参数和初始状态是否合理；
- 安全阈值、额定值和停止条件是否有工程依据；
- 结果是否可支持 `mechanism`、`functional`、`physical-performance` 或 `research-safety` 级别结论；
- 是否可以作为正式基线、比较基准或学习输入；
- 是否可使用和发布第三方模型、论文、器件资料；
- 是否将结论扩展到未验证的硬件、功率等级或实时环境。

### 5.2 结构化审查单

建议每个边界项使用以下记录：

```text
review_id / boundary_id
question
scope_and_assumptions
evidence_refs
machine_checks_summary
decision: approved | rejected | limited | incomplete
limitations
reviewer / reviewed_at
```

修改公共契约、Schema、Plant 参数、初始状态、安全合同、证据等级或学习准入策略时，应关联 `change_id` 和审批记录。未批准的结果可以用于探索和诊断，但不得自动升级为正式基线或学习数据。

## 6. 证据产物与追溯关系

建议的运行目录在现有 v0 产物上增加检查和审批文件：

```text
runs/<run_id>/
├─ manifest.json
├─ config.snapshot.json
├─ environment.json
├─ preflight.json                 # 规划中
├─ qualification.json
├─ safety.json
├─ metrics.json
├─ events.json
├─ samples.json / samples.npz
├─ contract_checks.json           # 规划中
├─ state_transitions.json         # 规划中
├─ comparison_check.json          # 规划中
├─ approvals/                     # 规划中
│  └─ boundary_review.json
└─ logs/
```

`manifest.json` 是索引和状态摘要，不应成为唯一证据。每个产物应有相对路径和 SHA-256；Manifest 自身不纳入自身索引，最终包可另行生成包级 Hash。图表、报告和比较结果必须引用 `run_id` 与产物 Hash，不能只保存脱离运行目录的图片。

## 7. 验收矩阵

| 能力 | 正向验收 | 反向/故障验收 | 当前状态 |
|---|---|---|---|
| Schema/配置 | 合法配置通过并生成快照 | 缺字段、非法单位、Hash 错误拒绝 | 部分已实现 |
| 信息隔离 | Controller 仅收到 measurement | 尝试读取 truth/Plant 对象被拒绝或无此字段 | 接口隔离已实现；沙箱未实现 |
| 时间因果 | 单调周期和合法 target time 完成运行 | 未来动作、时间回退、未来输出导致 fail-closed | 基础已实现 |
| 安全 | 有限值和限幅动作完成 | NaN/Infinity、越界动作停止并留痕 | 基础已实现 |
| 状态机 | 合法状态按顺序转换 | 非法跳转、人工未批进入可选学习扩展被拒绝 | 状态摘要已有；完整 Guard 未实现 |
| Provenance/Hash | 已知来源和产物 Hash 可复核 | dirty/unknown 来源不能冒充正式基线 | 占位信息仍存在 |
| 产物 | 成功运行原子发布、索引可校验 | 缺产物、重复 run_id、异常中断保留失败证据 | 基础已实现 |
| 人工审查 | 审查单、审批人和限制可追溯 | 无审批不得发布；接入可选学习扩展时不得进入学习 | 尚未实现 |
| 可视化追溯 | 图表引用 run 和 Hash | 脱离原始运行的图表被拒绝 | 尚未实现 |

## 8. 当前实现与未来实现边界

### 当前 v0 已能证明

- Fake Plant/Controller 可通过公共契约运行；
- Controller measurement 与 Plant truth 在接口层分离；
- 基础时间单调、动作时间、非有限值和动作限幅检查存在；
- 运行结果采用临时目录、原子发布和 SHA-256 产物索引；
- Experiment/Manifest Schema 和基础失败测试可运行。

### 当前 v0 尚不能证明

- 对任意第三方 Controller 的完全信息隔离；
- 真实 PySpice/Ngspice 模型的数值收敛、物理代表性和跨环境复现；
- 完整多速率/事件因果语义；
- dirty 基线拒绝、完整环境锁定和真实组件 Hash；
- 状态机 Guard、人工审批、比较准入和可视化追溯；学习准入仅在用户启用可选学习/适配扩展时需要。

## 9. 实施顺序建议（仅方案）

1. 固化约束目录和规则 ID，建立 `constraint -> checker -> artifact -> owner` 映射。
2. 实现 Preflight、Lifecycle 和统一检查结果格式，先覆盖现有 Fake backend。
3. 补齐 provenance、dirty 基线和组件 Hash，再迁移真实 Plant。
4. 增加结构化人工审查和审批文件，并把发布入口设为 fail-closed；若用户启用可选学习/适配扩展，再对该扩展的学习入口设为 fail-closed。
5. 最后实现比较、可视化和报告追溯，确保每个派生结果都能回到原始 run。

## 10. 未决问题

- 正式需求拥有者、公共契约批准人和最终工程责任人尚未指定；
- `research-safety` 证据等级的具体审批标准尚未定义；
- 真实 Plant/Controller 的沙箱或访问审计机制尚未选型；
- 运行失败后哪些文件必须追加写入、哪些文件只读，尚未形成不可变存储策略；
- 审批记录的存储位置、签名方式和长期保留策略尚未确定；
- 可视化工具链及其版本、图表 Hash 和报告格式尚未确定。

本方案不改变当前代码行为，也不把上述未来能力描述为已完成能力。其作用是为后续实现、人工核验和 Agent 验收提供共同的检查语言和证据边界。

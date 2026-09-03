# v0 架构与接口契约

**状态**：规范草案 v0.1

本文是实现约束，不是产品设计约束。它定义公共运行层必须提供的边界，专项
Plant、Controller 和实验仍由使用者实现。

## 1. 核心对象

公共运行层由五个对象组成：

```text
ExperimentSpec -> Runner -> PlantAdapter
                         -> ControllerAdapter
                         -> Safety/Qualification
                         -> ArtifactWriter
```

### 1.1 ExperimentSpec

必须是可序列化、可校验的配置对象，结构以 `../schemas/experiment.schema.json` 为
最小机器校验基线，至少包含：

- `experiment_id`、`run_id`、`schema_version`；
- `plant_id`、`controller_id` 及其配置；
- `timebase`：仿真步长、控制周期、采样时刻和总时长；
- 初态策略、输入/负载 schedule、随机种子；
- 安全合同和资格规则的引用及内容 Hash；
- 输出目录和结果保留策略。

禁止从当前机器环境、当前工作目录或隐式全局变量补齐关键字段。

### 1.2 PlantAdapter

Plant 必须提供以下语义（具体 Python 类型可由实现选择）：

```text
capabilities() -> CapabilitySet
reset(initial_state) -> PlantState
advance(action, duration) -> PlantObservation
snapshot() -> SerializableSnapshot
restore(snapshot) -> PlantState
```

约束：

- `advance` 只能接受执行动作和允许的外部输入，不能接受 Controller 的内部状态；
- `PlantObservation` 必须区分 Controller 可见测量和仅供离线审计的内部真值；
- 单位必须明确，推荐 SI 单位，角度统一使用 degree 或 radian 之一；
- 不支持的能力必须显式返回，不得静默降级；
- 收敛失败、时间轴断裂和非有限值必须成为结构化错误。

### 1.3 ControllerAdapter

Controller 的最小语义为：

```text
reset(controller_state, seed) -> ControllerState
observe(measurement, command, timing, state) -> ActionRequest
```

`measurement` 只能包含测量合同允许的字段；不得传入 Plant 对象、仿真器、
真实负载、内部结温、未公开的真实状态或事件结束后的数据。

`ActionRequest` 至少包含：

- 请求动作及其单位；
- 动作产生时刻和目标执行时刻；
- 限幅前请求值、限幅后值和限幅原因；
- Controller diagnostics；
- 可选的可序列化状态更新。

### 1.4 Runner

Runner 负责时序，不负责替实验作结论。每个控制周期按以下顺序执行：

1. 读取当前时钟和 schedule；
2. 从 Plant 获取本周期允许的测量；
3. 调用 Controller 一次；
4. 依次执行安全投影、执行延迟、量化和动作；
5. 推进 Plant；
6. 记录事件、测量、动作、错误和审计信息。

Runner 必须保证单调时钟、明确的采样年龄和确定的调用次数。Controller 异常、
Plant 异常或安全触发后，默认停止后续动作并进入失败/隔离状态。

## 2. 时间和因果契约

- 所有时间戳使用单调仿真时间，单位为秒。
- `sample_time <= action_time <= next_advance_end`；违反时必须拒绝运行。
- `age_steps`、采样延迟、执行延迟和 PWM 生效时刻必须记录。
- 预览输入必须在配置中声明其可见时间范围；未来 Plant 输出永远不可见。
- 多速率 Controller 必须声明控制周期和允许的观测更新频率。
- 事件前特征一旦冻结，不得在事件结束后重新计算并影响当前动作。

## 3. 复位、随机性和状态

- 每次正式运行必须显式选择 cold start、warm start 或 qualified snapshot。
- Snapshot 必须带来源 Plant/合同 Hash 和生成时间，不能跨不兼容合同恢复。
- 所有随机过程使用显式 seed；未提供 seed 的运行只能标记为 exploratory。
- Controller 状态、Plant 状态和学习候选状态必须分别保存。

## 4. 能力扩展

扩展通过 capability 声明完成，例如 `continuous_time`、`switching_detail`、
`thermal_state`、`multirate_control`。Runner 只依赖能力，不依赖具体拓扑名称。
新增能力必须同时提供：接口说明、最小示例、正向测试、失败测试和 Schema 版本。

## 5. 必须测试的契约

- fake Plant + fake Controller 的最小闭环；
- Controller 无法读取 Plant truth 的接口测试；
- 未来数据注入不改变当前动作的因果测试；
- 非有限值、收敛失败、时间倒退和能力缺失的 fail-closed 测试；
- reset/snapshot/restore 的一致性测试；
- 多次相同 seed、配置和代码版本运行的确定性测试。

# 编译后电路的执行适配器（`CircuitPlant`）v1

**状态：** 契约文档。本文描述当前实现的能力边界，不是发布批准，不是对任何仿真
结果的主张，也不是电气正确性证明。

**前置：** `docs/64_circuit_topology_authoring_v1.md` 解决了"我画出的电路能不能变成
网表"。本文解决剩下的一半："这份网表能不能被内核当作一个 Plant 来推进时间"。

## 1. 从编译到执行

`pe_sim.circuit_compile` 是纯文本层：它不执行、不写文件、不导入后端。
`pe_sim.backends.circuit_plant.CircuitPlant` 是执行层：它把同一个描述符按控制窗口
反复编译，把每个窗口的末状态作为下一个窗口的初状态，并复用已评审的 ngspice 进程
边界执行。

这不是第二条求解路径：网表仍然由 `compile_topology` 生成，执行仍然经过
`pe_sim.backends.process` 的进程适配器，解析仍然经过 `pe_sim.backends.wrdata`。
新增加的只有"哪个窗口、什么初值、什么外部输入"这三个决定。

## 2. 适配器契约

`CircuitPlant` 实现 Runner 使用的 Plant 形状：

| 方法 | 行为 |
| --- | --- |
| `capabilities()` | `continuous_time`、`external_input`、`snapshot` |
| `action_policy()` | 单一 `duty ∈ [0, 1]` |
| `reset(state)` | 接受 `inductor_current_a` / `current_a`、`output_voltage_v` / `vout`，以及以元件 id 为键的状态；回显它实际接受的键 |
| `observe()` | `vout`、`inductor_current`，以及 `duty`、`switch_node_voltage`、输入电压、负载电阻 |
| `advance(action, duration, external_input)` | 解一个零阶保持窗口，返回目标时刻的观测 |
| `snapshot()` / `restore()` | 记录并恢复每个电感电流与电容电压 |

测量约定：

- `vout` 是**唯一一个 `resistor_load` 的正端到负端电压**。没有负载或有两个负载的
  描述符在构造期拒绝，因为内核的测量契约只命名一个输出电压。
- `inductor_current` 是描述符顺序中的第一个电感电流；没有电感时为 0。
- `switch_node_voltage` 是第一个不接参考网的开关输出端电压；没有开关时为 0。
- 最多一个 `voltage_source` 可以被外部 `vin_v` 更新。额外的固定源照常渲染，但
  一个外部标量无法指向它们，因此更新会被拒绝而不是猜一个。

`vout` 的"正端减负端"是刻意的：负载可以浮在非参考节点之间，只读正端会把它当成
对地电压。

## 3. 窗口、相位与续跑

Runner 拥有时钟；适配器在每次 `advance` 时：

1. 计算窗口终点（当前控制窗口的终点，或请求的时长，取较长者）；
2. 若缓存覆盖目标时刻且 `duty`、输入电压、负载电阻都未变，直接插值读取；
3. 否则重新编译并求解，`carrier_phase_s = 窗口起点 mod 开关周期`；
4. 从解出的表里读取目标时刻的每个电感电流与电容电压，作为下一个窗口的初值。

**续跑是状态等价，不是位等价。** 只携带元件状态（电感电流、电容电压），不持久化
ngspice 内部求解器状态。实测在异步 Buck 上，`snapshot → restore → 下一窗口` 与
连续运行给出逐位相同的结果；但这不构成一般保证，比较仍应使用数值容差。

参数注入与状态注入都不改变 `descriptor_fingerprint`：描述符仍然是调用方引用的
身份，而 `CompiledCircuit.digest` 标识具体这个窗口的网表（含初值、外部输入、占空比
和相位）。因此同一份描述符的多窗口运行有一组不同的网表摘要，但只有一个描述符
指纹。

## 4. 求解器设置与网表必须一致

`CircuitPlant` 的 `NgspiceSolverSettings` 是**唯一**来源：它决定 `.options` 的
`abstol`/`chgtol`/`gmin`/`reltol`/`vntol`、`method`、`set numdgt` 和 `.temp`。
Manifest 记录的 `solver_settings` 就是写进网表的那一组值。

构造参数 `integrator` 只是对 `NgspiceSolverSettings.integration_method` 的覆盖：
传入时就地替换，不传就使用设置对象自己的方法。这样不存在"清单里写 gear、网表里
写 trap"的可能。

## 5. CLI：`simulate-topology`

```text
pe-sim simulate-topology <topology.json> \
  --duty 0.5 --duration 1e-3 [--step S] [--control-period S] \
  [--carrier-phase S] [--integrator trap|gear|euler] [--timeout S] \
  [--ngspice PATH] [--output-dir DIR]
```

这是一次**开环、恒定占空比**的检查，不创建 run package。它先走
`validate-topology` 的同一份校验，再编译、执行、输出：

- 成功：`pe-sim.topology-simulation.v1`，退出码 0；
- 文档/编译/请求问题：沿用 `pe-sim.rejection.v1`，退出码 2；
- 求解器失败：`status = "FAILED"`、`category = "backend_process_failure"`，退出码 1。

报告包含网表摘要、描述符指纹、控制模式、求解器身份、末点观测，以及每个信号的
`min`/`max`/`last`。带 `--output-dir` 时额外写出：

- `simulation.json`：与 stdout 相同的报告（含 `artifacts` 路径）；
- `samples.json`：`pe-sim.topology-samples.v1`，`time_s` 与全部信号列。

样本点数上限 2,000,000；超过时拒绝并要求用 `--step` 抽稀，而不是截断。

`--carrier-phase` 的含义与 `compile-topology` 完全一致：窗口起点模开关周期。
实现方式是把一次性窗口的绝对起点设为该相位，因此报告里的 `time_s` 是从这个相位
起算的绝对时间。

## 6. 证据边界

- `CircuitPlant` 在运行时注册 `circuit-topology` 求解路径，**不写入**
  `reviewed_identities.json`。因此 `review_status` 恒为 `not_pinned`：
  - 探索性运行（非 `formal_comparison`）且状态为 `QUALIFIED` 时，Runner 记录的
    `evidence_level` 是 `functional`；
  - 形式比较被评审状态封顶为 `limited`；要求已评审实现的策略下直接失败；
  - 无论哪种情况，`pe_sim.credibility` 的 `implementation_review` 因子都是
    `limited`，不是 `pass`。
  "`functional` 是使用层级，不是"这个电路被评审过"的声明。
- 描述符没有评审记录。编译器能证明的只是"这份图被渲染成了它声明的器件"，
  不能证明电路电气正确、收敛、稳定或可用。
- 适配器不提供 `analytical_steady_state`。没有解析平衡点就诚实地报告"不适用"，
  不拿数值末点冒充解析参考。
- **死区与体二极管。** 已评审的同步 Buck 模板不建模体二极管；在两个栅极都关断的
  死区里，电感电流被强行截断。`CircuitPlant` 忠实复现了这个模型（与
  `NgspiceSyncBuckPlant` 的末段结果在 1e-3 内一致），但这不是"同步 Buck 收敛到
  6 V"的证明。用户可以在描述符里并联一个 `freewheeling_diode` 来显式提供续流
  路径；那会是一份不同的、由用户负责的电路。

## 7. 内置 Plant 注册

`circuit-topology` 已注册为内置 Plant，可以在实验文档里直接写：

```json
{
  "plant_id": "circuit-topology",
  "plant_config": {
    "topology": { "schema": "topology-descriptor-v1", "...": "..." },
    "control_period_s": 1e-05,
    "sample_step_s": 5e-08
  }
}
```

`plant_config.topology` 必须是一个完整的描述符文档；工厂先走与
`validate-topology` 相同的校验，再构造执行适配器。声明表把 `topology` 声明为
`object`，把 `integrator` 声明为带 `enum` 的字符串，因此 `describe
circuit-topology` 仍然能回答“这个 plant_config 能写什么”。

该 Plant 在目录里是 `third-party` 信任级别，因为电路由调用方编写，项目没有评审
它。它不声明 `topology_exporter`（描述符已经在 `plant_config` 里，硬造一个从标量
身份参数反推图结构的导出器会描述另一份电路），因此工作台导出会报告“没有拓扑导出
器”，而不是画一份猜出来的图。

## 8. 未做

- **多控制关系。** 编译器只理解 `single_duty_complementary`。多相、交错、三电平
  需要各自的声明与渲染。
- 子电路、`.include`、SPICE 子集导入（Step C）。

## 9. 证据

| 测试 | 固定的事实 |
| --- | --- |
| `tests/test_circuit_plant.py` | 输出网与元件名来自描述符、`not_pinned`、网表选项与记录的求解器设置一致、两个负载拒绝、reset 回显与未知键拒绝 |
| `tests/test_circuit_plant.py`（真实 ngspice） | 异步 Buck 与已评审适配器末值一致；两个窗口与一个长窗口一致；`snapshot → restore` 与连续窗口逐位一致；外部 `vin_v` 改变下一窗口；互补电路与已评审同步适配器末值一致；`pe-sim run` 以 `plant_id = "circuit-topology"` 发布包 |
| `tests/test_circuit_plant.py`（CLI） | 缺少求解器时的拒绝信封；成功报告与 `simulation.json`/`samples.json` 的形状与点数一致 |

## 10. 相关文档

| 文档 | 关系 |
| --- | --- |
| `docs/64_circuit_topology_authoring_v1.md` | 词表、校验、编译 |
| `docs/31_ngspice_backend_scope_v1.md` | 进程边界 |
| `docs/22_stage12_evidence_levels_v1.md` | 证据阶梯 |
| `docs/38_kernel_review_v0.md` | 评审 pin 的边界 |
| `docs/releases/v0.21.0.md` | 本次发布说明 |

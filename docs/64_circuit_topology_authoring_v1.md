# 电路拓扑的 Agent 编写面（Step B 前半）v1

**状态：** 契约文档。本文描述当前实现的能力边界，不是发布批准，不是对任何仿真
结果的主张，也不是电气正确性证明。

**前置：** `docs/63_agent_facing_spec_validation_v1.md`（Step A）解决了"我能在
`plant_config` 里写什么"。本文解决另一半问题："我能画出什么电路，它能不能变成
可求解的网表"。

## 1. 问题

Step A 之后，agent 能拿到 `plant_config` 的完整声明表，却拿不到电路结构的信息：

- `TopologyDescriptor` 只支持**单向导出**：内置 plant 类 → 描述符 → DOT/邻接矩阵。
  它的 `component.kind` 是任意非空字符串，写错一个字母不会被发现。
- 描述符构造函数只检查"端口指向已声明的网"，不检查网是不是悬空、图是不是连通、
  控制网有没有驱动、两个理想电压源有没有形成短路回路。
- `TopologyDescriptor.from_dict` **要求** `fingerprint`，而计算它需要实现规范化
  JSON 的 SHA-256 —— 这对人手写描述符是陷阱，不是门槛。
- 描述符和网表之间没有通路：内置拓扑能跑，是因为 `render_buck_netlist` 手写了
  那一份电路，而不是因为描述符被编译了。

Step B 的目标是把描述符从"展示用途的单向导出"变成"可校验、可编译的双向契约"。

## 2. 本次交付的三个层次

| 层次 | 模块 | 回答的问题 |
| --- | --- | --- |
| 器件词表 | `pe_sim.circuit_devices` | 有哪些元件种类，每种需要哪些端口和参数 |
| 结构校验 | `pe_sim.topology_validation` | 这份描述符是不是一份自洽、连通、可解释的电路图 |
| 网表编译 | `pe_sim.circuit_compile` | 这份描述符能不能渲染成求解器可执行的网表 |

三个模块都是 stdlib-only，都不导入 `pe_sim.backends`，都不启动求解器、不写文件。

## 3. 器件词表（`pe-sim.circuit-devices.v1`）

词表是**器件**词表而非**拓扑**词表：这里只有 `voltage_source`、`inductor`、
`voltage_controlled_switch` 这样的元件，没有 buck、boost 之类拓扑名。这保持了
`docs/43` 的不变量 I5（内核不命名任何引擎、拓扑、厂商），同时给了编译器一个有限
可检查的面。

`DeviceSpec` 声明：

- `kind`：规范名；
- `ports`：**有序**端口表，每个端口声明 `port_id`、允许的 `net_kinds`、`direction`、
  说明。顺序就是 SPICE 元件卡的节点顺序。
- `parameters`：复用 `plant_parameters.ParameterSpec`，因此电路参数与
  `plant_config` 字段用同一套形状描述 —— 学会一个就学会了另一个。
- `spicetype`：元件字母；`None` 表示**没有元件卡**。
- `model_card`：需要的 `.model` 种类（开关、二极管）。
- `aliases`：旧拼写别名，例如 `diode` → `freewheeling_diode`。

### 3.1 参数的可选性规则

**有默认值的参数可省略，其余必须给出。** 编译器绝不为没有默认值的参数挑一个
看起来合理的数字。因此 `inductance_h` 缺失是拒绝，而 `initial_current_a` 缺失
等价于 `0.0`。

### 3.2 保留注解

`DEVICE_ANNOTATIONS = {"phase_index"}` 是允许出现在任意元件 `parameters` 里的
**保留注解**：它描述多相变换器里第几相，供显示使用，永不被渲染。它由元件自身
的节点表达，所以不参与网表。

### 3.3 当前的八种器件

| kind | SPICE | 端口（按序） | 必填参数 |
| --- | --- | --- | --- |
| `voltage_source` | `V` | positive, negative | `voltage_v` |
| `pulse_source` | `V` | positive, negative | `switching_frequency_hz` |
| `inductor` | `L` | input, output | `inductance_h` |
| `capacitor` | `C` | positive, negative | `capacitance_f` |
| `resistor_load` | `R` | positive, negative | `resistance_ohm` |
| `voltage_controlled_switch` | `S` | input, output, control | 无 |
| `freewheeling_diode` | `D` | anode, cathode | 无 |
| `averaged_switch` | **无** | input, output, control | 无 |

`averaged_switch` 是词表里唯一不可渲染的种类。它是真实存在的种类，因为项目发布了
使用它的平均化参考模型；但它命名的是**周期平均行为块**，没有元件卡。编译器遇到
它必须拒绝，而不是发明一份从未被评审过的电路。

### 3.4 端口的网种类约束

- `control` 端口只能接 `control` 网；
- 电源类元件的 `negative` 端口可接 `electrical` 或 `reference`（浮地源是合法电路，
  高边栅极驱动的参考节点就是开关节点）；
- `capacitor` / `resistor_load` 的 `negative` 同理；
- `freewheeling_diode.anode` 可接 `electrical` 或 `reference`。

`thermal` 与 `other` 网在描述符契约里合法，但没有电学含义，因此校验器报
`unsupported_net_kind`，编译器拒绝。

## 4. 结构校验（`pe-sim.topology-validation.v1`）

### 4.1 两类描述符

校验器明确区分两件事：

- **一份合法的描述符**：图结构自洽；
- **一份可编译的电路**：编译器能渲染。

一个含行为块（`averaged_switch`）的描述符是前者而不是后者，报 `not_compilable`
**警告**，`ok` 仍为 `true`。把这两件事混成一个错误，会让项目自己发布的平均化参考
描述符被判为非法。

### 4.2 封闭的 26 个码

错误（`severity = "error"`）：

| 码 | 含义 |
| --- | --- |
| `invalid_document` | 文档不是 JSON 对象 |
| `schema_violation` | 顶层字段、标识符、版本号、字段集不合法 |
| `unsupported_net_kind` | `thermal` / `other` 网 |
| `unsupported_device_kind` | 词表外的元件种类（带 `suggestion`） |
| `unknown_port` | 器件没有这个端口（带 `suggestion`） |
| `missing_port` | 器件缺少必需端口 |
| `port_net_kind_mismatch` | 端口接错了网种类 |
| `unknown_net` | 端口指向未声明的网 |
| `duplicate_component_id` / `duplicate_net_id` / `duplicate_port_id` | 标识符重复 |
| `unknown_parameter` | 器件没有这个参数（带 `suggestion`） |
| `missing_parameter` | 缺必填参数 |
| `invalid_parameter_value` | 参数类型或范围不合法 |
| `fingerprint_mismatch` | 声明的指纹与内容不符 |
| `isolated_net` | 网被 0 个或 1 个端口绑定（悬空端子） |
| `missing_reference_net` / `multiple_reference_nets` | 参考网数量不对 |
| `disconnected_graph` | 电路不连通，列出离岛网名 |
| `control_net_undriven` / `control_net_multiple_drivers` | 控制网无驱动 / 多个驱动 |
| `voltage_source_loop` | 理想电压源构成回路（短路） |

警告（`severity = "warning"`，不影响 `ok`）：

| 码 | 含义 |
| --- | --- |
| `not_compilable` | 含无元件卡的行为块 |
| `actuator_missing` | 没有任何执行器 |
| `multiple_control_nets` | 多于一个控制网 |
| `no_output_load` | 没有负载，也就没有可观测输出 |

`WARNING_ISSUE_CODES` 是这四个码的封闭集合，测试钉住它，因此一个码不可能悄悄
改变严重级别。

### 4.3 一条缺陷只报一次

这是 Step A 学到的规则，这里同样适用并有三处具体体现：

1. 端口拼错时只报 `unknown_port`，不再同时报 `missing_port`；
2. 文档还有其他错误时不报 `fingerprint_mismatch` —— 一次编辑不该被记两次，等文档
   其他部分干净后它会立刻出现；
3. 控制网永远不报 `isolated_net`：要么它被正确驱动，要么 `control_net_undriven`
   已经精确指出了真正的问题。

### 4.4 建议必须是可以安全照做的

与 Step A 相同：短于 6 个字符的键、以及相似度低于 0.75 的匹配，一律不给
`suggestion`。把垃圾字段悄悄映射成一个真实参数，比什么都不说更糟。

### 4.5 `checked` 只列真正跑过的检查组

`CHECK_GROUPS = (document_shape, nets, components, connectivity, control, sources,
fingerprint)`。schema 不识别时 `checked` 为空；文档没有 `fingerprint` 键时不列
`fingerprint`。"干净"与"没检查"必须可以区分。

### 4.6 指纹是可选的

`build_descriptor(document)` 接受**没有 `fingerprint`** 的文档。已经记录下来的
描述符需要指纹来证明没被改过；正在编写的描述符还没有这段历史，要求 agent 自己
实现规范化 JSON 的 SHA-256 是陷阱而非门槛。有指纹时照常校验；没有时
`validate-topology` 在信封里给出 `computed_fingerprint`。

`tests/test_topology_validation.py` 断言 `build_descriptor` 与
`TopologyDescriptor.from_dict` 在两者都适用时结果相等，因此这两条路径不会发散。

## 5. 网表编译（`circuit-netlist-v1`）

### 5.1 三条性质

- **确定性文本。** 元件卡按描述符顺序输出，数值统一用 `%.12e`，不写时间、主机名
  或路径。因此网表文本的 SHA-256 就是这份编译电路的稳定身份。
- **拒绝而非发明。** 渲染不了的描述符给出代码，绝不做近似。
- **单一被控标量。** 内核只命令一个无量纲占空比，因此编译器要求**恰好一个执行器**
  与**恰好一个载波**，脉冲宽度由传入的 `duty` 推导，而不是从描述符里读。

### 5.2 `pulse_source` 不存占空比

描述符声明载波频率，调用方声明命令。二者永不合并成同一个字段，因此同一份描述符
可以为任意占空比编译，且 `digest` 会随之改变（`descriptor_fingerprint` 不变）。

### 5.3 载波相位与尾脉冲

`carrier_phase_s` 是求解窗口开始时载波所在的相位（窗口起点模开关周期）。分段
（零阶保持）运行必须传它：不传就等于每个窗口都把载波重启在上升沿，会在所有采样
值都看起来合理的情况下每周期多送或少送能量。

相位落在**导通区间内部**时，SPICE 无法用“平移的上升沿”表达“一个在 t=0 之前
就开始的脉冲还剩多少”。渲染器因此引入两个**合成节点**和一个求和源：

```text
V2 pwm_pulse 0 PULSE(0 1 7e-06 1e-09 1e-09 5e-06 1e-05)
V3 pwm_tail  0 PWL(0 1 2e-06 1 2.001e-06 0)
B1 pwm 0 V = V(pwm_pulse) + V(pwm_tail)
```

合成节点名在冲突时追加下划线直到不撞名 —— 描述符里恰好用了同名网络是合法文档，
把两个节点静默合并会污染电路。这是**载波的渲染细节**，不是对电路的修改，因此控制
网上仍然只有一个驱动元件的声明事实不变。

实测（同一电路、四个相位，D = 0.5，120 µs 窗口，末 30 个样本均值）：

| 相位 | 尾宽 | vout 差 | iL 差 |
| --- | --- | --- | --- |
| 0 | 0 | 6.1e-05 | — |
| 3 µs | 2 µs | 1.3e-04 | — |
| 6 µs | 0 | 2.3e-04 | — |
| 9.9 µs | 0 | 3.9e-05 | — |

最大偏差 2.4e-04，相对于 iL ≈ 6.8 A 是 3.5e-05 相对误差。

### 5.4 封闭的 14 个拒绝码

`ambiguous_reference_net`、`invalid_carrier_phase`、`invalid_duration`、`invalid_duty`、
`invalid_integrator`、`invalid_option`、`invalid_parameter`、`invalid_sample_step`、
`missing_parameter`、`multiple_actuators`、`multiple_carriers`、`no_carrier`、
`not_compilable`、`unsupported_net_kind`。

其中 `multiple_actuators` 有一条重要的物理理由必须写在文档里：**同一个控制网驱动
两个开关，会把互补器件渲染成同时导通。** 同步 Buck 有两个栅极网，因此当前编译器
拒绝它 —— 这不是缺陷，而是“双控制网与单一占空比的推导关系尚未声明”这一事实的
诚实表达。见 §7。

### 5.5 与已评审模板的等价性

编译器的正确性不是断言出来的，是对比出来的。对同一电路、同一命令：

- **结构上**：`tests/test_circuit_compile.py` 断言编译网表的
  `(元件字母, 节点序列)` 与 `render_buck_netlist` 的完全一致；
- **数值上**：设置 `PE_SIM_NGSPICE` 后，测试把两份网表交给同一个 ngspice 求解并
  比较末段均值。

2026-10-29 实测（D = 0.5，Vin = 12 V，L = 100 µH，C = 470 µF，R = 10 Ω，
fsw = 100 kHz，20 ms 窗口）：

```text
compiled  vout=6.022243  iL=0.638947
reviewed  vout=6.022284  iL=0.638935
delta     vout=4.077e-05  iL=1.190e-05
```

差约 7 ppm，来自采样点数不同（446992 vs 450094 行），不是模型差异。
`D·Vin = 6.000 V`，与 6.022 V 的偏差是二极管正向压降，两个网表相同。

## 6. CLI 表面

| 命令 | 用途 | 退出码 |
| --- | --- | --- |
| `pe-sim describe-devices [kind]` | 打印器件词表或单个器件声明 | 0；未知种类 2 |
| `pe-sim validate-topology <file>` | 打印 `pe-sim.topology-validation.v1` 信封 | 无错误 0，否则 2 |
| `pe-sim compile-topology <file> [--duty D] [--duration S] [--step S] [--carrier-phase S] [--integrator trap\|gear\|euler] [--output P] [--json]` | 编译并打印/写出网表 | 0；否则 2 |

三个命令都在 `load_builtin_plants()` **之前**分派：电路问题不需要任何 plant 注册，
也不需要求解器。拒绝信封沿用 `pe-sim.rejection.v1`，`compile-topology` 的编译失败
额外带 `compile_code` 与 `details`；**没有新增拒绝码**，
`REQUEST_REJECTION_CODES` 未变。

`describe-devices` 与 `describe` 对拼错的名称使用同一类信封：一个是
`unknown_plant`（Step A 已有的码），一个是 `unsupported_device_kind` 放在 `issues`
里，`error_code` 保持 `invalid_request`。

## 7. 边界与非主张

- 校验通过 **不等于** 电路电气正确、数值稳定、收敛、可发布。
- 编译成功 **不等于** 模型被评审。agent 编写的拓扑没有评审记录，按 `docs/22` 的
  证据阶梯，它产出的结果最高只能是 `limited`，绝不可能是 `comparable`。
- **一次只有八个器件、三种网种类**，没有互感、变压器、受控源、非线性器件、
  子电路、`.include`。
- **不支持互补控制。** 同步 Buck、模型侧控制 Buck、峰值电流 Buck 的描述符都声明
  了两个执行器共用或分用控制网；当前编译器只接受一个执行器加一个载波。
  下一步是让描述符声明"多个控制网如何从单一占空比推导"（例如
  `metadata.control = {mode: "single_duty_complementary", dead_time_s: ...}`），
  并在编译器里实现；本次**未做**。
- **`dead_time_s` 被记录但不渲染。** 声明在 `pulse_source` 上，因为描述符若有
  两个载波就必须说明它们的关系；单执行器编译器没有位置应用它。
- 不做 SPICE 子集导入（Step C）。

## 8. 未做：`CircuitPlant` 与通用 `ModelBuilder`

本次交付的是**编写与编译**，不是**执行**。仍然缺：

- `CircuitPlant`：从 `NgspiceBuckPlant` 抽出、以描述符驱动网表渲染与信号名解析的
  执行适配器；
- 通用 `ModelBuilder`：与 `BuckNetlistParameters` 解耦的模型参数载体；
- 互补控制的声明与实现（见 §7）。

这些都需要触及 `pe_sim.backends` 与评审身份（`reviewed_identities.json`），因此
必须作为一次显式决策单独提交，而不是混在编写面里。

## 9. 证据

| 测试 | 固定的事实 |
| --- | --- |
| `tests/test_topology_validation.py` | 26 码封闭、6 份内置描述符零错误、单缺陷单报告、建议安全门、`checked` 真实性、`build_descriptor` 与 `from_dict` 一致、标识符规则与 `topology._ID_RE` 一致 |
| `tests/test_circuit_compile.py` | 14 码封闭、结构与已评审 Buck 逐端子一致、摘要稳定与可复算、相位与尾脉冲（真实 ngspice 三个相位均一致）、全部拒绝路径、无 backend 导入 |
| `tests/test_agent_topology_surface.py` | 三个 CLI 命令的信封、退出码、不建目录、空注册表可用 |

## 10. 相关文档

| 文档 | 关系 |
| --- | --- |
| `docs/63_agent_facing_spec_validation_v1.md` | Step A：`plant_config` 的声明与校验 |
| `docs/24_stage11_contracts_v1.md` | `TopologyDescriptor` 契约 |
| `docs/43_kernel_roadmap_v1.md` | 不变量 I5（内核不命名拓扑） |
| `docs/22_stage12_evidence_levels_v1.md` | 证据阶梯 |
| `docs/45_error_and_rejection_codes_v1.md` | 拒绝信封 |
| `docs/releases/v0.20.0.md` | 本次发布说明 |

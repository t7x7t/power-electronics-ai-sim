# SPICE 网表导入（v1）

**状态**：设计说明。本文件说明 `pe_sim.spice_import` 接受什么、拒绝什么，以及为什么
导入不产生第二条执行路径。它不主张任何电路在电气上正确。

## 1. 问题

`docs/64` 让 agent 能用 JSON 写一张电路图；但工程里更常见的起点是一份已经存在、
已经被人工核对过的 `.cir`。让使用者把网表逐行重打一遍，既浪费也引入新的出错机会。

本层只做一件事：**把网表子集翻译成同一种描述符**。翻译结果不是新格式、不是新
契约，就是 `topology-descriptor-v1`，因此后续的一切（结构校验、网表编译、窗口
执行）与手写描述符完全共用。

## 2. 一条路径，不是第二条执行路径

`pe_sim.spice_import` 是**前端**，不是求解器：

| 问题 | 由谁回答 |
| --- | --- |
| 这份网表能不能表达成器件词表？ | `spice_import`（`import_ok`） |
| 这个图自洽吗（连通、参考网、控制网）？ | `topology_validation`（`validation`） |
| 能不能渲染成网表、能不能按这个 duty 跑？ | `circuit_compile` |
| 跑出来的波形可信到什么程度？ | `CircuitPlant` + 证据阶梯（`docs/65`、`docs/22`） |

模块本身 stdlib-only，不导入 `pe_sim.backends`，不写文件、不启动求解器、
不创建运行目录。`tests/test_spice_import.py` 用 AST 检查它自己的导入语句，因此
"导入一个网表"永远不需要一台装着仿真器的机器。

## 3. 接受的子集

### 3.1 元件

| 字母 | 器件种类 | 节点顺序 | 值/模型 |
| --- | --- | --- | --- |
| `V` | `voltage_source` | positive, negative | `12`、`DC 12` |
| `V` | `pulse_source` | positive, negative | `PULSE(v0 v1 td tr tf pw per)` |
| `L` | `inductor` | input, output | 电感量，可选 `IC=` |
| `C` | `capacitor` | positive, negative | 电容量，可选 `IC=` |
| `R` | `resistor_load` | positive, negative | 电阻值 |
| `S` | `voltage_controlled_switch` | input, output, control, **control-reference** | `.model` 名 |
| `D` | `freewheeling_diode` | anode, cathode | `.model` 名 |

`S` 的第四个节点在词表里**不是端口**：它是栅极电压的参考节点，编译器从驱动该
控制网的载波负端推导它。导入器因此校验两者一致（§3.5），而不是把它塞进描述符。

其余字母（`B`、`E`、`F`、`G`、`H`、`I`、`J`、`K`、`M`、`Q`、`T`、`U`、`W`、`X`、
`Y`、`Z`）一律 `unsupported_element`。特别注意 `B`：本项目自己的编译器在"窗口
起点落在导通区间内部"时会渲染一个 `B` 源来合成尾脉冲（`docs/64` §5.3），但那是
**窗口的渲染细节**，不是电路元件，因此这样的网表不能再被导回来。这不是缺陷，
是同一条边界的两个方向。

### 3.2 指令：接受、丢弃并记录、拒绝

**接受**：`.param NAME=<数值>`、`.model NAME D(...)`、`.model NAME SW(...)`、
`.end`（结束读取）。

`.param` 只接受数字字面量。元件值里的 `{name}` 会被替换成该数字；`{name}` 未经
声明、或 `.param` 的值本身是表达式（`{2*Vin}`）都是 `unsupported_expression`。
替换一个就写在文件里的常数不增加任何导入器没有的信息；求值一个表达式则是发明。

**丢弃但逐条记录**（`ignored_directives`，含行号与理由）：`.tran`、`.options`/`.opt`、
`.temp`、`.save`、`.print`、`.plot`、`.op`、`.dc`、`.ac`、`.four`/`.fourier`、
`.measure`/`.meas`、`.width`、`.title`、`.nodeset`，以及**整个 `.control … .endc`
块**。理由是同一个：这些描述**怎么跑**，而"怎么跑"由编译出的拓扑与运行配置拥有。
丢掉它们不改变电路，因此不构成静默丢失 —— 每一条都在信封里点名。

**拒绝**：`.include`/`.inc`、`.lib`、`.subckt`/`.ends`、`.func`、`.global`、`.ic`、
`.if`/`.else`/`.elseif`/`.endif`、`.step`、`.csparam`、`.data`/`.enddata`，以及任何
未知指令。理由分两类：前几种让电路依赖导入器**从未读过的文本**（被包含的文件、
子电路内部、条件分支），`.ic` 则是因为元件级的 `IC=` 才是无歧义的写法 ——
同一节点上两个电容时，`.ic V(vout)=5` 无法说明该给哪一个。

一个 `.subckt` 块只报一次，块内各行不再报错：一个不支持的构造是一处缺陷，
不是每行一处。

### 3.3 数值

接受十进制数，可带 `T`、`G`、`Meg`、`Mil`、`K`、`m`、`u`、`n`、`p`、`f` 比例
因子，因子之后的多余字母忽略（`10kohm` = 10 kΩ）。**不接受 `a`（atto，1e-18）**：
`ic=0.45A` 在 SPICE 里是 4.5e-19，`docs/34` 记录过这个陷阱造成的静默零初值，
所以这里让它变成 `invalid_value` 而不是一个看起来正常的数。

解析出的值统一规范到 **15 位有效十进制数字**。`100u` 在双精度里是
`9.999999999999999e-05`，而编译网表只渲染 13 位有效数字；15 位比双精度粗、比
渲染精度细，既去掉了表示噪声（`100u` 读作 `0.0001`），又不丢失电路能承载的任何
信息。

### 3.4 节点与网名

- 节点 `0` 变成参考网 `gnd`。这个名字是保留的：真实存在一个叫 `gnd` 的节点时，
  它变成 `gnd_2`，两个节点**不会**被合并（合并会静默短路一段电路）。
- 其它节点名清洗成合法标识符（`in/rail` → `in_rail`），冲突时按出现顺序追加
  `_2`、`_3`。映射表在信封的 `net_name_map` 里，重命名从不隐藏。
- 网种类由连接推导：`0` → `reference`；作为载波正端或 `S` 控制端出现的节点 →
  `control`；其余 → `electrical`。SPICE 没有热的/机械的网，因此导入结果也只会有
  这三种。

### 3.5 控制网与控制参考

- 载波必须从**正端**驱动控制网；`V2 0 pwm PULSE(...)` 是 `unsupported_control_reference`。
- `S` 的第四节点必须等于驱动该控制网的载波的负端。浮地/高边栅极驱动是合法电路：
  `Vpwm pwm sw PULSE(...)` 配 `S1 vin sw pwm sw SWMOD` 会被接受，`sw` 仍是
  `electrical`，`pwm` 是 `control`。
- 控制网上没有载波时，第四节点必须是 `0`；否则同样拒绝。此时描述符仍然是合法的，
  `control_net_undriven` 由校验器报出 —— 一处缺陷只报一次。

### 3.6 占空比不是导入项

网表里的 `PULSE(0 1 0 1e-9 1e-9 5e-6 1e-5)` 含有一个具体脉宽，也就是一个具体
占空比。本项目的占空比是**编译期命令**（`docs/64` §5.2），不是描述符字段。导入器
因此：

- 记录 `observed_duty`（按元件 id），供人对照；
- 给出 `pulse_width_not_imported` 警告，说明这条信息不会进入描述符；
- 绝不把宽度写进描述符，也绝不把它当作"默认 duty"。

两份网表如果载波宽度不同，导入后得到的是同一份描述符 —— 因为它们本来就是
同一个电路的两个工作点。

### 3.7 互补控制

两个载波 + 两个开关的网表（同步 Buck 就是）可以导入，但导入的描述符**不申报**
两者的关系。导入器给 `complementary_control_undeclared` 警告并说明补法：补上

```json
"metadata": {"control": {"mode": "single_duty_complementary", "dead_time_s": 1e-7}}
```

之后即可编译。从网表的两个 PULSE 卡推断"它们互补"是猜时序，而两个独立载波
也可以实现任何时序；本层不猜。

## 4. 封闭的 18 个代码

错误（`severity = "error"`）：

| 码 | 含义 |
| --- | --- |
| `empty_netlist` | 没有元件 |
| `missing_title` | 首行看起来是元件卡或指令（SPICE 会把它当标题吞掉） |
| `malformed_card` | 令牌数不对、`.endc` 无配对、`.control` 未闭合、重复参数 |
| `unsupported_element` | 元件字母不在词表内 |
| `unsupported_directive` | 指令会改变电路形态或引入间接引用 |
| `unsupported_expression` | `{…}` 表达式，或引用未声明的 `.param` |
| `unsupported_source_function` | `PWL`/`SIN`/`EXP`/`AC`，或初值非 0 的 PULSE |
| `invalid_value` | 数值不可解析、非有限、或超出物理范围 |
| `duplicate_element` | 元件名重复（SPICE 名字大小写不敏感） |
| `unknown_model` | 元件引用了没有 `.model` 卡的模型 |
| `unsupported_model_type` | `.model` 类型不是 `D`/`SW`，或与元件种类不符 |
| `unsupported_parameter` | 模型或元件参数在词表里没有对应项 |
| `duplicate_model` | 同名 `.model` 声明两次 |
| `missing_ground` | 没有任何元件接到节点 `0` |
| `unsupported_control_reference` | 载波接在参考网，或开关的栅极参考不是驱动载波的负端 |

警告（`severity = "warning"`，不影响 `import_ok`）：

| 码 | 含义 |
| --- | --- |
| `unused_model` | `.model` 卡没有任何元件引用 |
| `pulse_width_not_imported` | 载波的脉宽（即占空比）是编译期命令，不进入描述符 |
| `complementary_control_undeclared` | 多载波多开关，描述符未申报它们的关系 |

`WARNING_IMPORT_CODES` 是这三个码的封闭集合，测试钉住它，因此一个码不可能悄悄
改变严重级别。`SpiceImportIssue` 在构造期校验"码 ↔ 严重级别"的对应，所以一个
警告码无法以 error 形式出现，反之亦然。

一次扫描报告全部缺陷：`Q1` 与 `.include` 同时出现时两条都在 `issues` 里，按
（严重级别，行号，码）排序。同一行同一个码只报一次。

## 5. 两个答案

信封（`pe-sim.spice-import.v1`）同时给出：

- `import_ok`：网表是否被完整翻译；
- `validation`：校验器对产出的描述符的判定（原样嵌入
  `pe-sim.topology-validation.v1`）；
- `ok`：两者都成立。

把两者合成一个布尔值，会让"用了词表外的元件"和"有一个悬空端子"看起来是同一类
问题。分开报告之后，`import-spice` 的退出码仍然只有一个，但读输出的客户端知道
该修哪一层。

```json
{
  "ok": true,
  "import_ok": true,
  "topology_id": "demo_buck",
  "title": "demo buck",
  "netlist_sha256": "550be0e0…",
  "descriptor": { "schema": "topology-descriptor-v1", "…": "…" },
  "descriptor_fingerprint": "616311dd…",
  "counts": {"components": 7, "nets": 5, "ignored_directives": 2},
  "element_kinds": {"voltage_source": 1, "pulse_source": 1, "…": 1},
  "net_name_map": {"0": "gnd", "vin": "vin", "pwm": "pwm", "…": "…"},
  "component_name_map": {"V1": "v1", "Vg": "vg", "…": "…"},
  "observed_duty": {"vg": 0.5},
  "ignored_directives": [{"directive": ".tran", "line": 11, "reason": "…"}],
  "issues": [{"code": "pulse_width_not_imported", "…": "…"}],
  "error_count": 0,
  "warning_count": 1,
  "validation": {"schema": "pe-sim.topology-validation.v1", "ok": true, "…": "…"},
  "notes": ["…"]
}
```

`descriptor` 里**没有** `fingerprint`：它是一份正在编写的描述符，`docs/64` §4.6
已经说明要求作者先算一个规范化 SHA-256 是陷阱而不是门槛。信封里另给
`descriptor_fingerprint`，是校验器/构建器算出的那个值。

## 6. CLI

```powershell
pe-sim import-spice .\circuit.cir --topology-id buck_from_netlist --output .\topology.json
```

| 命令 | 行为 | 退出码 |
| --- | --- | --- |
| `pe-sim import-spice <netlist> [--topology-id ID] [--output P]` | 打印信封；给了 `--output` 时把描述符写到 `P` | 0；`ok` 为假 2 |

- 读不到文件是**请求**被拒绝（`invalid_request`），因为根本没有文档；
- 只有导入**整体**成功时才写文件：`ok` 同时要求“网表翻译成功”与“产出的描述符通过
  结构校验”。一份能翻译但拓扑不自洽的网表（例如有悬空端子）会退出 2、不写任何文件，
  因此后续命令不会拿到一份看起来可用的半对文档；
- `--output` 只在真正写盘时出现在信封里，失败时没有 `output` 字段；
- 与 `validate-topology`/`compile-topology` 一样在注册表加载之前分派，不需要任何
  已注册 plant，不需要求解器；
- 没有新增拒绝码，`REQUEST_REJECTION_CODES` 未变。

## 7. 证据

| 测试 | 固定的事实 |
| --- | --- |
| `tests/test_spice_import.py` | 18 码封闭与"码 ↔ 严重级别"对应；六个字母与词表双向一致；模块不导入后端；手写 Buck 的七张卡与五个网种类；数值替换与表达式拒绝；atto 后缀不被静默应用；节点重命名表；`gnd` 不与 `0` 合并；浮地栅极被接受、错误栅极参考被拒绝；`.control` 与全部分析指令被记录；`.subckt` 一处缺陷一次报告；两个答案分离；往返后元件卡逐张相同 |
| 同上（`PE_SIM_NGSPICE`） | 导入电路与描述符编译电路求解到同一工作点（末 500 点均值差 < 1e-3） |
| `tests/test_agent_topology_surface.py` | `import-spice` 的信封、写出、退出码、不建运行目录、空注册表可用 |

**往返实测**（2026-10-29，D = 0.5，Vin = 12 V，L = 100 µH，C = 470 µF，R = 10 Ω，
fsw = 100 kHz，10 ms）：把 `compile-topology` 的输出重新导入再编译，

```text
lines differing (comments excluded): 1
  reviewed: wrdata output.dat v(vin) v(sw) v(vout) v(pwm) i(L1)
  imported: wrdata output.dat v(vin) v(pwm) v(sw) v(vout) i(L1)
rows: 225870 225870
v(vout)  mean 5.891977557  delta 0.000e+00
i(l1)    mean 0.660046660  delta 0.000e+00
v(sw)    mean 5.415989133  delta 0.000e+00
v(pwm)   mean 0.453800000  delta 0.000e+00
```

唯一的差异是 `wrdata` 里保存信号的**顺序**（它跟随描述符里网的声明顺序，而导入
后的顺序是网表里的出现顺序）；元件卡、模型卡、`.options`/`.temp`/`.tran` 全部
逐字节相同，因此解也逐点相同。

## 8. 边界与非主张

- **六个字母、一种波形。** 没有受控源、非线性器件、变压器、互感、子电路、
  `.include`、参数表达式、条件段、`.ic` 节点初值。
- **`.control` 与全部分析指令被丢弃。** 记录在 `ignored_directives` 里，但它们
  承载的意图（例如某个 `alter` 序列）不会被重建。如果一段网表的语义主要写在
  `.control` 里，导入后得到的只是它的电路骨架。
- **词表本身的边界会传给导入结果。** `voltage_v`、`initial_current_a`、
  `initial_voltage_v` 在词表里是非负的，因此负电源或负初值的网表会导入成功、
  但在 `validation` 里报 `invalid_parameter_value`。这是同一条规则的两次显现，
  不是导入器的缺陷。
- **导入 ≠ 评审。** 一次成功的导入不说明电路电气正确、数值稳定或收敛。证据上限
  与其他 agent 编写的拓扑相同：形式比较封顶 `limited`，探索性运行记 `functional`，
  任何情况下都不可能是 `comparable`。
- **往返只保真于电路，不保真于文本。** 注释、`.param` 名称、指令、保存信号顺序
  都可能变化；元件与连接不会。

## 9. 相关文档

| 文档 | 关系 |
| --- | --- |
| `docs/64_circuit_topology_authoring_v1.md` | 器件词表、结构校验、网表编译；本层的前置 |
| `docs/65_circuit_execution_v1.md` | 执行适配器与 `simulate-topology` |
| `docs/34_pyspice_optional_backend_v0.md` | `ic=0.45A` 的事故记录 |
| `docs/24_stage11_contracts_v1.md` | `TopologyDescriptor` 契约 |
| `docs/22_stage12_evidence_levels_v1.md` | 证据阶梯与上限 |
| `docs/45_error_and_rejection_codes_v1.md` | 拒绝信封 |
| `docs/releases/v0.22.0.md` | 本次发布说明 |

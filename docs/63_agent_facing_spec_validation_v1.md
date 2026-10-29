# Agent 接入面：参数声明与结构化文档校验（v1）

**状态：** 面向 Agent 与自动化客户端的接入面说明。它描述"写文档前查什么、写完后验
什么、被拒时读到什么"，不改变任何求解行为、证据等级或评审指纹。

**适用版本：** 0.19.0 起。本文只描述已实现并已测试的表面；未实现的部分明确列在 §10。

---

## 1. 问题：扁平错误字符串无法被修复

在 0.19.0 之前，CLI 对实验文档的错误只返回一条扁平字符串，例如：

```text
unsupported ngspice-buck plant_config fields: ['not_a_key']
```

这对人够用，对 Agent 不够用：

- **无法定位**：字符串里没有机器可导航的位置，Agent 只能整篇重写或猜。
- **无法区分拼写与契约**：`vin` 是 `input_voltage_v` 的简写误用，还是这个 plant
  根本不接受输入电压？两者需要的修法完全不同。
- **只能晚期失败**：多数错误只在"运行目录已创建、后端进程已启动"之后才暴露，代价是
  一次失败的 run，而不是一次免费的检查。
- **一次只报一个**：修一个字段、再跑一次、再撞下一个，回合数高且更容易改错。

本文件描述的表面把同一批检查提前到"建 run 之前"，并把结果变成可定位、可分支、可
批量修复的记录。

## 2. 三个面

| 面 | 命令 | 回答的问题 | 何时调用 |
| --- | --- | --- | --- |
| 查 | `pe-sim describe <plant_id>` | 这个 plant 接受哪些 `plant_config` 字段？类型、单位、边界、默认值是什么？ | 写文档之前 |
| 验 | `pe-sim validate <config>` | 我写的文档形状对吗？字段名对吗？值在声明范围内吗？ | 写完之后、建 run 之前 |
| 跑 | `pe-sim run <config>` | 真跑一次；被拒时附带同一份结构化 issues | 验通过之后 |

### 2.1 Agent 循环

| 步骤 | 动作 | 成功信号 | 失败时读什么 |
| --- | --- | --- | --- |
| 1. 查 | `pe-sim describe <plant_id>` | `schema: pe-sim.plant-parameters.v1` 与 `parameter_schema` | 拒绝信封 `unknown_plant`（拼错 plant 名） |
| 2. 写 | 按声明写实验文档 | — | — |
| 3. 验 | `pe-sim validate <config>` | `"ok": true`，退出码 0 | `issues[]` 的 `path` / `code` / `suggestion` |
| 4. 修 | 按 `path` 改字段，按 `suggestion` 改名 | — | — |
| 5. 跑 | `pe-sim run <config>` | 终端结果 `pe-sim.run-result.v1` | 仍是 `issues[]`（见 §2.3） |

第 3 步可以重复任意次：`validate` 不建目录、不起求解器，失败的代价只是一次进程
启动。第 5 步仍可能失败，但那时失败的是**运行**，不再大概率是文档拼写。

### 2.2 `describe`

`pe-sim describe <plant_id>` 打印 `pe-sim.plant-parameters.v1`：

```json
{
  "schema": "pe-sim.plant-parameters.v1",
  "plant_id": "buck",
  "parameters": [
    {
      "name": "input_voltage_v",
      "kind": "number",
      "unit": "V",
      "minimum": 0.0,
      "exclusive_minimum": true,
      "maximum": null,
      "default": 12.0,
      "nullable": false,
      "description": "DC voltage applied to the converter input port."
    }
  ],
  "parameter_schema": {
    "type": "object",
    "additionalProperties": false,
    "properties": {
      "input_voltage_v": {
        "type": "number",
        "exclusiveMinimum": 0.0,
        "default": 12.0,
        "description": "DC voltage applied to the converter input port.",
        "x-unit": "V"
      }
    }
  },
  "schedule_keys": ["input_voltage_v", "load_ohm", "load_resistance_ohm", "time_s", "vin_v", "vref"],
  "unknown_keys": "rejected"
}
```

- `parameters` 按字段名排序，来自 `pe_sim.plant_parameters.PLANT_PARAMETERS`。内置
  声明覆盖 12 个 plant：`fake`、`fake-load`、`buck`、`boost`、`multiphase-buck`、
  `ngspice-buck`、`ngspice-sync-buck`、`whole-run-buck`、`model-control-buck`、
  `peak-current-buck`、`whole-run-switching-buck`、`circuit-topology`。
- `parameter_schema` 是同一份声明的 JSON Schema 视图：闭合对象
  （`additionalProperties: false`），数值字段带 `exclusiveMinimum` / `minimum` /
  `maximum` / `default`，带量纲的字段带 `x-unit`（JSON Schema 没有单位词表，这里
  不要求读者从字段名推断单位）。
- `unknown_keys: "rejected"` 声明工厂行为：未知 `plant_config` 键不会被"忽略"。
- `schedule_keys` 是 `input_schedule` 接受的键词表。它必须和参数表一起给出：只用
  `describe` 写文档的 agent，如果不知道调度词表，会把 `plant_config` 写对、却在
  `input_schedule` 里编造一个键。该表同样由 `tests/test_plant_parameters.py` 钉在
  注册表上。
- 表的唯一性由 `tests/test_plant_parameters.py` 保证：它把声明与
  `builtin_plants` 的 `*_DEFAULTS` 与工厂构造器逐项对照，工厂改了而声明没改会失败。
- 未知 `plant_id` 返回拒绝信封：`status: CONFIG_REJECTED`，
  `error_code: unknown_plant`，并**同样**携带 `validation` 与 `issues`（`path` 为
  `/plant_id`），因此 `describe` 与 `run` 的拒绝可以用同一个解析器处理。

`describe` 只读声明表，不构建 plant、不调用工厂、不探测后端。

### 2.3 `validate` 与 `run` 的 `issues`

`pe-sim validate <config> [--schema PATH] [--backend NAME]` 打印
`pe-sim.spec-validation.v1`：

```json
{
  "schema": "pe-sim.spec-validation.v1",
  "plant_id": "buck",
  "ok": false,
  "error_count": 1,
  "warning_count": 1,
  "issues": [
    {
      "code": "unknown_parameter",
      "path": "/plant_config/vin",
      "message": "plant 'buck' does not accept plant_config field 'vin'",
      "expected": "one of: capacitance_f, inductance_h, input_voltage_v, integration_step_s, load_resistance_ohm",
      "suggestion": "input_voltage_v",
      "severity": "error"
    },
    {
      "code": "exploratory_contract_default",
      "path": "/contracts",
      "message": "contracts safety and qualification use the all-zero exploratory hash, ...",
      "expected": "a non-zero reviewed hash for each contract",
      "suggestion": null,
      "severity": "warning"
    }
  ],
  "checked": ["schema", "plant_id", "plant_config", "controller", "input_schedule", "contracts"]
}
```

（上面两段 JSON 都是节选：`describe` 只展示了一个字段，`validate` 的 `message` 省略
了后半句。字段名与结构是完整的。）

规则：

- `ok` 只由 `error_count == 0` 决定；`warning` 不影响 `ok`，也不改变退出码。
- 退出码：`ok: true` → 0；存在任意 `error` → 2。语义与拒绝信封的退出码一致。
- `issues` 先按 `severity` 排序（`error` 在前），再按 `path` 排序；同一份文档重复
  运行得到逐字节相同的输出，便于 Agent 做 diff。
- 文档本身不是 JSON 对象时，`validate` 仍输出本信封，`issues[0].code` 为
  `invalid_document`，`path` 为 `""`。
- 配置文件无法读取或不是合法 JSON 时，输出的是**拒绝信封**（`status:
  CONFIG_REJECTED`，`error_code: invalid_request`，附 `reason: "invalid_document"`），
  因为此时连"文档"都不存在，没有可定位的字段。
- `--backend NAME` 是一致性断言，不是构建请求：它只解析注册表并比较 `backend_id`，
  不匹配时仍用既有拒绝码 `backend_mismatch`。它不会构建 plant。
- `--schema PATH` 只替换 `schema` 阶段的 JSON Schema（默认是打包在
  `pe_sim.schemas` 里的那份），其余阶段不变。
- `checked` 列出**本次实际生效**的阶段，顺序与 `CHECKED_STAGES` 一致，但会因文档而
  缩短：`plant_id` 无法解析时，`plant_config` 与 `input_schedule` 没有对象可查，`checked`
  就只有 `["schema", "controller", "contracts"]`（控制器阶段是否能跑见下一条）。
  消费方可以据此区分"检查过且干净"与"没检查"——只看 `ok` 无法区分这两件事。
- **控制器阶段（`controller`）。** `controller_id` 是安全标识符由 schema 管；能解析成
  注册表里的一条注册时，再比对 `controller_config` 的键是否都在该控制器声明的
  `config_keys` 里。两个新码：`unknown_controller`（`/controller_id`，带候选与安全
  建议）与 `unknown_controller_config`（`/controller_config/<key>`）。
  三个边界必须说清楚：
  - **只查键名，不查值类型。** 注册声明只有 `config_keys`，没有类型表；再造一份类型
    声明会让这里变成第二个模型，所以值写错（如 `"kp": "fast"`）仍要到 `run` 才拒。
  - **注册表为空时该阶段不跑**，`checked` 里没有 `controller`，文档也不会因拼写未知
    控制器而失败：装了但没加载的控制器与拼错的名字，验证器无法区分。
  - **`collect_issues` 不加载注册根。** `pe-sim validate` 作为组合根只填入控制器注册
    （不构建 plant、不启动求解器、不建目录）；库层调用者自己决定装哪些控制器。

`pe-sim run` 在文档存在结构化问题时，输出的是同一个拒绝信封（`status:
CONFIG_REJECTED`，`error_code: invalid_request`），并额外带两个可选字段：

| 字段 | 值 |
| --- | --- |
| `validation` | `"pe-sim.spec-validation.v1"` |
| `issues` | 与 `validate` 相同的 `SpecIssue` 数组（含 `severity`） |

本轮**没有新增拒绝码**：拒绝码仍是 `invalid_request`，`REQUEST_REJECTION_CODES`
未变。`issues[].code` 不是拒绝码，见 `docs/45` §2.1。

## 3. JSON Pointer 约定

`path` 是 RFC 6901 JSON Pointer，指向提交的文档本身：

| 位置 | Pointer |
| --- | --- |
| 文档根 | `""`（空字符串） |
| 顶层字段 | `/plant_id`、`/plant_config` |
| `plant_config` 的一个字段 | `/plant_config/vin` |
| 调度表第 0 项的一个字段 | `/input_schedule/0/vinn_v` |
| 含 `/` 或 `~` 的键 | 按 RFC 6901 转义为 `~1` 与 `~0` |

约定：`path` 指向可修复的最小对象。字段级问题指向字段本身；对象级问题（缺少必填
项、合同哈希）指向该对象。因此 Agent 可以按 `path` 直接做定位式修改，不必理解
`message` 的自然语言。

## 4. 问题码词表（7 个，封闭）

`pe_sim.spec_validation.SPEC_ISSUE_CODES` 是封闭集合；`SpecIssue` 拒绝未声明的码。

| 码 | 严重级别 | 位置（典型） | 含义 | 是否带 `expected` / `suggestion` |
| --- | --- | --- | --- | --- |
| `invalid_document` | error | `""` | 文档不是 JSON 对象 | `expected: "object"`；无建议 |
| `schema_violation` | error | 出错字段 | 不满足共用实验 JSON Schema（打包版或 `--schema` 指定版） | `expected` 为 `"<validator>: <value>"`，例如 `"required: [...]"` |
| `unknown_plant` | error | `/plant_id` | `plant_id` 既不在声明表，也不在注册表 | `expected` 列出已知 id；`suggestion` 给出最接近的 id |
| `unknown_parameter` | error | `/plant_config/<key>` | 该 plant 不接受这个 `plant_config` 字段 | `expected` 列出允许字段；`suggestion` 给出最接近的字段名 |
| `invalid_parameter_value` | error | `/plant_config/<name>` | 值类型或边界不符声明（例如非数值、`<= 0`、超出 `maximum`） | `expected` 形如 `"number > 0"`；无建议 |
| `unknown_schedule_key` | error | `/input_schedule/<i>/<key>` | 该 plant 不接受这个调度字段 | `expected` 列出允许键；`suggestion` 给出最接近的键 |
| `exploratory_contract_default` | **warning** | `/contracts` | `safety` / `qualification` 合同使用全零探索性 hash | `expected` 为非零已评审 hash；无建议 |

两点必须说清楚：

1. `exploratory_contract_default` 是 warning，不是 error：全零合同是文档化的探索性
   默认值，不是缺陷。它只说明这次运行**最多只能**被报告为探索性，不能用于正式比较。
   `ok` 仍然是 `true`。
2. 词表只收录**有产生者**的码。一个永远不会出现的码会让客户端写出永远不执行的分支，
   所以它不被接受进词表；新增一个码属于契约事件，需要同步更新本文件与测试。
3. `suggestion` 是**提示而非权威**，且宁缺不错。只有当错字与某个真实字段名足够接近
   （整串相似度达阈，或短别名如 `vin`）时才给出；像 `not_a_key` 这样的长垃圾键不会
   得到建议，因为把它"修"成一个真实字段会静默改变文档语义。缺建议不是死路：
   `expected` 总是列出全部合法名。

### 4.1 去重规则

同一个拼写错误可能同时触发 `schema_violation`（`additionalProperties`）与更具体的
`unknown_parameter` / `unknown_schedule_key`。`collect_issues` 会丢弃被具体问题覆盖
的通用 `additionalProperties` 记录，避免要求 Agent 修两次同一个错。根级
（`path == ""`）的 schema 失败永不被丢弃。没有具体问题覆盖时，通用记录保留——例如
`plant_id` 无法解析时，若 schema 层报告了 `additionalProperties` 失败，该记录会
保留，而不是静默。

## 5. 三层校验：结构化层先行，工厂层兜底

| 层 | 实现 | 何时运行 | 拒绝方式 |
| --- | --- | --- | --- |
| 1. 结构化层 | `pe_sim.spec_validation.collect_issues` / `validate_document` | `validate` 与 `run` 的文档预检 | `SpecValidationError`（携带全部 issues） |
| 2. 注册表层 | `cli._validate_schedule_keys` | 解析注册表之后、构建 plant 之前 | `ValueError` → `invalid_request` |
| 3. 工厂层 | `builtin_plants._reject_unknown_keys` 等 | 构建 plant 时 | `ValueError` → `invalid_request` |

三层都是 fail-closed：任何一层拒绝，都不创建运行目录。第 1 层负责"可定位、可批量
修复"，第 2、3 层保证即使有人绕过第 1 层，未知键也不会被静默忽略。第 2、3 层保持
历史消息形态，因为它们服务的调用者还包括不经过 `validate` 的 Python API 用户。

`ngspice-buck` 与 `ngspice-sync-buck` 的注册现在显式声明 `config_keys`
（frozenset），而不是 `None`。这是把工厂本来就有的白名单提前到校验阶段，不是新增
限制：同样的键在工厂层仍然被同一份白名单拒绝。

## 6. `validate` 的边界（明确不做）

`validate` 是一个**纯文档检查**。它：

- 不构建 plant，不调用工厂，不实例化任何适配器；
- 不执行任何适配器实现：网表渲染、开关模型、求解器包装都不运行；
- 不启动求解器，不创建后端进程；
- 不读取、不探测后端可执行文件（未安装 ngspice 时对 `ngspice-buck` 文档照样工作）；
- 不创建运行目录、不写任何文件、不修改输入文档；
- 不需要 `PE_SIM_NGSPICE` 或任何外部程序；只需要安装时已有的 `jsonschema`。

关于模块导入，两个命令的界线不同，需要分开说：

- `describe` 只读 `pe_sim.plant_parameters` 的声明表，不加载注册根，也不导入
  `pe_sim.backends`。在没有安装求解器的机器上它就是纯粹的查表。
- `validate` 默认同样不加载注册根。只有当文档带 `input_schedule`（需要查调度词表）
  或调用方传了 `--backend`（需要解析注册表）时，才会加载内置注册根；这是**模块导入**，
  仍然不实例化、不执行任何适配器。

因此它**不能**：

- 证明电路正确、拓扑合法或参数物理合理；
- 证明数值稳定、收敛、步长足够；
- 替代 safety / qualification / validity 判定（这些只在实际运行中产生）；
- 替代人工评审或发布批准；
- 检查跨字段约束（例如 `output_min < output_max`、
  `integration_step_s <= switching_period / 20`）：这些目前只在模型层校验，见 §10。

另外，`jsonschema` 缺失或 schema 文件不可读属于**安装缺陷**，不是文档问题；这种情况
会抛 `RuntimeError` 并以拒绝信封 `invalid_request` 报出，而不会被伪装成一条可修复的
`SpecIssue`。

## 7. 第三方 plant 路径

第三方通过 `pe_sim.plants` 入口点注册的 plant 没有内置声明表条目，规则如下：

| 情形 | `validate` 行为 |
| --- | --- |
| 注册表能解析该 id | 不报 `unknown_plant`；用注册声明检查，并按其 `schedule_keys` 检查 `input_schedule` |
| 注册声明了 `parameter_schema` | 用该 schema 的 `properties` 做未知键检查与值检查（未知键仍然只报一次，不重复） |
| 未声明 `parameter_schema` | 只做 `schema` 阶段（共享实验 Schema）的形状校验与调度键检查；未知 `plant_config` 键留给工厂层拒绝 |
| 注册表也无法解析 | `unknown_plant`，`expected` 同时列出声明表与已注册 id，`suggestion` 给出最接近者 |

注册表的 `parameter_schema` 字段在注册时被冻结并做形状校验：必须是
`type: object`、必须 `additionalProperties: false`、必须声明 `properties` 映射；若同时
声明了 `config_keys`，两者的键集必须完全相同。这样 catalog 与校验器是同一份契约的
两个视图，不会对同一个 plant 给出两个互相矛盾的答案。该字段
`compare=False, hash=False`：它是声明，不参与注册相等性或哈希，避免同一份注册因字典
顺序而变化。

这一路径的设计取舍是"留通道但不失控"：**导入的是声明，不是可执行网表**。`validate`
最多解析注册声明并读取其 JSON Schema，不会构建适配器、不会调用插件工厂、不会执行
其网表或求解代码（解析入口点会导入插件自己的注册模块，这一点与其他 CLI 命令相同）。
没有声明的插件仍然能在工厂处被拒绝未知键，因此"没有声明"不会退化为"什么都接受"。

需要注意的当前差异已经收窄：`pe-sim run` 与 `pe-sim validate` 现在共用同一条结构化
预检（`validate_document`），因此声明了 `parameter_schema` 的插件在两条路径上会得到
同样的键/值检查。未声明 schema 的插件在两条路径上都只做 shape 检查，未知键仍由
第 2、3 层拒绝。

## 8. 非主张

- `validate` 通过 ≠ 可发布。它只说明文档与当前声明一致。
- `ok: true` 只表示"当前声明的检查没有发现问题"，不表示没有未声明的检查。
- `ParameterSpec` / `parameter_schema` 是**参数声明**，不是 safety 判定、不是资格判定、
  不是物理正确性证明。
- `severity=warning` 不影响 `ok`，也不会阻止 `run`；它只在信封里如实记录。
- 本表面没有新增拒绝码，也没有改变任何既有拒绝码的含义。
- 本表面没有改变求解行为、证据等级推导或评审指纹。

## 9. 证据

| 测试 | 固定的事实 |
| --- | --- |
| `tests/test_plant_parameters.py` | 11 个 plant 的声明与工厂默认值/构造器一致；Schema 闭合 |
| `tests/test_spec_validation.py` | 9 个码封闭（精确等值）、Pointer 形状、去重、插件解 析、控制器阶段（未知 id / 未声明键 / 空注册表）、`checked` 真实性、warning 不影响 `ok` |
| `tests/test_agent_spec_surface.py` | `describe`/`validate`/`run` 三面一致；`validate` 不建目录；无需 ngspice |

## 10. 未做（Step B / Step C 与跨字段约束）

- **Step B**：拓扑结构那一半已在 **v0.20.0** 交付（器件词表、结构校验、网表编译，
  见 `docs/64_circuit_topology_authoring_v1.md`）。仍缺 `CircuitPlant` 执行适配器、
  通用 `ModelBuilder`、载波相位支持与互补控制声明。本表面只覆盖 `plant_config`
  声明，不覆盖拓扑结构声明；两者由不同模块回答，但形状一致。
- **Step C**：SPICE 子集导入器尚未实现；`validate` 不能读取网表并据此校验参数，
  `validate-topology` 也不能。
- **跨字段约束**：`output_min < output_max`、
  `integration_step_s <= switching_period / 20` 这类关系目前只在模型层（适配器构造
  或运行期）校验，`validate` 不做。文档可以在单字段层面全部合法，而在运行期因跨字段
  关系被拒。
- 参数声明只覆盖内置 plant；第三方 plant 的声明质量由插件作者负责，`validate` 只
  执行其声明，不评价声明本身是否合理。

## 11. 相关文档

| 文档 | 关系 |
| --- | --- |
| `docs/45_error_and_rejection_codes_v1.md` | 拒绝信封与 `issues` 可选字段 |
| `docs/03_agent_operating_policy_v0.md` | Agent 强制流程中的 `validate` 步骤 |
| `docs/44_run_interaction_contract_v1.md` | `list-plants` 的 catalog 声明 |
| `docs/64_circuit_topology_authoring_v1.md` | Step B 前半：器件词表、拓扑校验、网表编译 |
| `docs/releases/v0.19.0.md` | Step A 的发布说明 |

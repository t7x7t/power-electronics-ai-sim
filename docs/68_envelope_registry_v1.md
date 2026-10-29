# 信封登记表（v1）

**状态**：契约索引。本文件列出项目会写出的每一个 `pe-sim.<名称>.vN` 信封标签，
说明它由哪个模块产生、描述什么。它不含物理结论，也不新增行为。

## 1. 为什么需要这张表

一个 agent 面对的是 JSON，不是 Python。它要先看 `schema` 字段才能决定用哪个
解析器；如果某个标签只在源码里存在、文档里没有，agent 就只能靠猜。反过来，如果
某个标签只写在文档里、没有任何模块产生它，那是一条空头承诺。

`tests/test_full_module_scan.py` 因此把两个方向都钉住：源码里出现的每个标签必须
在本表中出现，本表中的每个标签必须在源码里出现。

## 2. 两个"问题数组"的键不要混用

`invalid_request` 拒绝信封里有两个可选键，它们的含义不同，不要互相顶替：

| 键 | 值 | 何时出现 |
| --- | --- | --- |
| `validation` | 恒为 `pe-sim.spec-validation.v1` | 实验文档本身有结构化问题 |
| `issues_schema` | 产生 `issues` 的那个词表标签 | 拒绝来自别的词汇表（当前只有设备词表） |

`pe-sim describe-devices <未知名>` 用的是 `issues_schema`
= `pe-sim.circuit-devices.v1`。它**不**填 `validation`：客户端按 `validation`
选择解析器，填错会让"设备词表信封"被"规格校验解析器"读掉，而且两边都叫
`issues`，错读不会报错，只会给出错答案。

## 3. 登记表

### 3.1 授权面（用户与 agent 直接接触）

| 标签 | 产生模块 | 内容 |
| --- | --- | --- |
| `pe-sim.circuit-devices.v1` | `pe_sim.circuit_devices` | 封闭器件词表：每种 kind 的端口、网络种类约束与参数表。`describe-devices` 的输出。 |
| `pe-sim.plant-parameters.v1` | `pe_sim.plant_parameters` | 每台参考装置的参数面（名称、单位、范围、默认值）。`describe` 的输出。 |
| `pe-sim.topology-validation.v1` | `pe_sim.topology_validation` | 拓扑文档的检查信封：`ok`、`issues[]`、`checked[]`。 |
| `pe-sim.spice-import.v1` | `pe_sim.spice_import` | SPICE 网表导入结果：翻译后的描述符、`ignored_directives`、`net_name_map`、警告与是否通过校验。 |
| `pe-sim.topology-simulation.v1` | `pe_sim.cli` | `simulate-topology` 的报告：编译记录、solver 溯源、样本摘要、指标块与证据等级。 |
| `pe-sim.topology-samples.v1` | `pe_sim.cli` | `simulate-topology --output-dir` 写出的逐点样本（`time_s` + 每个信号）。 |
| `pe-sim.waveform-metrics.v1` | `pe_sim.waveform_metrics` | 从一个已求解窗口测出的能量平衡、效率、实测占空比与电感电流统计。 |
| `pe-sim.spec-validation.v1` | `pe_sim.spec_validation` | 实验文档的校验信封：`ok`、`error_count`、`warning_count`、`issues[]`、`checked[]`。 |

### 3.2 拒绝与失败

| 标签 | 产生模块 | 内容 |
| --- | --- | --- |
| `pe-sim.rejection.v1` | `pe_sim.error_codes` | 所有退出码 2 的统一拒绝信封：`status`、`error_code`、`error`，外加可选细节。 |

### 3.3 运行与产物

| 标签 | 产生模块 | 内容 |
| --- | --- | --- |
| `pe-sim.run-result.v1` | `pe_sim.run_events` | `run` 的最终结果信封：状态、产物路径、失败分类、证据等级。 |
| `pe-sim.run-event.v1` | `pe_sim.run_events` | 运行事件流的一条记录。 |
| `pe-sim.run-demo.v1` | `pe_sim.run_events` | `demo` 打包运行的输出。 |
| `pe-sim.trace-contract.v1` | `pe_sim.time_model` | 轨迹契约：时间模型、样本规则、单位与可见性。 |
| `pe-sim.run-inspection.v1` | `pe_sim.inspection` | 已发布运行目录的检视结果。 |
| `pe-sim.run-catalog.v1` | `pe_sim.catalog` | 运行目录的清单。 |
| `pe-sim.run-review.v1` | `pe_sim.review` | 评审记录。 |

### 3.4 来源与身份

| 标签 | 产生模块 | 内容 |
| --- | --- | --- |
| `pe-sim.reviewed-identities.v1` | `pe_sim.identity` | 已评审实现的身份表（`reviewed_identities.json` 的形状）。 |
| `pe-sim.adapter-verification.v1` | `pe_sim.identity` | 适配器身份核验结果。 |
| `pe-sim.attestation.v1` | `pe_sim.attestation` | 一次运行的证明记录。 |
| `pe-sim.attestation-write.v1` | `pe_sim.attestation` | 写出证明时的输入信封。 |
| `pe-sim.attestation-anchor.v1` | `pe_sim.attestation` | 证明的锚点（提交与摘要）。 |
| `pe-sim.attestation-verification.v1` | `pe_sim.attestation` | 证明的校验结果。 |
| `pe-sim.public-api.v1` | `pe_sim.public_api` | `pe_sim.__all__` 的稳定性快照。 |
| `pe-sim.schema-registry.v1` | `pe_sim.schema_registry` | manifest 的模式版本迁移表。 |

### 3.5 能力与分析

| 标签 | 产生模块 | 内容 |
| --- | --- | --- |
| `pe-sim.capabilities.v1` | `pe_sim.capabilities` | 本机可选能力探测（后端可执行文件、版本）。 |
| `pe-sim.model-capability.v1` | `pe_sim.model_capabilities` | 一台装置声明的模型能力面。 |
| `pe-sim.capability-analysis.v1` | `pe_sim.capability_analysis` | 一次 AC / 阻抗 / 损耗分析的输入文档。 |
| `pe-sim.capability-report.v1` | `pe_sim.capability_analysis` | 上述分析的报告外壳。 |
| `pe-sim.ac-analysis.v1` | `pe_sim.ac_analysis` | 单点线性化小信号结果。 |
| `pe-sim.impedance-analysis.v1` | `pe_sim.impedance_analysis` | 输入/输出阻抗与 Middlebrook 比。 |
| `pe-sim.derived-report.v1` | `pe_sim.derive` | 从已有运行派生的报告。 |
| `pe-sim.claim.v1` | `pe_sim.claim` | 一条可被证据支持或反驳的声明。 |
| `pe-sim.compatibility.v1` | `pe_sim.compatibility` | 版本/环境兼容性判定。 |

### 3.6 研究、参数与确认

| 标签 | 产生模块 | 内容 |
| --- | --- | --- |
| `pe-sim.study.v1` | `pe_sim.study` | 研究文档（点集与目的）。 |
| `pe-sim.study-report.v1` | `pe_sim.study` | 研究报告（逐点结果）。 |
| `pe-sim.study-aggregate.v1` | `pe_sim.study` | 研究聚合（统计量、可比性理由）。 |
| `pe-sim.parameter-view.v1` | `pe_sim.parameter_view` | 参数的来源与可信度视图。 |
| `pe-sim.parameter-confirmation.v1` | `pe_sim.parameter_confirmation` | 参数确认请求。 |
| `pe-sim.parameter-confirmation-verification.v1` | `pe_sim.parameter_confirmation` | 参数确认的校验结果。 |

### 3.7 装置与可视化

| 标签 | 产生模块 | 内容 |
| --- | --- | --- |
| `pe-sim.plant-catalog.v1` | `pe_sim.plant_registry` | `plant_catalog()` 的装置清单。 |
| `pe-sim.visualization-service.v1` | `pe_sim.visualization_service` | 只读回环服务的响应外壳。 |

## 4. 版本规则

`v1` 是当前的唯一版本。标签里的数字只有在**同一个键的含义改变**时才提升；新增
键不提升版本，因为客户端必须容忍未知键。`tests/test_full_module_scan.py` 钉住
标签集合本身，所以新增一个信封必须同时改本文件——这是有意的耦合。

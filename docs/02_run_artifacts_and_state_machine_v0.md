# v0 运行产物、状态机与证据规则

## 1. 目录和最小产物

每次运行写入独立目录：

```text
runs/<run_id>/
├── manifest.json
├── config.snapshot.json
├── environment.json
├── qualification.json
├── safety.json
├── metrics.json
├── events.json
├── samples.npz
└── logs/
```

文件写入采用临时目录加原子重命名。Manifest 写入前不得宣称运行完成；缺失必需
文件的目录必须标记为 `INCOMPLETE`。

## 2. 状态机

```text
CREATED -> RUNNING -> RUN_OK
                    -> RUN_FAILED
                    -> INCOMPLETE

RUN_OK -> QUALIFIED
        -> DISQUALIFIED

QUALIFIED -> COMPARABLE
          -> LEARNING_ELIGIBLE

LEARNING_ELIGIBLE -> LEARNING_UPDATED
                  -> LEARNING_REJECTED
```

状态含义必须分离：

- `RUN_OK` 只表示 Runner 正常结束；
- `QUALIFIED` 表示满足本实验资格规则；
- `COMPARABLE` 表示与指定基线的合同、初态、时间网格和输入一致；
- `LEARNING_ELIGIBLE` 表示允许进入学习或适配；
- 任何安全失败、非有限值、未来信息泄漏或合同不匹配都禁止进入学习。

状态不得通过字符串覆盖产生。每个转换应记录检查项、时间、规则版本和失败原因。

## 3. Manifest 最小字段

Manifest 必须包含：

```text
schema_version, experiment_id, run_id, created_at
source_commit, branch, working_tree_status
environment, plant, controller, contracts
timebase, initial_state, random_seed
artifacts, status, qualification, safety, evidence_level
```

机器校验使用 `../schemas/manifest.schema.json`；输入配置使用
`../schemas/experiment.schema.json`。未知字段可以保留，但不得改变已定义
字段的语义；Schema 不兼容变更必须升级主版本。

## 4. Hash 规则

- 算法固定为 SHA-256。
- JSON 使用 UTF-8、排序键、无额外空白的规范化表示；禁止 NaN/Infinity。
- `config_hash`、`plant_hash`、`controller_hash`、`contract_hash` 和文件 SHA-256
  分开保存，不允许用一个总 Hash 隐藏来源。
- 路径在 Hash 前转换为相对路径并使用 `/`；绝对路径、时间戳和临时目录排除。
- Manifest 保存自身以外的产物 Hash；最终包可再生成一个包级 Hash。
- 工作区有未提交修改时必须记录 dirty 状态，正式基线默认拒绝 dirty 运行。

## 5. 资格、安全和证据

安全检查在指标计算之前执行，资格检查独立于性能指标。安全失败采用 fail-closed：
停止动作、保留波形、写入原因，不得由“指标很好”覆盖。

证据等级固定为：

`mechanism`、`functional`、`physical-performance`、`research-safety`、
`hardware-readiness`。

最后一级只允许由真实器件、实时性、硬件和台架验证流程产生，任何仿真 Runner
不得自行设置为 `hardware-readiness`。

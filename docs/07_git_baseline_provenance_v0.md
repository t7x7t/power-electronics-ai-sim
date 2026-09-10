# Git 基线与运行来源记录（v0）

## 目的

一次仿真结果必须能够回答“使用了哪一版源码”。`pe_sim.provenance`
以只读方式读取 Git，不修改仓库，也不替运行者提交文件。Runner 将来源信息
写入 `manifest.json` 的 `source_commit`、`branch` 和 `working_tree_status`，并在
`provenance.status` 中给出下游工具可用的保守判断。

## 三种状态

| 工作树 | `provenance.status` | 含义与建议 |
|---|---|---|
| `clean` | `known` | HEAD、分支和工作树可识别；可作为正式比较候选，仍需人工批准。 |
| `dirty` | `exploratory` | 存在未提交或未跟踪文件；结果保留，但不应作为正式基线或学习输入。 |
| `unknown` | `unknown` | Git 不可用、路径不在仓库或无法读取 HEAD；只能作探索性证据。 |

`unknown` 不会被静默改写为 `clean`。`detached` HEAD 会保留完整 commit，分支字段为
`detached`，仍可由 commit 唯一绑定。

## 运行时行为

```python
from pe_sim.provenance import collect_git_provenance

info = collect_git_provenance()       # 默认使用当前工作目录
info = collect_git_provenance("D:/project")
```

Runner 每次运行都会把 Git 信息复制到 Manifest，同时写入 `environment.git`。
运行不会因为 dirty 或 unknown 自动失败，这是为了保留调试证据；发布、正式比较和
可选学习/适配准入等上层流程应根据状态拒绝或要求人工审批；核心基础设施不会因存在学习字段而自动启用学习。

## 建立基线的建议

1. 审查代码、配置、Schema 和测试后创建一个 clean commit 或 tag。
2. 正式实验前确认 `working_tree_status == "clean"`，并保存 Manifest 与 CI 结果。
3. 任何 dirty 运行都保留其 Manifest 和限制说明，不覆盖已有 `run_id`。
4. 迁移外部工程时另外登记来源 commit 和许可证；Git 身份本身不等于模型正确性。

当前 v0 仅记录 Git 身份，不负责初始化仓库、自动提交、签名或远程发布。项目根目录
是否创建初始仓库以及哪些文件纳入首个基线，应由项目维护者人工决定。

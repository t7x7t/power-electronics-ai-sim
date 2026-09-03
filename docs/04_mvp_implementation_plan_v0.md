# v0 MVP 实施路线与验收标准

## 1. MVP 目标

在全新环境中，用一个命令运行 PSFB 理想模型的负载阶跃，并得到可校验的 Manifest、
波形、指标和明确状态。MVP 不包含器件、电热、在线学习、硬件接口或自动参数优化。

## 2. 任务顺序

| ID | 任务 | 主要产物 | 验收标准 |
|---|---|---|---|
| A0 | 项目元数据 | `pyproject.toml`、环境锁定、LICENSE、CI | 新环境可安装，基础测试可收集 |
| A1 | 公共类型和单位 | `pe_sim/contracts` | fake Plant/Controller 契约测试通过 |
| A2 | Runner 最小闭环 | `pe_sim/runtime` | reset、采样、动作、推进顺序可审计 |
| A3 | 结果写入 | `pe_sim/artifacts`、Manifest Schema | 成功、失败、未完成均生成合法状态 |
| A4 | PSFB 参考接入 | `examples/psfb_pi_step` | 一条命令生成完整运行目录 |
| A5 | 资格与安全 | `pe_sim/qualification`、`pe_sim/safety` | 非有限值、越界、合同不匹配 fail-closed |
| A6 | 可复现性 | 重跑脚本和回归基线 | 相同输入可复现，dirty 基线被拒绝 |
| A7 | 第二对象验证 | 第二种简单拓扑或独立 fake backend | 公共接口不依赖 PSFB 字段 |

## 3. 推荐目录

```text
power-electronics-ai-sim/
├── src/pe_sim/{contracts,runtime,artifacts,qualification,safety,plants,controllers}
├── schemas/
├── configs/
├── examples/
├── tests/{unit,integration,validation,slow}
├── docs/
├── agent/
└── runs/                 # 本地产物，不提交
```

## 4. Gate 与 Definition of Done

### Gate 0：骨架

目录、依赖、许可证、CI 和文档索引齐全；没有业务代码也能运行 `--help` 和基础测试。

### Gate 1：契约

fake Plant + fake Controller 完成闭环；时间倒退、缺失能力、非法单位和非有限值都有
失败测试；Controller 不能访问 Plant truth。

### Gate 2：证据

成功、失败、资格失败、合同不匹配各有 fixture；Manifest 通过 Schema；所有产物有
相对路径和 SHA-256；失败日志不可被覆盖。

### Gate 3：参考实验

陌生用户可从 README 创建环境并运行 PSFB PI 负载阶跃；结果可重复，图形和指标能回到
同一 run；结论带证据等级和限制。

### Gate 4：泛化检查

第二种拓扑或 backend 通过同一公共契约；若失败，应收窄项目“通用”声明，而不是给
公共接口增加 PSFB 专用字段。

## 5. 变更管理

每项任务绑定一个短分支或变更集。修改接口、Schema、安全合同或基线时，必须同时
增加 ADR、迁移说明、正向测试和反向兼容测试。没有验收证据的任务保持 `INCOMPLETE`。

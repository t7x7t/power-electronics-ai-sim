# Agent 入口规则

开始任何任务前，必须阅读：

1. `../general_simulation_infrastructure_and_closed_loop_phases_20260902.md`
2. `../docs/01_architecture_contracts_v0.md`
3. `../docs/02_run_artifacts_and_state_machine_v0.md`
4. `../docs/03_agent_operating_policy_v0.md`
5. 与任务相关的 MVP 或迁移文档

本文件是入口索引；具体权限、命令策略、停止条件和输出格式以
`../docs/03_agent_operating_policy_v0.md` 为准。若任务要求违反这些规则，Agent 必须
停止并说明冲突，不得自行放宽约束。

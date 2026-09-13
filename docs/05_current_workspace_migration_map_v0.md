# 当前工作区迁移映射

**状态说明（2026-09-13）**：这是候选内容的审查清单，不是迁移许可或当前发布
内容清单。截至当前最小本地发布范围，没有任何 `D:\PySpice` 文件、模型、结果或
文档被迁移。`docs/09_license_and_git_baseline_scope_v0.md` 的来源/许可证限制及
`docs/27_minimum_local_release_scope_v0.md` 的发布排除项优先于本文中的候选映射。
表中“允许迁移”只表示未来可能的审查对象，仍须逐文件获得记录的来源、许可证和
技术批准。

源工作区：`D:\PySpice`。迁移原则是选择性提取，不复制整个混合研究工作区。

## 1. 候选迁移内容（逐文件审查后）

| 源位置 | v0 处理 | 说明 |
|---|---|---|
| `sim/dc_dc/psfb/circuit.py` | 提取参考 Plant 适配层 | 不把 PSFB 参数写进公共契约 |
| `sim/dc_dc/psfb/simulation.py` | 提取 Runner 时序经验 | 先拆出通用时钟/动作边界 |
| `sim/dc_dc/psfb/controller.py` | 提取 PI 参考 Controller | 通过公共 Measurement/Action 接口接入 |
| `sim/dc_dc/psfb/metrics.py` | 提取通用指标的纯函数 | 指标不得决定资格或安全状态 |
| `analysis/run.py` | 作为迁移参考，不直接复制 | 当前入口含 PSFB 专用工厂和默认值 |
| `tests/` | 按契约重新筛选 | unit/integration/validation/slow 分层 |
| `D:\PySpice\docs\ai_assisted_power_electronics_simulation_lifecycle_20260902.md` | 作为源工作区流程参考 | 迁移时提取经验，不直接作为公共项目依赖 |

## 2. 不应进入公共 v0

- `hardware/` 下的 BOM、Gerber、PCB 工程、原理图、Netlist；
- 带个人绝对路径、密钥或内部实验记录的文件；
- 大型 `analysis/results/`、`tmp/`、`.pytest_*` 和缓存目录；
- 尚未审查许可证的论文原文、器件模型和第三方资料；
- 只服务某个 CVB/ILC 研究问题的学习库和一次性脚本。

## 3. 迁移检查表

- [ ] 为每个迁移文件标记来源 commit 和许可证；
- [ ] 删除绝对路径和隐式全局配置；
- [ ] 将 PSFB 专用字段隔离在参考适配器；
- [ ] 为迁移行为补充契约测试，而不是只复制旧测试；
- [ ] 用 v0 Runner 重新生成参考 Manifest；
- [ ] 对比旧结果时同时记录模型、初态、时间网格和合同 Hash；
- [ ] 旧结果只作为历史证据，不自动成为新基线。

## 4. 迁移完成条件

当 v0 示例能够独立安装、运行、验证和生成结果后，才迁移物理模型。每增加一层模型
都必须有独立的能力声明、合同、smoke、失败测试和证据限制。

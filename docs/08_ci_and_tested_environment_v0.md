# 持续集成与已验证测试环境（v0）

**Merged from:** the two former `docs/08` records — the v0 CI description and
the v0 verified/tested CI environment record — merged 2026-10-25. Content is
preserved; only duplication was removed.

**Status:** v0 record. Neither source file declared a separate status line;
both are v0 descriptions of the same CI surface (`.github/workflows/ci.yml`)
and its tested dependency combination. The role is unchanged: description of
the current CI behaviour and the tested environment, not a release claim.

## 1. 为什么需要 CI

本项目的目标是让用户和 AI Agent 长期扩展、复用仿真基础设施。每次修改都可能同时影响公共契约、Runner、Schema、产物格式和命令行入口。CI（持续集成）为这些可机械判断的要求提供重复、统一的自动验收，避免结果只在某一台开发机上通过。

CI 不能判断模型是否足够接近真实电路、参数是否合理，也不能把 Fake backend 的结果升级为硬件结论。这些仍属于人工边界审查。CI 的作用是确保“代码按声明运行，并且没有破坏已经约定的基础行为”。

## 2. 当前工作流

`.github/workflows/ci.yml` 在 push、pull request 和手动触发上运行，包含三个 job：

| Job | 矩阵 | 作用 |
| --- | --- | --- |
| `contract` | Ubuntu + Windows × Python 3.10/3.11/3.12 | 使用 `requirements-tested.txt` 的精确 pin 集，执行 Schema 校验、完整测试和 CLI smoke |
| `next-python` | Ubuntu × Python 3.13 | 使用 `[test]` 范围依赖，验证已声明 Python 上界的兼容性；不是发布工具链 |
| `package` | Ubuntu × Python 3.12 | 构建 sdist/wheel，`twine check`，在干净 venv 安装 wheel，运行 `demo`/`inspect-run`/`capabilities` |

Windows 加入 `contract` 矩阵的原因：正式发布门禁在 Windows 上运行，CI 不能只测试 Linux。

Python 3.13 job 故意不使用 `requirements-tested.txt`：已验证 pin 集保留
`numpy==1.26.4`，而该版本没有 Python 3.13 wheel。该 job 记录 `pip freeze`，其结果是
**兼容性证据**，不是 exact release toolchain 证据。

工作流只使用内置的 Fake/平均 Buck 测试夹具，因此它验证的是公共契约、Runner 生命周期、产物和 fail-closed 行为，不是 PySpice/Ngspice 的物理正确性。真实后端和正式 demo 仍由 `scripts/release_check.ps1` 在显式环境中执行，不进入日常 CI。

## 3. 已验证的 CI 环境（Verified CI Environment）

The v0 test suite was verified locally with this recommended environment:

```text
Python 3.12.7
pytest 7.4.4
jsonschema 4.23.0
numpy 1.26.4
```

`requirements-tested.txt` records the package pins. These pins are a tested
reference combination, not a claim that every platform is equivalent. The
runtime package remains dependency-light; PySpice and Ngspice require a
separate environment review and lock before migration.

The lightweight GitHub Actions matrix uses these pins on Python 3.10, 3.11,
and 3.12 on both Ubuntu and Windows; a separate Python 3.13 job uses the
range-based `[test]` dependencies and is labelled compatibility evidence. This
prevents a resolver's newest unverified dependency release from being
misrepresented as a failure of the documented CI surface. The formal local
release gate remains the only exact release-toolchain record.

Python 3.10 is supported through the small conditional `tomli` dependency,
which supplies the standard-library `tomllib` interface used by environment
metadata parsing on that interpreter version.

The browser workbench is temporarily withdrawn, so CI and the release gate no
longer require a Node/npm toolchain. The distinction between admitted Python
ranges and the release-gate toolchain is defined in
[`docs/28_release_toolchain_policy_v0.md`](28_release_toolchain_policy_v0.md).

## 4. 本地复现

在项目根目录执行：

```powershell
python -m pip install -e ".[test]"
python -m pytest --basetemp .pytest_ci
python -m pe_sim.cli --help
python -m pe_sim.cli run examples/buck_pi_step/config.json --backend buck
```

该 smoke run 使用理想平均 Buck 测试夹具，输出目录由配置中的 `output.directory` 决定。它不代表参考电路或物理结果。

## 5. 通过与不通过

- 任意安装、Schema、测试、wheel 安装或 CLI 步骤失败，CI 检查失败；变更不应进入已保护的正式分支。
- CI 通过只说明自动化检查通过，不等同于实验合格、模型有效或硬件就绪。
- CI 不是发布门禁：真实 ngspice/PySpice 验收和正式 demo 证据仍由 `scripts/release_check.ps1` 与 `scripts/user_acceptance.py` 记录。
- 真实模型迁移后，应增加独立的 PySpice/Ngspice job；该 job 的依赖、版本和许可证应在引入前单独审查。

## 6. 与版本和证据的关系

CI 结果应与触发它的 Git commit 关联保存。运行 Manifest 记录源码 provenance，便于将实验结果与经过测试的代码版本对应起来。dirty 工作区、未知来源或人工审查未通过时，不能仅凭 CI 绿色状态把结果标记为正式基线或学习输入。

## 7. 依赖策略

运行时依赖保持为空，以便用户只安装基础 Runner。测试工具通过 `pip install -e ".[test]"` 安装：`pytest`、`jsonschema` 和 `numpy`。依赖版本目前采用下限而非锁文件；跨机器完全复现仍需要后续补充环境锁定和 provenance 记录。

"""Release-identity consistency for the package version and license.

The checks read files from the repository and one recorded CLI invocation.
They are independent of Git state and of the network: no tag, remote, or
index is consulted, and no build is performed. They do not prove that a
release was approved, tagged, or reproducible; they only prove that the
in-code version, the build metadata, the CLI output, the release note name,
and the license declaration agree.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pe_sim
from pe_sim.public_api import EXPERIMENTAL_API, STABLE_API

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised on Python 3.10 only
    import tomli as tomllib

REPO_ROOT = Path(__file__).resolve().parents[1]
PYPROJECT_PATH = REPO_ROOT / "pyproject.toml"
CLI_PATH = REPO_ROOT / "src" / "pe_sim" / "cli.py"
LICENSE_PATH = REPO_ROOT / "LICENSE"

_VERSION_PATTERN = re.compile(r"^\d+\.\d+\.\d+$")
_HARD_CODED_VERSION_PATTERN = re.compile(r"""version\s*=\s*["']\d+\.\d+\.\d+["']""")


def _pyproject() -> dict:
    return tomllib.loads(PYPROJECT_PATH.read_text(encoding="utf-8"))


def test_package_version_matches_pyproject_version():
    declared = _pyproject()["project"]["version"]
    assert pe_sim.__version__ == declared, (
        "pe_sim.__version__ 与 pyproject.toml 的 project.version 不一致："
        f"{pe_sim.__version__!r} != {declared!r}"
    )


def test_package_version_is_a_semver_triplet():
    assert _VERSION_PATTERN.match(pe_sim.__version__), (
        f"版本号必须是 X.Y.Z 形式，实际为 {pe_sim.__version__!r}"
    )


def test_cli_reports_the_package_version():
    completed = subprocess.run(
        [sys.executable, "-m", "pe_sim.cli", "--version"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, f"--version 退出码非零：{completed.stderr!r}"
    text = completed.stdout.strip()
    assert text.endswith(pe_sim.__version__), (
        f"--version 输出未包含包版本 {pe_sim.__version__!r}：{completed.stdout!r}"
    )
    prefix = text[: -len(pe_sim.__version__)]
    assert prefix in {"", "pe-sim "}, f"--version 输出格式意外：{completed.stdout!r}"
    assert completed.stdout.count("\n") == 1 and text, (
        f"--version 必须只输出一行：{completed.stdout!r}"
    )


def test_cli_source_uses_the_single_version_source():
    source = CLI_PATH.read_text(encoding="utf-8")

    assert "__version__" in source, "cli.py 必须显式导入并使用 __version__"
    assert f'version="{pe_sim.__version__}"' not in source, (
        "cli.py 不得硬编码当前版本号"
    )
    assert _HARD_CODED_VERSION_PATTERN.search(source) is None, (
        "cli.py 中不得出现任何硬编码的 X.Y.Z 版本字面量"
    )


def test_release_note_exists_and_names_the_version():
    note = REPO_ROOT / "docs" / "releases" / f"v{pe_sim.__version__}.md"

    assert note.is_file(), f"缺少发布说明 {note}"
    assert pe_sim.__version__ in note.read_text(encoding="utf-8"), (
        f"发布说明 {note.name} 未提及版本 {pe_sim.__version__}"
    )


def test_version_is_not_part_of_the_public_surface():
    assert "__version__" not in pe_sim.__all__, "__version__ 不得进入 __all__"
    assert "__version__" not in STABLE_API, "__version__ 不得进入 STABLE_API"
    assert "__version__" not in EXPERIMENTAL_API, "__version__ 不得进入 EXPERIMENTAL_API"


def test_pyproject_declares_mit_license_with_license_file():
    project = _pyproject()["project"]

    assert project["license"] == "MIT", f"license 声明应为 MIT：{project.get('license')!r}"
    assert "LICENSE" in project["license-files"], (
        f"license-files 必须包含 LICENSE：{project.get('license-files')!r}"
    )
    first_line = LICENSE_PATH.read_text(encoding="utf-8").lstrip().splitlines()[0]
    assert first_line.startswith("MIT License"), (
        f"LICENSE 首行应为 MIT License：{first_line!r}"
    )

def test_no_test_file_pins_the_current_version_as_a_literal():
    """A test that asserts a version literal goes stale on the next bump.

    The version has one source; tests must compare against ``pe_sim.__version__``
    (or a synthetic fixture version that is deliberately different).
    """

    version = pe_sim.__version__
    offending = []
    for path in sorted(Path(__file__).resolve().parent.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        if f'== "{version}"' in text or f"== '{version}'" in text:
            offending.append(path.name)
    assert not offending, (
        "compare against pe_sim.__version__ instead of pinning "
        f"{version!r}: {offending}"
    )

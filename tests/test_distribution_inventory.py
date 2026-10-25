"""The distribution inventory contract (`docs/53_release_governance_v1.md`).

These tests build real wheel-shaped archives inside ``tmp_path`` and load the
script by path, because ``scripts/`` is not an importable package.  Nothing here
reads the developer's ``dist/`` directory, the network, or the project package,
so the suite is independent of the state of the worktree.

The inventory is a *record*, not a verification: most assertions below pin the
places where the format must refuse to imply more than it checked (dependency
licenses, license equivalence, reproducibility, machine paths).
"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import re
import sys
import zipfile
from pathlib import Path

import pytest

REPOSITORY = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPOSITORY / "scripts" / "distribution_inventory.py"
DIST_INFO = "power_electronics_ai_sim-0.7.0.dist-info"
WHEEL_NAME = "power_electronics_ai_sim-0.7.0-py3-none-any.whl"
WINDOW_ABSOLUTE = re.compile(r"^[A-Za-z]:[\\/]")


def _load_script():
    spec = importlib.util.spec_from_file_location(
        "pe_sim_scripts_distribution_inventory", SCRIPT_PATH
    )
    assert spec is not None, SCRIPT_PATH
    assert spec.loader is not None, SCRIPT_PATH
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


inventory = _load_script()


# ---------------------------------------------------------------------------
# fixture wheels
# ---------------------------------------------------------------------------


def _metadata_text(
    *,
    name: str = "power-electronics-ai-sim",
    version: str = "0.7.0",
    requires_python: str | None = ">=3.10",
    license_expression: str | None = "MIT",
    license_header: str | None = None,
    requires_dist: tuple[str, ...] = (
        "jsonschema>=4.20",
        'tomli>=2.0.1; python_version < "3.11"',
    ),
) -> str:
    lines = ["Metadata-Version: 2.4", f"Name: {name}", f"Version: {version}"]
    if requires_python is not None:
        lines.append(f"Requires-Python: {requires_python}")
    if license_expression is not None:
        lines.append(f"License-Expression: {license_expression}")
    if license_header is not None:
        lines.append(f"License: {license_header}")
    lines.extend(f"Requires-Dist: {value}" for value in requires_dist)
    return "\n".join(lines) + "\n\nLong description of the fixture wheel.\n"


def _write_wheel(
    path: Path,
    *,
    metadata: str | None = None,
    members: dict[str, bytes] | None = None,
    with_metadata: bool = True,
    with_record: bool = True,
    with_directory_entry: bool = True,
) -> Path:
    payload = _metadata_text() if metadata is None else metadata
    files: list[tuple[str, bytes]] = [
        ("pe_sim/__init__.py", b'__version__ = "0.7.0"\n'),
        ("pe_sim/cli.py", b"def main(argv=None):\n    return 0\n"),
        (
            f"{DIST_INFO}/WHEEL",
            b"Wheel-Version: 1.0\nGenerator: fixture\nRoot-Is-Purelib: true\n"
            b"Tag: py3-none-any\n",
        ),
    ]
    if with_metadata:
        files.append((f"{DIST_INFO}/METADATA", payload.encode("utf-8")))
    if with_record:
        files.append((f"{DIST_INFO}/RECORD", b"pe_sim/__init__.py,,\n"))
    files.extend((members or {}).items())
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as archive:
        if with_directory_entry:
            archive.writestr(f"{DIST_INFO}/", b"")
        for name, data in files:
            archive.writestr(name, data)
    return path


@pytest.fixture()
def wheel(tmp_path: Path) -> Path:
    return _write_wheel(tmp_path / WHEEL_NAME)


def _strings(value):
    if isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)
    elif isinstance(value, str):
        yield value


# ---------------------------------------------------------------------------
# shape and hashes
# ---------------------------------------------------------------------------


def test_inventory_declares_the_schema_rule_version_and_top_level_keys(wheel: Path):
    data = inventory.build_inventory(wheel)

    assert data["schema_version"] == "pe-sim.distribution-inventory.v1"
    assert data["rule_version"] == "inventory-v1"
    assert set(data) == {
        "schema_version",
        "rule_version",
        "generated_at_utc",
        "distribution",
        "contents",
        "contents_digest",
        "dependencies",
        "licenses",
        "limitations",
    }
    distribution = data["distribution"]
    assert distribution["name"] == "power-electronics-ai-sim"
    assert distribution["version"] == "0.7.0"
    assert distribution["requires_python"] == ">=3.10"
    assert set(distribution) == {
        "name",
        "version",
        "requires_python",
        "metadata_license",
        "artifact",
    }


def test_artifact_entry_hashes_the_wheel_file_itself(wheel: Path):
    data = inventory.build_inventory(wheel)
    raw = wheel.read_bytes()
    artifact = data["distribution"]["artifact"]

    assert artifact["filename"] == WHEEL_NAME
    assert artifact["sha256"] == hashlib.sha256(raw).hexdigest()
    assert artifact["size_bytes"] == len(raw)


def test_contents_are_sorted_and_hash_every_member_except_record_and_directories(
    wheel: Path,
):
    data = inventory.build_inventory(wheel)
    with zipfile.ZipFile(wheel) as archive:
        expected = {
            info.filename: archive.read(info)
            for info in archive.infolist()
            if not info.is_dir() and not info.filename.endswith(".dist-info/RECORD")
        }

    paths = [entry["path"] for entry in data["contents"]]
    assert paths == sorted(paths)
    assert set(paths) == set(expected)
    assert f"{DIST_INFO}/RECORD" not in paths
    assert f"{DIST_INFO}/" not in paths
    for entry in data["contents"]:
        member = expected[entry["path"]]
        assert entry["sha256"] == hashlib.sha256(member).hexdigest()
        assert entry["size_bytes"] == len(member)


def test_contents_digest_recomputes_from_the_recorded_contents(wheel: Path):
    data = inventory.build_inventory(wheel)
    canonical = json.dumps(
        data["contents"], ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")

    assert data["contents_digest"] == hashlib.sha256(canonical).hexdigest()


def test_two_builds_of_the_same_wheel_are_identical_and_untimestamped(wheel: Path):
    first = inventory.build_inventory(wheel)
    second = inventory.build_inventory(wheel)

    assert first == second
    assert first["generated_at_utc"] is None


# ---------------------------------------------------------------------------
# timestamps
# ---------------------------------------------------------------------------


def test_timestamp_sets_only_generated_at_utc(wheel: Path, tmp_path: Path):
    plain = tmp_path / "plain.json"
    stamped = tmp_path / "stamped.json"

    assert inventory.main([str(wheel), "-o", str(plain)]) == 0
    assert (
        inventory.main(
            [str(wheel), "-o", str(stamped), "--timestamp", "2026-10-25T00:00:00Z"]
        )
        == 0
    )

    plain_data = json.loads(plain.read_text(encoding="utf-8"))
    stamped_data = json.loads(stamped.read_text(encoding="utf-8"))
    assert stamped_data["generated_at_utc"] == "2026-10-25T00:00:00Z"
    plain_data.pop("generated_at_utc")
    stamped_data.pop("generated_at_utc")
    assert plain_data == stamped_data


def test_non_iso_timestamp_is_invalid_input(wheel: Path, tmp_path: Path, capsys):
    output = tmp_path / "out.json"

    assert inventory.main([str(wheel), "-o", str(output), "--timestamp", "yesterday"]) == 2
    assert not output.exists()
    assert "ISO 8601" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# declared dependencies
# ---------------------------------------------------------------------------


def test_requires_dist_parsing_covers_runtime_markers_and_extras():
    parsed = inventory.parse_requires_dist(
        [
            "jsonschema>=4.20",
            "tomli>=2.0.1; python_version < '3.11'",
            'numpy>=1.24; extra == "test"',
            "pytest>=7.4; extra == 'test' and python_version >= \"3.8\"",
            "requests",
            "flask (>=2.0)",
        ]
    )

    assert parsed == [
        {"name": "flask", "specifier": ">=2.0", "marker": None, "extra": None},
        {"name": "jsonschema", "specifier": ">=4.20", "marker": None, "extra": None},
        {"name": "numpy", "specifier": ">=1.24", "marker": None, "extra": "test"},
        {
            "name": "pytest",
            "specifier": ">=7.4",
            "marker": 'python_version >= "3.8"',
            "extra": "test",
        },
        {"name": "requests", "specifier": None, "marker": None, "extra": None},
        {
            "name": "tomli",
            "specifier": ">=2.0.1",
            "marker": 'python_version < "3.11"',
            "extra": None,
        },
    ]


def test_dependencies_are_grouped_into_runtime_and_extras(tmp_path: Path):
    wheel = _write_wheel(
        tmp_path / WHEEL_NAME,
        metadata=_metadata_text(
            requires_dist=(
                "jsonschema>=4.20",
                'tomli>=2.0.1; python_version < "3.11"',
                'numpy>=1.24; extra == "test"',
                'pytest>=7.4; extra == "test" and python_version >= "3.8"',
            )
        ),
    )
    data = inventory.build_inventory(wheel)

    assert [entry["name"] for entry in data["dependencies"]["runtime"]] == [
        "jsonschema",
        "tomli",
    ]
    assert data["dependencies"]["extras"] == {
        "test": [
            {"name": "numpy", "specifier": ">=1.24", "marker": None, "extra": "test"},
            {
                "name": "pytest",
                "specifier": ">=7.4",
                "marker": 'python_version >= "3.8"',
                "extra": "test",
            },
        ]
    }


def test_dependency_licenses_are_recorded_as_not_verified(wheel: Path):
    entries = inventory.build_inventory(wheel)["licenses"]["dependencies"]

    assert [entry["name"] for entry in entries] == ["jsonschema", "tomli"]
    for entry in entries:
        assert entry["declared"] is None
        assert entry["source"] == "not_recorded"
        assert entry["status"] == "not_verified"


# ---------------------------------------------------------------------------
# declared license
# ---------------------------------------------------------------------------


def test_license_file_hash_and_path_are_recorded(tmp_path: Path, wheel: Path):
    license_file = tmp_path / "licenses" / "LICENSE.txt"
    license_file.parent.mkdir()
    license_file.write_text("MIT License\n", encoding="utf-8")

    project = inventory.build_inventory(wheel, license_file=license_file)["licenses"][
        "project"
    ]

    assert project["license_file"] == {
        "path": "LICENSE.txt",
        "sha256": hashlib.sha256(license_file.read_bytes()).hexdigest(),
    }
    assert project["declared"] == "MIT"
    assert project["declared_source"] == "artifact_metadata"


def test_missing_license_file_is_recorded_as_null_with_a_limitation(wheel: Path):
    data = inventory.build_inventory(wheel)
    project = data["licenses"]["project"]

    assert project["license_file"] == {"path": None, "sha256": None}
    assert any("no license file" in item for item in data["limitations"])


def test_absent_metadata_license_is_not_recorded(tmp_path: Path):
    wheel = _write_wheel(
        tmp_path / WHEEL_NAME, metadata=_metadata_text(license_expression=None)
    )
    data = inventory.build_inventory(wheel)

    assert data["distribution"]["metadata_license"] is None
    assert data["licenses"]["project"]["declared"] is None
    assert data["licenses"]["project"]["declared_source"] == "not_recorded"
    assert any("declares no license string" in item for item in data["limitations"])


def test_plain_license_header_is_the_fallback_declaration(tmp_path: Path):
    wheel = _write_wheel(
        tmp_path / WHEEL_NAME,
        metadata=_metadata_text(license_expression=None, license_header="MIT"),
    )
    data = inventory.build_inventory(wheel)

    assert data["distribution"]["metadata_license"] == "MIT"
    assert data["licenses"]["project"]["declared"] == "MIT"
    assert data["licenses"]["project"]["declared_source"] == "artifact_metadata"


def test_limitations_always_state_the_core_non_claims(wheel: Path):
    limitations = inventory.build_inventory(wheel)["limitations"]
    text = " ".join(limitations)

    assert limitations
    assert "does not resolve, download, or verify dependencies" in text
    assert "not claimed to be byte-identical" in text
    assert "identifies one wheel file, not a source commit" in text


# ---------------------------------------------------------------------------
# output format
# ---------------------------------------------------------------------------


def test_written_file_is_canonical_utf8_without_bom(wheel: Path, tmp_path: Path):
    output = tmp_path / "inventory.json"

    returned = inventory.write_inventory(inventory.build_inventory(wheel), output)
    raw = output.read_bytes()

    assert returned == output
    assert not raw.startswith(b"\xef\xbb\xbf")
    assert raw.endswith(b"\n")
    canonical = (
        json.dumps(
            json.loads(raw.decode("utf-8")),
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
        + "\n"
    ).encode("utf-8")
    assert raw == canonical


def test_output_defaults_to_the_wheel_directory(wheel: Path):
    assert inventory.main([str(wheel)]) == 0
    written = wheel.parent / "distribution-inventory.json"

    assert written.is_file()
    data = json.loads(written.read_text(encoding="utf-8"))
    assert data["distribution"]["artifact"]["filename"] == WHEEL_NAME


def test_dash_output_writes_the_inventory_to_stdout(wheel: Path, monkeypatch):
    captured = io.BytesIO()

    class _Stdout:
        buffer = captured

        def write(self, text):  # pragma: no cover - must not be used
            raise AssertionError("the text stdout path was used instead of the buffer")

    monkeypatch.setattr(sys, "stdout", _Stdout())

    assert inventory.main([str(wheel), "-o", "-"]) == 0
    payload = captured.getvalue()
    assert payload.endswith(b"\n")
    data = json.loads(payload.decode("utf-8"))
    assert data["distribution"]["artifact"]["filename"] == WHEEL_NAME


def test_no_emitted_value_is_an_absolute_or_rooted_path(wheel: Path, tmp_path: Path):
    output = tmp_path / "inventory.json"
    license_file = tmp_path / "LICENSE"
    license_file.write_text("MIT\n", encoding="utf-8")

    inventory.write_inventory(
        inventory.build_inventory(wheel, license_file=license_file), output
    )

    for value in _strings(json.loads(output.read_text(encoding="utf-8"))):
        assert not WINDOW_ABSOLUTE.match(value), value
        assert not value.startswith("/"), value
        assert not value.startswith("\\\\"), value
        assert str(tmp_path) not in value, value


# ---------------------------------------------------------------------------
# --check and invalid input
# ---------------------------------------------------------------------------


def test_check_passes_when_unchanged_and_fails_after_the_wheel_changes(
    wheel: Path, tmp_path: Path
):
    output = tmp_path / "distribution-inventory.json"

    assert inventory.main([str(wheel), "-o", str(output)]) == 0
    assert inventory.main([str(wheel), "-o", str(output), "--check"]) == 0

    _write_wheel(wheel, members={"pe_sim/extra.py": b"EXTRA = 1\n"})
    assert inventory.main([str(wheel), "-o", str(output), "--check"]) == 1


def test_check_without_an_existing_file_is_a_mismatch(wheel: Path, tmp_path: Path):
    output = tmp_path / "absent.json"

    assert inventory.main([str(wheel), "-o", str(output), "--check"]) == 1
    assert not output.exists()


def test_invalid_input_exits_two_and_writes_nothing(tmp_path: Path, capsys):
    output = tmp_path / "distribution-inventory.json"

    missing = tmp_path / "missing.whl"
    assert inventory.main([str(missing), "-o", str(output)]) == 2
    assert not output.exists()
    assert "error" in capsys.readouterr().err

    not_a_zip = tmp_path / "not-a-wheel.whl"
    not_a_zip.write_text("this is not a zip archive", encoding="utf-8")
    assert inventory.main([str(not_a_zip), "-o", str(output)]) == 2
    assert not output.exists()
    assert "error" in capsys.readouterr().err

    no_metadata = _write_wheel(tmp_path / WHEEL_NAME, with_metadata=False)
    assert inventory.main([str(no_metadata), "-o", str(output)]) == 2
    assert not output.exists()
    assert "METADATA" in capsys.readouterr().err

    bad_encoding = _write_wheel(
        tmp_path / "bad-encoding-0.7.0-py3-none-any.whl",
        with_metadata=False,
        members={f"{DIST_INFO}/METADATA": b"Name: x\nVersion: \xff\xfe\n"},
    )
    assert inventory.main([str(bad_encoding), "-o", str(output)]) == 2
    assert not output.exists()
    assert "UTF-8" in capsys.readouterr().err


def test_check_needs_a_file_output(wheel: Path, capsys):
    assert inventory.main([str(wheel), "-o", "-", "--check"]) == 2
    assert "error" in capsys.readouterr().err

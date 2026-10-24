"""T4: reading an older release's package and stating comparability honestly.

The fixture under ``tests/fixtures/older_packages/v0.5.9-buck`` is a real
package produced by the v0.5.9 release, not a hand-written manifest.  These
tests cover the *reader* side: the package is readable, discoverable, and the
comparison report refuses to imply cross-release equality.

The installed-wheel half of T4 is `scripts/upgrade_check.ps1`: it builds and
installs the current checkout and repeats these reads with the installed
console script.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from pe_sim.catalog import scan_runs
from pe_sim.inspection import inspect_run
from pe_sim.reproducibility import compare_runs

_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "older_packages" / "v0.5.9-buck"
_FIXTURE_COMMIT = "ed4552131170b46eeb5eabd857f8ccfde5cbcdcb"


def _manifest(directory: Path) -> dict:
    return json.loads((directory / "manifest.json").read_text(encoding="utf-8"))


def _copy_with(tmp_path: Path, name: str, **overrides) -> Path:
    target = tmp_path / name
    shutil.copytree(_FIXTURE, target)
    document = _manifest(target)
    document.update(overrides)
    (target / "manifest.json").write_text(
        json.dumps(document, indent=2) + "\n", encoding="utf-8"
    )
    return target


def test_the_fixture_is_a_real_package_from_the_previous_release():
    manifest = _manifest(_FIXTURE)

    assert manifest["schema_version"] == "0.1"
    assert manifest["source_commit"] == _FIXTURE_COMMIT
    assert manifest["status"] == "QUALIFIED"
    assert manifest["hashes"]["package_sha256"]
    assert (manifest["plant"] or {}).get("id") == "buck"


def test_the_older_package_is_readable_and_discoverable():
    inspection = inspect_run(_FIXTURE)

    assert inspection.status == "ok"
    compatibility = inspection.summary["compatibility"]
    assert compatibility["status"] == "current"
    assert compatibility["may_rewrite"] is False
    assert compatibility["package_schema"] == "0.1"

    catalog = scan_runs(_FIXTURE.parent)
    assert [entry.run_id for entry in catalog.runs] == ["buck-reference"]


def test_an_identical_package_keeps_the_historical_comparison_payload():
    payload = compare_runs(_FIXTURE, _FIXTURE).to_dict()

    assert payload["outcome"] == "exact_match"
    # No compatibility block means "same schema version and same source
    # commit"; existing clients see the payload they already knew.
    assert "compatibility" not in payload


def test_a_cross_release_comparison_is_not_assessed(tmp_path):
    other_commit = _copy_with(tmp_path, "other-release", source_commit="0" * 40)

    note = compare_runs(_FIXTURE, other_commit).to_dict()["compatibility"]

    assert note["cross_version_comparison"] == "not_assessed"
    assert note["left"]["status"] == "current"
    assert note["right"]["status"] == "current"
    assert any("different source commits" in reason for reason in note["reasons"])
    assert any("not comparable across releases" in item for item in note["limitations"])


def test_a_newer_schema_is_not_comparable_at_all(tmp_path):
    future = _copy_with(tmp_path, "future-schema", schema_version="9.9")

    payload = compare_runs(_FIXTURE, future).to_dict()
    note = payload["compatibility"]

    assert note["cross_version_comparison"] == "unsupported"
    assert note["right"]["status"] == "incompatible"
    assert payload["outcome"] == "unknown"

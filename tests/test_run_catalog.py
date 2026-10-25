"""Read-only run catalog (roadmap Phase B1).

The catalog must answer "what runs exist" cheaply and without ever becoming a
second definition of "valid package".  These tests pin both properties: the
metadata it reports, and the fact that it touches nothing.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pe_sim.catalog import CATALOG_SCHEMA, CatalogError, scan_runs
from pe_sim.cli import main
from pe_sim.run_store import RUNS_ROOT_ENV


def _run(
    root: Path,
    name: str,
    *,
    created_at: str = "2026-01-01T00:00:00Z",
    status: str = "QUALIFIED",
    run_state: str = "PUBLISHED",
    plant_id: str = "buck",
    plant_class: str = "BuckPlant",
    trust_level: str | None = "reference",
    evidence_level: str = "functional",
    qualification_level: str = "limited",
) -> Path:
    directory = root / name
    directory.mkdir(parents=True)
    plant = {"id": plant_id, "hash": "a" * 64, "identity": {"class": plant_class}}
    if trust_level is not None:
        plant["trust_level"] = trust_level
    manifest = {
        "schema_version": "0.1",
        "experiment_id": "exp",
        "run_id": name,
        "created_at": created_at,
        "status": status,
        "evidence_level": evidence_level,
        "plant": plant,
        "qualification": {"level": qualification_level, "passed": True},
        "manifest_sha256": "b" * 64,
    }
    (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (directory / ".run-state.json").write_text(json.dumps({"status": run_state}), encoding="utf-8")
    return directory


def test_reports_recorded_metadata_for_each_run(tmp_path):
    _run(tmp_path, "alpha")

    catalog = scan_runs(tmp_path)

    assert catalog.schema_version == CATALOG_SCHEMA
    assert len(catalog.runs) == 1
    entry = catalog.runs[0]
    assert entry.run_id == "alpha"
    assert entry.path == "alpha"
    assert entry.status == "QUALIFIED"
    assert entry.run_state == "PUBLISHED"
    assert entry.published is True
    assert entry.plant_id == "buck"
    assert entry.plant_class == "BuckPlant"
    assert entry.trust_level == "reference"
    assert entry.evidence_level == "functional"
    assert entry.qualification_level == "partial"
    assert entry.problems == ()


def test_finds_runs_in_nested_directories(tmp_path):
    _run(tmp_path / "group" / "inner", "nested")

    catalog = scan_runs(tmp_path)

    assert [entry.path for entry in catalog.runs] == ["group/inner/nested"]


def test_reads_the_class_from_either_recorded_identity_shape(tmp_path):
    """Fixtures record identity.class; adapters record identity.implementation.class."""

    _run(tmp_path, "flat-shape", plant_class="FakePlant")
    nested = _run(tmp_path, "nested-shape")
    manifest = json.loads((nested / "manifest.json").read_text(encoding="utf-8"))
    manifest["plant"]["identity"] = {"implementation": {"class": "NgspiceBuckPlant"}}
    (nested / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    by_id = {entry.run_id: entry for entry in scan_runs(tmp_path).runs}

    assert by_id["flat-shape"].plant_class == "FakePlant"
    assert by_id["nested-shape"].plant_class == "NgspiceBuckPlant"


def test_orders_newest_first_then_by_run_id(tmp_path):
    _run(tmp_path, "old", created_at="2026-01-01T00:00:00Z")
    _run(tmp_path, "new", created_at="2026-03-01T00:00:00Z")
    _run(tmp_path, "same-a", created_at="2026-02-01T00:00:00Z")
    _run(tmp_path, "same-b", created_at="2026-02-01T00:00:00Z")

    order = [entry.run_id for entry in scan_runs(tmp_path).runs]

    assert order == ["new", "same-b", "same-a", "old"]


def test_filters_are_applied_before_the_limit(tmp_path):
    _run(tmp_path, "a", plant_id="buck")
    _run(tmp_path, "b", plant_id="boost")
    _run(tmp_path, "c", plant_id="boost", status="RUN_OK")

    assert [e.run_id for e in scan_runs(tmp_path, plant_id="boost").runs] == ["c", "b"]
    assert [e.run_id for e in scan_runs(tmp_path, status="RUN_OK").runs] == ["c"]
    assert [e.run_id for e in scan_runs(tmp_path, run_id_prefix="b").runs] == ["b"]
    assert [e.run_id for e in scan_runs(tmp_path, plant_id="boost", limit=1).runs] == ["c"]


def test_unpublished_runs_are_visible_but_filterable(tmp_path):
    _run(tmp_path, "staged", run_state="RUNNING")

    assert [e.run_id for e in scan_runs(tmp_path).runs] == ["staged"]
    assert scan_runs(tmp_path, published_only=True).runs == ()


def test_a_missing_state_marker_is_reported_not_hidden(tmp_path):
    directory = _run(tmp_path, "no-state")
    (directory / ".run-state.json").unlink()

    entry = scan_runs(tmp_path).runs[0]

    assert entry.published is False
    assert "run state marker is missing or unreadable" in entry.problems


def test_an_unreadable_manifest_is_listed_as_skipped(tmp_path):
    directory = tmp_path / "broken"
    directory.mkdir()
    (directory / "manifest.json").write_text("{not json", encoding="utf-8")

    catalog = scan_runs(tmp_path)

    assert catalog.runs == ()
    assert catalog.skipped[0]["path"] == "broken"
    assert "unreadable" in catalog.skipped[0]["reason"]


def test_temporary_staging_directories_are_pruned(tmp_path):
    _run(tmp_path, ".run-id.tmp")

    catalog = scan_runs(tmp_path)

    assert catalog.runs == ()
    assert catalog.skipped == ()


def test_the_catalog_never_writes(tmp_path):
    _run(tmp_path, "alpha")
    before = {
        path: (path.stat().st_mtime_ns, path.stat().st_size)
        for path in tmp_path.rglob("*")
        if path.is_file()
    }

    scan_runs(tmp_path)

    after = {
        path: (path.stat().st_mtime_ns, path.stat().st_size)
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    assert after == before


def test_the_catalog_reports_that_it_does_not_verify(tmp_path):
    _run(tmp_path, "alpha")

    catalog = scan_runs(tmp_path)

    assert any("inspect_run" in item for item in catalog.limitations)
    assert any("never writes" in item for item in catalog.limitations)


def test_a_missing_root_is_an_explicit_error(tmp_path):
    with pytest.raises(CatalogError, match="does not exist"):
        scan_runs(tmp_path / "absent")


def test_invalid_limit_is_rejected(tmp_path):
    with pytest.raises(CatalogError, match="limit"):
        scan_runs(tmp_path, limit=-1)


def test_cli_list_runs_prints_the_catalog(tmp_path, capsys, monkeypatch):
    _run(tmp_path, "alpha")
    monkeypatch.chdir(tmp_path)

    assert main(["list-runs", str(tmp_path)]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["schema_version"] == CATALOG_SCHEMA
    assert payload["count"] == 1
    assert payload["runs"][0]["run_id"] == "alpha"
    assert payload["limitations"]


def test_cli_list_runs_rejects_a_missing_root(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)

    assert main(["list-runs", str(tmp_path / "absent")]) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "CONFIG_REJECTED"


def test_cli_list_runs_uses_the_environment_root_without_a_positional(
    tmp_path, capsys, monkeypatch
):
    store = tmp_path / "store"
    _run(store, "alpha")
    workdir = tmp_path / "work"
    workdir.mkdir()
    monkeypatch.chdir(workdir)  # no ./runs here, so the default cannot answer
    monkeypatch.setenv(RUNS_ROOT_ENV, str(store))

    assert main(["list-runs"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["count"] == 1
    assert payload["runs"][0]["run_id"] == "alpha"
    assert Path(payload["root"]).resolve() == store.resolve()

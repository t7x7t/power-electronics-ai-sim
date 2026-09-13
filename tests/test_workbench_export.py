"""Focused checks for the offline published-run to workbench adapter."""

from __future__ import annotations

import json

import pytest

from pe_sim import OutputDataset, TopologyDescriptor, TraceDataset
from pe_sim.artifacts import artifact_index, manifest_digest, package_digest
from pe_sim.workbench_export import WorkbenchExportError, export_run_to_workbench_fixture


def _published_buck_run(root):
    run_dir = root / "buck-demo"
    run_dir.mkdir()
    samples = [
        {"step_index": 0, "time_s": 0.0, "vout": 0.0, "inductor_current": 0.0, "action": 0.2},
        {"step_index": 1, "time_s": 1e-5, "vout": 0.0025, "inductor_current": 0.24, "action": 0.1995},
    ]
    (run_dir / "samples.json").write_text(json.dumps(samples), encoding="utf-8")
    (run_dir / ".run-state.json").write_text(json.dumps({"run_id": "buck-demo", "status": "PUBLISHED"}), encoding="utf-8")
    manifest = {
        "run_id": "buck-demo",
        "status": "QUALIFIED",
        "run_mode": "exploratory",
        "evidence_level": "functional",
        "working_tree_status": "dirty",
        "source_commit": "a" * 40,
        "plant": {
            "id": "buck",
            "identity": {
                "topology": "buck",
                "kind": "ideal_averaged_reference",
                "parameters": {
                    "input_voltage_v": 12.0,
                    "inductance_h": 100e-6,
                    "capacitance_f": 470e-6,
                    "load_resistance_ohm": 10.0,
                    "integration_step_s": 1e-6,
                },
            },
        },
    }
    manifest["artifacts"] = artifact_index(run_dir)
    manifest["manifest_sha256"] = manifest_digest(manifest)
    manifest["package_sha256"] = package_digest(run_dir, manifest)
    manifest["hashes"] = {
        "manifest_sha256": manifest["manifest_sha256"],
        "package_sha256": manifest["package_sha256"],
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
    return run_dir, samples, manifest


def test_verified_buck_run_exports_read_only_contract_fixtures(tmp_path):
    run_dir, samples, manifest = _published_buck_run(tmp_path)
    original = {path.name: path.read_bytes() for path in run_dir.iterdir() if path.is_file()}

    exported = export_run_to_workbench_fixture(run_dir, tmp_path / "derived")

    topology = TopologyDescriptor.from_dict(json.loads(exported.files["topology.json"].read_text(encoding="utf-8")))
    trace = TraceDataset.from_dict(json.loads(exported.files["trace.json"].read_text(encoding="utf-8")))
    outputs = OutputDataset.from_dict(json.loads(exported.files["outputs.json"].read_text(encoding="utf-8")))
    assert topology.topology_id == "buck"
    assert trace.provenance.run_id == "buck-demo"
    assert trace.provenance.manifest_sha256 == manifest["manifest_sha256"]
    assert trace.provenance.package_sha256 == manifest["package_sha256"]
    assert trace.provenance.metadata["integrity"] == "verified"
    assert [sample.values["plant.vout"] for sample in trace.samples] == [row["vout"] for row in samples]
    assert outputs.get("vout.final").payload == samples[-1]["vout"]
    assert outputs.get("inductor_current.response").payload["values"][-1] == samples[-1]["inductor_current"]
    assert {path.name: path.read_bytes() for path in run_dir.iterdir() if path.is_file()} == original


def test_tampered_run_package_is_not_exported(tmp_path):
    run_dir, _, _ = _published_buck_run(tmp_path)
    (run_dir / "samples.json").write_text("[]", encoding="utf-8")

    with pytest.raises(WorkbenchExportError, match="artifact index|package_sha256"):
        export_run_to_workbench_fixture(run_dir, tmp_path / "derived")

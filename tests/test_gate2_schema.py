import json
from pathlib import Path
import pytest
import jsonschema

from pe_sim.artifacts import ArtifactWriter
from pe_sim.contracts import ExperimentSpec, Timebase
from pe_sim.runtime import FakePIController, FakePlant, Runner


def test_experiment_retention_matches_schema(tmp_path):
    spec = ExperimentSpec("x", "ret", "p", "c", Timebase(duration_s=0.001), output_dir=str(tmp_path), result_retention="on_failure")
    assert spec.to_dict()["output"]["retention"] == "on_failure"


def test_manifest_is_not_hardware_readiness_and_has_provenance(tmp_path):
    spec = ExperimentSpec("x", "prov", "p", "c", Timebase(duration_s=0.001), output_dir=str(tmp_path))
    result = Runner().run(spec, FakePlant(), FakePIController())
    manifest = json.loads((result.run_dir / "manifest.json").read_text())
    assert manifest["evidence_level"] != "hardware-readiness"
    assert manifest["provenance"]["status"] in {"known", "unknown", "exploratory"}
    assert "logs/run.json" in manifest["artifacts"]
    schema = json.loads(Path("schemas/manifest.schema.json").read_text())
    jsonschema.validate(manifest, schema)


def test_manifest_schema_rejects_hardware_readiness(tmp_path):
    spec = ExperimentSpec("x", "hw", "p", "c", Timebase(duration_s=0.001), output_dir=str(tmp_path))
    result = Runner().run(spec, FakePlant(), FakePIController())
    manifest = json.loads((result.run_dir / "manifest.json").read_text())
    manifest["evidence_level"] = "hardware-readiness"
    schema = json.loads(Path("schemas/manifest.schema.json").read_text())
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(manifest, schema)


def test_run_id_cannot_be_overwritten(tmp_path):
    writer = ArtifactWriter(tmp_path, "same")
    for name in ArtifactWriter.REQUIRED:
        if name == "logs/.keep":
            continue
        writer.write_json(name, {})
    writer.finalize()
    writer2 = ArtifactWriter(tmp_path, "same")
    for name in ArtifactWriter.REQUIRED:
        if name == "logs/.keep":
            continue
        writer2.write_json(name, {})
    with pytest.raises(FileExistsError):
        writer2.finalize()

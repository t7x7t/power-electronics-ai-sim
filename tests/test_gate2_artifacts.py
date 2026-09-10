import json
from pe_sim.artifacts import canonical_json
from pe_sim.contracts import ExperimentSpec, Timebase
from pe_sim.runtime import FakePIController, FakePlant, Runner


def test_artifacts_have_hash_index_and_manifest_state(tmp_path):
    spec = ExperimentSpec("fake", "run-2", "fake-plant", "fake-controller", Timebase(duration_s=0.001, control_period_s=0.001), output_dir=str(tmp_path))
    result = Runner().run(spec, FakePlant(), FakePIController())
    manifest = json.loads((result.run_dir / "manifest.json").read_text())
    assert manifest["status"] in {"QUALIFIED", "RUN_OK", "RUN_FAILED", "INCOMPLETE"}
    assert manifest["artifacts"]
    assert all(len(item["sha256"]) == 64 for item in manifest["artifacts"].values())
    assert canonical_json(manifest).endswith(b"\n")

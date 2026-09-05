import json

from pe_sim.artifacts import manifest_digest, package_digest
from pe_sim.contracts import ExperimentSpec, Timebase
from pe_sim.runtime import FakePIController, FakePlant, Runner


def _run(tmp_path, run_id, *, gain=1.0, plant=None):
    spec = ExperimentSpec(
        "stage7",
        run_id,
        "plant",
        "controller",
        Timebase(duration_s=0.002, control_period_s=0.001),
        output_dir=str(tmp_path),
    )
    return Runner().run(spec, plant or FakePlant(gain=gain), FakePIController())


def test_manifest_and_package_digests_are_independently_recomputable(tmp_path):
    result = _run(tmp_path, "hashes")
    manifest_path = result.run_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert manifest["manifest_sha256"] == manifest_digest(manifest)
    assert manifest["package_sha256"] == package_digest(result.run_dir, manifest)
    assert manifest["hashes"] == {
        "manifest_sha256": manifest["manifest_sha256"],
        "package_sha256": manifest["package_sha256"],
    }
    assert manifest["worktree_analysis_sha256"]
    assert manifest["provenance"]["worktree_analysis_sha256"] == manifest["worktree_analysis_sha256"]

    tampered = dict(manifest)
    tampered["plant"] = dict(manifest["plant"])
    tampered["plant"]["identity"] = dict(manifest["plant"]["identity"])
    tampered["plant"]["identity"]["configuration"] = dict(manifest["plant"]["identity"]["configuration"])
    tampered["plant"]["identity"]["configuration"]["gain"] = 99.0
    assert manifest_digest(tampered) != manifest["manifest_sha256"]

    (result.run_dir / "samples.json").write_text("tampered", encoding="utf-8")
    assert package_digest(result.run_dir, manifest) != manifest["package_sha256"]


def test_component_hash_includes_fallback_configuration(tmp_path):
    first = _run(tmp_path / "first", "one", gain=1.0)
    second = _run(tmp_path / "second", "two", gain=2.0)
    first_manifest = json.loads((first.run_dir / "manifest.json").read_text(encoding="utf-8"))
    second_manifest = json.loads((second.run_dir / "manifest.json").read_text(encoding="utf-8"))

    assert first_manifest["plant"]["hash"] != second_manifest["plant"]["hash"]
    assert first_manifest["plant"]["identity"]["configuration"]["gain"] == 1.0
    assert second_manifest["plant"]["identity"]["configuration"]["gain"] == 2.0
    assert first_manifest["provenance"]["plant_sha256"] == first_manifest["plant"]["hash"]


def test_environment_has_runtime_identity_and_explicit_backend_limitation(tmp_path):
    result = _run(tmp_path, "environment")
    manifest = json.loads((result.run_dir / "manifest.json").read_text(encoding="utf-8"))
    environment = json.loads((result.run_dir / "environment.json").read_text(encoding="utf-8"))

    assert environment["python"]["version"]
    assert environment["platform"]["system"]
    assert environment["status"] in {"known", "partial", "unknown"}
    assert environment["backends"]
    assert any(item["status"] == "not_declared" for item in environment["backends"])
    assert manifest["environment"] == environment
    assert "unknown" not in environment["python"]["version"].lower()


def test_declared_backend_and_solver_settings_are_recorded(tmp_path):
    class DeclaredBackendPlant(FakePlant):
        def backend_provenance(self):
            return {
                "name": "fixture-solver",
                "version": "1.2.3",
                "solver_settings": {"tolerance": 1e-9},
            }

    result = _run(tmp_path, "declared-backend", plant=DeclaredBackendPlant())
    environment = json.loads((result.run_dir / "environment.json").read_text(encoding="utf-8"))
    backend = next(item for item in environment["backends"] if item["name"] == "fixture-solver")
    assert backend["status"] == "known"
    assert backend["version"] == "1.2.3"
    assert backend["solver_settings"] == {"tolerance": 1e-9}


def test_failed_run_retains_complete_provenance(tmp_path):
    class FailingPlant(FakePlant):
        def advance(self, action, duration, external_input=None):
            raise TimeoutError("backend timed out")

    result = _run(tmp_path, "failed", plant=FailingPlant())
    manifest = json.loads((result.run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert result.status == "RUN_FAILED"
    assert manifest["source_commit"]
    assert manifest["environment"]["python"]["version"]
    assert manifest["plant"]["hash"]
    assert manifest["controller"]["hash"]
    assert manifest["manifest_sha256"] == manifest_digest(manifest)
    assert manifest["package_sha256"] == package_digest(result.run_dir, manifest)

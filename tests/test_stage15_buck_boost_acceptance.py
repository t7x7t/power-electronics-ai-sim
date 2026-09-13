"""Stage 15 L1 acceptance for the project-owned averaged Buck/Boost models.

These tests deliberately verify infrastructure and declared averaged-model
behavior only.  They do not test switching, Ngspice, thermal behavior, or a
hardware/product claim.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from pe_sim import ExperimentSpec, FakePIController, Timebase, check_controller_adapter, check_plant_adapter
from pe_sim.artifacts import manifest_digest, package_digest
from pe_sim.dirty import DirtyAnalysis
from pe_sim.evidence import classify_stage10
from pe_sim.postprocess import summarize_run
from pe_sim.provenance import GitProvenance
from pe_sim.reference_plants import BoostPlant, BuckPlant
from pe_sim.runtime import Runner
from pe_sim.cli import main as cli_main
import pe_sim.runtime as runtime_module


def _plant(factory, *, step: float = 2e-6, load: float = 10.0):
    return factory(
        input_voltage_v=12.0,
        inductance_h=100e-6,
        capacitance_f=470e-6,
        load_resistance_ohm=load,
        integration_step_s=step,
    )


def _spec(
    tmp_path: Path,
    plant,
    run_id: str,
    *,
    duration: float = 4e-5,
    input_schedule: tuple[dict[str, float], ...] = (),
) -> ExperimentSpec:
    return ExperimentSpec(
        "stage15-l1",
        run_id,
        plant.topology,
        "pi",
        Timebase(duration_s=duration, control_period_s=1e-5),
        input_schedule=input_schedule,
        output_dir=str(tmp_path),
        seed=17,
    )


def _run_reference_with_clean_provenance(tmp_path: Path, factory, run_id: str, monkeypatch):
    """Publish a real reference run under controlled test provenance.

    Stage 10 correctly rejects the developer's dirty worktree.  This helper
    changes only the Runner's process-local provenance collectors so the
    integration test can exercise the published-package gate with a real
    Buck/Boost artifact, rather than a hand-written package fixture.
    """

    observed_git = runtime_module.collect_git_provenance()
    observed_worktree = runtime_module.analyze_git_worktree()
    clean_git = GitProvenance(
        source_commit=observed_git.source_commit,
        branch=observed_git.branch,
        working_tree_status="clean",
        status="known",
        limitations=(),
    )
    clean_worktree = DirtyAnalysis(
        status="clean",
        source_commit=clean_git.source_commit,
        branch=clean_git.branch,
        repo_root=observed_worktree.repo_root,
        paths=(),
        paths_by_category={
            category: ()
            for category in ("source", "config", "tests", "docs", "generated", "other")
        },
        reasons=(),
        suggestions=(),
    )
    monkeypatch.setattr(runtime_module, "collect_git_provenance", lambda: clean_git)
    monkeypatch.setattr(runtime_module, "analyze_git_worktree", lambda: clean_worktree)
    plant = _plant(factory, step=1e-6)
    return Runner().run(
        _spec(tmp_path, plant, run_id),
        plant,
        FakePIController(kp=0.2),
        checkpoint_interval_steps=2,
    )


@pytest.mark.parametrize("factory", [BuckPlant, BoostPlant])
def test_plant_and_controller_conformance(factory):
    plant_report = check_plant_adapter(lambda: _plant(factory), action_bounds=(0.0, 1.0))
    controller_report = check_controller_adapter(FakePIController)
    assert plant_report.passed, plant_report.failures
    assert controller_report.passed, controller_report.failures


@pytest.mark.parametrize("factory,duty", [(BuckPlant, 0.4), (BoostPlant, 0.4)])
def test_analytical_equilibrium_matches_declared_equations(factory, duty):
    plant = _plant(factory)
    equilibrium = plant.analytical_steady_state(duty)
    expected_voltage = 12.0 * duty if factory is BuckPlant else 12.0 / (1.0 - duty)
    assert equilibrium["output_voltage_v"] == pytest.approx(expected_voltage)
    assert equilibrium["inductor_current_a"] == pytest.approx(expected_voltage / 10.0 / (1.0 - duty) if factory is BoostPlant else expected_voltage / 10.0)
    plant.reset(equilibrium)
    di, dv = plant._derivatives(duty, plant.inductor_current_a, plant.output_voltage_v)
    assert di == pytest.approx(0.0, abs=1e-12)
    assert dv == pytest.approx(0.0, abs=1e-12)


@pytest.mark.parametrize("factory,duty", [(BuckPlant, 0.2), (BuckPlant, 0.5), (BoostPlant, 0.2), (BoostPlant, 0.5)])
def test_step_halving_is_numerically_stable(factory, duty):
    coarse, fine = _plant(factory, step=2e-6), _plant(factory, step=1e-6)
    coarse.reset({})
    fine.reset({})
    coarse.advance(duty, 0.002)
    fine.advance(duty, 0.002)
    assert coarse.observe().measurement["vout"] == pytest.approx(
        fine.observe().measurement["vout"], rel=1e-7, abs=1e-8
    )


@pytest.mark.parametrize("factory", [BuckPlant, BoostPlant])
def test_reference_parameter_and_duty_boundaries_fail_closed(factory):
    with pytest.raises(ValueError, match="finite and positive"):
        _plant(factory, load=0.0)
    with pytest.raises(ValueError, match="finite and positive"):
        factory(input_voltage_v=12.0, inductance_h=float("nan"), capacitance_f=470e-6, load_resistance_ohm=10.0)
    plant = _plant(factory)
    plant.reset({})
    with pytest.raises(ValueError, match=r"finite and in \[0, 1\]"):
        plant.advance(float("nan"), 1e-5)
    with pytest.raises(ValueError, match=r"finite and in \[0, 1\]"):
        plant.advance(1.01, 1e-5)
    if factory is BoostPlant:
        with pytest.raises(ValueError, match="undefined"):
            plant.analytical_steady_state(1.0)


@pytest.mark.parametrize("factory", [BuckPlant, BoostPlant])
def test_small_duty_sweep_and_long_run_remain_finite(factory):
    for duty in (0.1, 0.3, 0.5):
        plant = _plant(factory)
        plant.reset({})
        observation = plant.advance(duty, 0.02)
        assert observation.time_s == pytest.approx(0.02)
        assert all(math.isfinite(float(value)) for value in observation.measurement.values())
        assert observation.measurement["vout"] >= 0.0


@pytest.mark.parametrize("factory", [BuckPlant, BoostPlant])
def test_runner_lifecycle_checkpoint_and_package_hashes(tmp_path, factory):
    plant = _plant(factory, step=1e-6)
    result = Runner().run(_spec(tmp_path, plant, f"{plant.topology}-lifecycle"), plant, FakePIController(kp=0.2), checkpoint_interval_steps=2)
    assert result.status == "QUALIFIED"
    manifest = json.loads((result.run_dir / "manifest.json").read_text(encoding="utf-8"))
    transitions = json.loads((result.run_dir / "state_transitions.json").read_text(encoding="utf-8"))
    audit = json.loads((result.run_dir / "audit.json").read_text(encoding="utf-8"))
    assert [(item["from"], item["to"]) for item in transitions] == [
        ("CREATED", "RUNNING"), ("RUNNING", "RUN_OK"), ("RUN_OK", "QUALIFIED")
    ]
    cycle = [
        "plant.observe",
        "safety.check_observation",
        "observation.observation-validity",
        "controller.observe",
        "safety.project_action",
        "plant.advance",
        "plant.observe",
        "advance_observation.observation-validity",
    ]
    expected_order = [
        "plant.capabilities",
        "plant.reset",
        "controller.reset",
        "plant.observe",
        "initial_observation.observation-validity",
        *cycle,
        *cycle,
        "plant.snapshot",
        *cycle,
        *cycle,
        "plant.snapshot",
        "qualification.check",
        "postrun.basic-sample-qualification",
    ]
    assert audit["call_order"] == expected_order
    assert [row["call_id"] for row in audit["calls"]] == list(range(1, len(audit["calls"]) + 1))
    assert all(row["ok"] is True for row in audit["calls"])
    assert audit["call_counts"] == {
        "plant.capabilities": 1,
        "plant.reset": 1,
        "controller.reset": 1,
        "plant.observe": 9,
        "initial_observation.observation-validity": 1,
        "safety.check_observation": 4,
        "observation.observation-validity": 4,
        "controller.observe": 4,
        "safety.project_action": 4,
        "plant.advance": 4,
        "advance_observation.observation-validity": 4,
        "plant.snapshot": 2,
        "qualification.check": 1,
        "postrun.basic-sample-qualification": 1,
    }
    assert manifest["audit"]["call_count"] == len(audit["calls"])
    assert (result.run_dir / "checkpoint.json").is_file()
    assert manifest["status"] == "QUALIFIED"
    assert manifest["manifest_sha256"] == manifest_digest(manifest)
    assert manifest["package_sha256"] == package_digest(result.run_dir, manifest)
    assert manifest["plant"]["identity"]["topology"] == plant.topology


@pytest.mark.parametrize("factory", [BuckPlant, BoostPlant])
def test_interrupted_reference_run_resumes_to_uninterrupted_baseline(tmp_path, factory):
    source_plant = _plant(factory, step=1e-6)
    interrupted = Runner().run(
        _spec(tmp_path, source_plant, f"{source_plant.topology}-source"),
        source_plant,
        FakePIController(kp=0.2),
        checkpoint_interval_steps=1,
        interrupt_after_steps=2,
    )
    assert interrupted.status == "INCOMPLETE"
    resumed_plant = _plant(factory, step=1e-6)
    resumed = Runner().run(
        _spec(tmp_path, resumed_plant, f"{resumed_plant.topology}-resumed"),
        resumed_plant,
        FakePIController(kp=0.2),
        resume_from=interrupted.run_dir,
    )
    baseline_plant = _plant(factory, step=1e-6)
    baseline = Runner().run(
        _spec(tmp_path, baseline_plant, f"{baseline_plant.topology}-baseline"),
        baseline_plant,
        FakePIController(kp=0.2),
    )
    assert resumed.status == "QUALIFIED"
    assert baseline.status == "QUALIFIED"
    assert json.loads((resumed.run_dir / "samples.json").read_text()) == json.loads(
        (baseline.run_dir / "samples.json").read_text()
    )
    interrupted_manifest = json.loads((interrupted.run_dir / "manifest.json").read_text())
    resumed_manifest = json.loads((resumed.run_dir / "manifest.json").read_text())
    baseline_manifest = json.loads((baseline.run_dir / "manifest.json").read_text())
    assert interrupted_manifest["status"] == "INCOMPLETE"
    assert resumed_manifest["recovery"]["resumed"] is True
    assert resumed_manifest["recovery"]["source"]["status"] == "INCOMPLETE"
    assert resumed_manifest["recovery"]["source"]["manifest_sha256"] == interrupted_manifest["manifest_sha256"]
    for name in ("plant", "controller"):
        assert resumed_manifest[name]["hash"] == baseline_manifest[name]["hash"]
    assert resumed_manifest["source_commit"] == baseline_manifest["source_commit"]
    assert resumed_manifest["manifest_sha256"] != baseline_manifest["manifest_sha256"]
    assert resumed_manifest["package_sha256"] != baseline_manifest["package_sha256"]
    for manifest in (interrupted_manifest, resumed_manifest, baseline_manifest):
        assert manifest["manifest_sha256"] == manifest_digest(manifest)
        assert manifest["package_sha256"] == package_digest(
            interrupted.run_dir if manifest is interrupted_manifest else resumed.run_dir if manifest is resumed_manifest else baseline.run_dir,
            manifest,
        )


@pytest.mark.parametrize("factory", [BuckPlant, BoostPlant])
def test_runner_applies_scheduled_input_voltage_update(tmp_path, factory):
    nominal_plant = _plant(factory, step=1e-6)
    nominal = Runner().run(
        _spec(tmp_path, nominal_plant, f"{nominal_plant.topology}-nominal"),
        nominal_plant,
        FakePIController(kp=0.2),
        checkpoint_interval_steps=1,
    )
    scheduled_plant = _plant(factory, step=1e-6)
    schedule = ({"time_s": 0.0, "vin_v": 12.0}, {"time_s": 2e-5, "vin_v": 9.0})
    scheduled = Runner().run(
        _spec(tmp_path, scheduled_plant, f"{scheduled_plant.topology}-vin-step", input_schedule=schedule),
        scheduled_plant,
        FakePIController(kp=0.2),
        checkpoint_interval_steps=1,
    )
    assert nominal.status == scheduled.status == "QUALIFIED"
    scheduled_samples = json.loads((scheduled.run_dir / "samples.json").read_text())
    nominal_samples = json.loads((nominal.run_dir / "samples.json").read_text())
    checkpoint = json.loads((scheduled.run_dir / "checkpoint.json").read_text())
    config = json.loads((scheduled.run_dir / "config.snapshot.json").read_text())
    assert config["input_schedule"] == list(schedule)
    assert checkpoint["plant_snapshot"]["input_voltage_v"] == pytest.approx(9.0)
    assert scheduled_samples[:3] == nominal_samples[:3]
    assert scheduled_samples[3]["vout"] != pytest.approx(nominal_samples[3]["vout"])


@pytest.mark.parametrize("factory", [BuckPlant, BoostPlant])
def test_identical_reference_runs_have_identical_samples(tmp_path, factory):
    first_plant, second_plant = _plant(factory), _plant(factory)
    first = Runner().run(_spec(tmp_path, first_plant, f"{first_plant.topology}-first"), first_plant, FakePIController(kp=0.2))
    second = Runner().run(_spec(tmp_path, second_plant, f"{second_plant.topology}-second"), second_plant, FakePIController(kp=0.2))
    first_manifest = json.loads((first.run_dir / "manifest.json").read_text())
    second_manifest = json.loads((second.run_dir / "manifest.json").read_text())
    assert json.loads((first.run_dir / "samples.json").read_text()) == json.loads((second.run_dir / "samples.json").read_text())
    assert first_manifest["plant"]["hash"] == second_manifest["plant"]["hash"]
    assert first_manifest["controller"]["hash"] == second_manifest["controller"]["hash"]


@pytest.mark.parametrize("factory", [BuckPlant, BoostPlant])
def test_stage10_to_stage12_evidence_chain_uses_published_reference_run(tmp_path, factory, monkeypatch):
    result = _run_reference_with_clean_provenance(
        tmp_path,
        factory,
        f"{factory.topology}-stage10-stage12",
        monkeypatch,
    )
    assert result.status == "QUALIFIED"
    published_state = json.loads((result.run_dir / ".run-state.json").read_text(encoding="utf-8"))
    manifest = json.loads((result.run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert published_state["status"] == "PUBLISHED"
    assert manifest["plant"]["identity"]["topology"] == factory.topology
    assert manifest["plant"]["identity"]["implementation"]["class"] == factory.__name__
    summary = summarize_run(result.run_dir)
    classification = classify_stage10(summary)
    assert classification["evidence_level"] == "functional"
    assert classification["runs"][0]["run_id"] == summary["provenance"]["run_id"]
    assert classification["runs"][0]["package_sha256"] == summary["provenance"]["package_sha256"]


@pytest.mark.parametrize("backend", ["buck", "boost"])
def test_cli_buck_and_boost_smoke(tmp_path, backend, capsys):
    config = {
        "schema_version": "0.1",
        "experiment_id": f"stage15-{backend}",
        "run_id": f"stage15-{backend}-cli",
        "plant_id": backend,
        "controller_id": "pi",
        "plant_config": {"input_voltage_v": 12.0, "inductance_h": 1e-4, "capacitance_f": 4.7e-4, "load_resistance_ohm": 10.0, "integration_step_s": 1e-6},
        "controller_config": {"kp": 0.2},
        "timebase": {"unit": "s", "control_period_s": 1e-5, "duration_s": 4e-5},
        "initial_state": {"mode": "cold_start"},
        "input_schedule": [],
        "seed": 17,
        "contracts": {"safety": {"id": "s", "hash": "0" * 64}, "qualification": {"id": "q", "hash": "0" * 64}},
        "output": {"directory": str(tmp_path), "retention": "keep"},
    }
    path = tmp_path / f"{backend}.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    assert cli_main(["run", str(path), "--backend", backend]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "QUALIFIED"
    assert (tmp_path / config["run_id"] / "manifest.json").is_file()

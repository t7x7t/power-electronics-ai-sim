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
from pe_sim.evidence import classify_summary
from pe_sim.postprocess import summarize_run
from pe_sim.reference_plants import BoostPlant, BuckPlant
from pe_sim.runtime import Runner
from pe_sim.cli import main as cli_main


def _plant(factory, *, step: float = 2e-6, load: float = 10.0):
    return factory(
        input_voltage_v=12.0,
        inductance_h=100e-6,
        capacitance_f=470e-6,
        load_resistance_ohm=load,
        integration_step_s=step,
    )


def _spec(tmp_path: Path, plant, run_id: str, *, duration: float = 4e-5) -> ExperimentSpec:
    return ExperimentSpec(
        "stage15-l1",
        run_id,
        plant.topology,
        "pi",
        Timebase(duration_s=duration, control_period_s=1e-5),
        output_dir=str(tmp_path),
        seed=17,
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
    assert [(item["from"], item["to"]) for item in transitions] == [
        ("CREATED", "RUNNING"), ("RUNNING", "RUN_OK"), ("RUN_OK", "QUALIFIED")
    ]
    assert (result.run_dir / "checkpoint.json").is_file()
    assert manifest["status"] == "QUALIFIED"
    assert manifest["manifest_sha256"] == manifest_digest(manifest)
    assert manifest["package_sha256"] == package_digest(result.run_dir, manifest)
    assert manifest["plant"]["identity"]["topology"] == plant.topology


@pytest.mark.parametrize("factory", [BuckPlant, BoostPlant])
def test_interrupted_reference_run_resumes(tmp_path, factory):
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
    assert resumed.status == "QUALIFIED"
    assert json.loads((resumed.run_dir / "samples.json").read_text())
    assert json.loads((resumed.run_dir / "manifest.json").read_text())["recovery"]["resumed"] is True


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


def test_stage10_to_stage12_evidence_chain(tmp_path):
    # A standalone published-package fixture keeps this acceptance test
    # independent from the current worktree's exploratory dirty status.
    from test_stage10_restricted_postprocess import _published_run

    run = _published_run(tmp_path / "published")
    summary = summarize_run(run)
    classification = classify_summary(summary)
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

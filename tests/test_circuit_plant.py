"""Execute a compiled topology as a stepped Plant adapter.

The compiler is proven against the reviewed netlists by ``test_circuit_compile``;
these tests hold the execution half: the adapter must resume from element state,
report the solver configuration it actually rendered, keep a complementary pair
on the same timeline, and expose the result through the CLI without pretending
that an unreviewed circuit is comparable evidence.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from pe_sim.backends import CircuitPlant
from pe_sim.backends.ngspice_plants import NgspiceBuckPlant
from pe_sim.backends.settings import NgspiceSolverSettings
from pe_sim.backends.sync_buck import NgspiceSyncBuckPlant
from pe_sim.cli import main as cli_main
from pe_sim.topology import (
    ngspice_buck_topology_descriptor,
    ngspice_sync_buck_topology_descriptor,
)


NGSPICE = os.environ.get("PE_SIM_NGSPICE")
needs_ngspice = pytest.mark.skipif(
    not NGSPICE,
    reason="set PE_SIM_NGSPICE to run the compiled-circuit execution tests",
)

PARAMETERS = dict(
    input_voltage_v=12.0,
    inductance_h=100e-6,
    capacitance_f=470e-6,
    load_resistance_ohm=10.0,
    switching_frequency_hz=100e3,
    control_period_s=10e-6,
    dead_time_s=100e-9,
)


def _plant(**overrides):
    values = dict(PARAMETERS)
    values.update(overrides)
    return SimpleNamespace(**values)


def _buck_descriptor():
    return ngspice_buck_topology_descriptor(_plant())


def _unused_solver(netlist: str):
    raise AssertionError("this test must not execute the solver")


def _plant_for(descriptor, **overrides):
    values = {
        "control_period_s": 10e-6,
        "solver_callable": _unused_solver,
        "sample_step_s": 50e-9,
    }
    values.update(overrides)
    return CircuitPlant(descriptor, **values)


# ---------------------------------------------------------------------------
# Construction and declarations
# ---------------------------------------------------------------------------


def test_the_adapter_names_its_output_and_elements_from_the_descriptor():
    plant = _plant_for(_buck_descriptor())

    assert plant.topology == "buck"
    assert plant.output_net == "vout"
    assert plant.element_names["inductor"] == "L1"
    assert plant.element_names["capacitor"] == "C1"
    assert plant.capabilities() == {"continuous_time", "external_input", "snapshot"}
    assert plant.action_policy().name == "duty"


def test_an_unreviewed_circuit_reports_not_pinned():
    identity = _plant_for(_buck_descriptor()).manifest_identity()

    assert identity["implementation"]["review"]["status"] == "not_pinned"
    assert identity["netlist_template"] == "circuit-netlist-v1"
    assert identity["descriptor"]["topology_id"] == "buck"
    assert set(identity["descriptor"]["nets"][0]) >= {"net_id", "kind"}


def test_the_rendered_options_are_the_recorded_solver_options():
    solver = NgspiceSolverSettings(
        integration_method="euler",
        abstol=2e-10,
        reltol=2e-3,
        num_digits=9,
        temperature_c=50.0,
    )
    plant = _plant_for(
        _buck_descriptor(),
        solver=solver,
        control_period_s=10e-6,
    )

    netlist = plant.preview_netlist(duty=0.5, duration_s=1e-4)

    assert "abstol=2.000000000000e-10" in netlist
    assert "method=euler" in netlist
    assert "set numdgt=9" in netlist
    assert ".temp 5.000000000000e+01" in netlist
    assert plant.manifest_identity()["solver_settings"]["integration_method"] == "euler"


def test_a_circuit_with_two_loads_is_refused():
    descriptor = _buck_descriptor()
    load = next(
        item for item in descriptor.components if item.component_id == "load"
    )
    extra = type(load)(
        "load2",
        load.kind,
        load.ports,
        parameters=dict(load.parameters),
        symbol_id=load.symbol_id,
    )
    components = descriptor.components + (extra,)
    rebuilt = type(descriptor)(
        descriptor.topology_id,
        components,
        descriptor.nets,
        metadata=descriptor.metadata,
    )

    with pytest.raises(ValueError, match="exactly one resistor_load"):
        _plant_for(rebuilt)


def test_reset_echoes_the_requested_initial_state():
    plant = _plant_for(_buck_descriptor())

    applied = plant.reset({"inductor_current_a": 1.5, "vout": 3.0})

    assert applied["inductor_current_a"] == pytest.approx(1.5)
    assert applied["vout"] == pytest.approx(3.0)
    observation = plant.observe()
    assert observation.measurement["vout"] == pytest.approx(3.0)
    assert observation.measurement["inductor_current"] == pytest.approx(1.5)


def test_reset_refuses_a_state_for_something_the_circuit_does_not_have():
    plant = _plant_for(_buck_descriptor())

    with pytest.raises(ValueError, match="unsupported initial_state key"):
        plant.reset({"rotor_speed": 1.0})


# ---------------------------------------------------------------------------
# Real-solver behaviour
# ---------------------------------------------------------------------------


def _executable_plant(descriptor, **overrides):
    values = {
        "control_period_s": 10e-6,
        "ngspice_executable": NGSPICE,
        "sample_step_s": 50e-9,
    }
    values.update(overrides)
    return CircuitPlant(descriptor, **values)


@needs_ngspice
def test_a_compiled_buck_settles_where_the_reviewed_adapter_settles():
    duration = 20e-3
    compiled = _executable_plant(_buck_descriptor())
    compiled.reset({})
    compiled_result = compiled.advance(0.5, duration)

    reviewed = NgspiceBuckPlant(
        control_period_s=10e-6,
        ngspice_executable=NGSPICE,
        sample_step_s=50e-9,
    )
    reviewed.reset({})
    reviewed_result = reviewed.advance(0.5, duration)

    assert compiled_result.measurement["vout"] == pytest.approx(
        reviewed_result.measurement["vout"], abs=1e-3
    )
    assert compiled_result.measurement["vout"] == pytest.approx(0.5 * 12.0, abs=0.15)


@needs_ngspice
def test_two_windows_resume_where_one_long_window_ends():
    one = _executable_plant(_buck_descriptor())
    one.reset({})
    single = one.advance(0.5, 2e-3)

    two = _executable_plant(_buck_descriptor())
    two.reset({})
    two.advance(0.5, 1e-3)
    windowed = two.advance(0.5, 1e-3)

    assert windowed.measurement["vout"] == pytest.approx(
        single.measurement["vout"], abs=1e-4
    )
    assert windowed.measurement["inductor_current"] == pytest.approx(
        single.measurement["inductor_current"], abs=1e-4
    )


@needs_ngspice
def test_restore_reproduces_the_next_window_exactly():
    continuous = _executable_plant(_buck_descriptor())
    continuous.reset({})
    continuous.advance(0.5, 1e-3)
    continuous.advance(0.5, 1e-3)
    snapshot = continuous.snapshot()
    reference = continuous.advance(0.5, 1e-3)

    resumed = _executable_plant(_buck_descriptor())
    resumed.restore(snapshot)
    result = resumed.advance(0.5, 1e-3)

    assert result.measurement["vout"] == pytest.approx(
        reference.measurement["vout"], abs=1e-9
    )
    assert result.measurement["inductor_current"] == pytest.approx(
        reference.measurement["inductor_current"], abs=1e-12
    )


@needs_ngspice
def test_an_external_voltage_update_changes_the_next_window():
    plant = _executable_plant(_buck_descriptor())
    plant.reset({})
    plant.advance(0.5, 1e-3)
    lowered = plant.advance(0.5, 1e-3, {"vin_v": 9.0})

    steady = _executable_plant(_buck_descriptor())
    steady.reset({})
    steady.advance(0.5, 1e-3)
    unchanged = steady.advance(0.5, 1e-3)

    assert plant.input_voltage_v == pytest.approx(9.0)
    assert lowered.measurement["vout"] < unchanged.measurement["vout"]


@needs_ngspice
def test_a_complementary_circuit_matches_the_reviewed_sync_adapter():
    compiled = _executable_plant(ngspice_sync_buck_topology_descriptor(_plant()))
    compiled.reset({})
    compiled_result = compiled.advance(0.5, 1e-3)

    reviewed = NgspiceSyncBuckPlant(
        control_period_s=10e-6,
        dead_time_s=100e-9,
        ngspice_executable=NGSPICE,
        sample_step_s=50e-9,
    )
    reviewed.reset({})
    reviewed_result = reviewed.advance(0.5, 1e-3)

    assert compiled.last_compiled().control_mode == "single_duty_complementary"
    assert compiled_result.measurement["vout"] == pytest.approx(
        reviewed_result.measurement["vout"], abs=1e-3
    )


# ---------------------------------------------------------------------------
# CLI surface
# ---------------------------------------------------------------------------


def _write_document(tmp_path: Path, document: dict) -> Path:
    document = json.loads(json.dumps(document))
    document.pop("fingerprint", None)
    path = tmp_path / "topology.json"
    path.write_text(json.dumps(document, indent=2), encoding="utf-8")
    return path


def test_simulate_topology_reports_a_missing_solver(tmp_path, capsys):
    path = _write_document(tmp_path, _buck_descriptor().to_dict())

    assert (
        cli_main(
            [
                "simulate-topology",
                str(path),
                "--duty",
                "0.5",
                "--duration",
                "1e-4",
                "--ngspice",
                str(tmp_path / "missing-ngspice.exe"),
            ]
        )
        == 2
    )
    payload = json.loads(capsys.readouterr().out)

    assert payload["error_code"] == "invalid_request"
    assert payload["reason"] == "backend_not_found"


@needs_ngspice
def test_simulate_topology_writes_a_report_and_bounded_samples(tmp_path, capsys):
    path = _write_document(tmp_path, _buck_descriptor().to_dict())
    output = tmp_path / "out"

    assert (
        cli_main(
            [
                "simulate-topology",
                str(path),
                "--duty",
                "0.5",
                "--duration",
                "1e-4",
                "--step",
                "5e-8",
                "--ngspice",
                NGSPICE,
                "--output-dir",
                str(output),
            ]
        )
        == 0
    )
    report = json.loads(capsys.readouterr().out)

    assert report["schema"] == "pe-sim.topology-simulation.v1"
    assert report["status"] == "COMPLETED"
    assert report["evidence_level"] == "limited"
    assert report["netlist"]["template"] == "circuit-netlist-v1"
    assert report["observation"]["measurement"]["vout"] > 0.0
    assert report["samples"]["points"] > 0
    assert (output / "simulation.json").is_file()
    samples = json.loads((output / "samples.json").read_text(encoding="utf-8"))
    assert samples["schema"] == "pe-sim.topology-samples.v1"
    assert len(samples["time_s"]) == report["samples"]["points"]
    assert set(samples["signals"]) == set(report["samples"]["signals"])


def _run_document(tmp_path: Path) -> Path:
    document = {
        "schema_version": "0.1",
        "experiment_id": "circuit-topology-run",
        "run_id": "circuit-topology-run",
        "plant_id": "circuit-topology",
        "controller_id": "pi",
        "plant_config": {
            "topology": _buck_descriptor().to_dict(),
            "control_period_s": 10e-6,
            "sample_step_s": 50e-9,
        },
        "controller_config": {"kp": 0.1, "ki": 0.0},
        "timebase": {"unit": "s", "control_period_s": 10e-6, "duration_s": 2e-4},
        "initial_state": {"mode": "cold_start"},
        "input_schedule": [{"time_s": 0.0, "vref": 5.0}],
        "seed": 0,
        "contracts": {
            "safety": {
                "id": "default-safety",
                "hash": "c8af0de8cfeec979bf5fd17dbf1c819df90d6fc5a95fc3a2f53d84271ba0ef58",
            },
            "qualification": {
                "id": "default-qualification",
                "hash": "a3ef375ff21b886d76977854178c77a5bd397e551170fe13e4db9e606f9f121e",
            },
        },
        "output": {"directory": str(tmp_path / "runs"), "retention": "keep"},
    }
    path = tmp_path / "run.json"
    path.write_text(json.dumps(document, indent=2), encoding="utf-8")
    return path


@needs_ngspice
def test_the_runner_accepts_an_authored_topology_as_a_plant(tmp_path, capsys):
    path = _run_document(tmp_path)

    assert cli_main(["run", str(path), "--ngspice", NGSPICE]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["plant_id"] == "circuit-topology"
    assert payload["status"] in {"QUALIFIED", "RUN_OK"}
    assert payload["evidence_level"] == "functional"
    manifest = json.loads(
        (Path(payload["run_dir"]) / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["plant"]["id"] == "circuit-topology"
    assert manifest["plant"]["verification"]["status"] == "verified"
    assert (
        manifest["plant"]["identity"]["implementation"]["review"]["status"]
        == "not_pinned"
    )
    assert manifest["route"]["event_granularity"] == "continuous"
    assert manifest["route"]["control_location"] == "kernel"
    assert manifest["plant"]["evidence"]["solver_invocations"] > 0

"""Opt-in acceptance tests for the real ngspice backend.

Set ``PE_SIM_NGSPICE`` to the reviewed executable to run these tests.  They are
skipped in ordinary development so a clean source checkout does not require an
external simulator.  This suite is local infrastructure acceptance only; it
does not certify device, thermal, hardware, or product behaviour.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from pe_sim.backends import NgspiceBuckPlant, NgspiceExecutableAdapter, NgspiceSolverSettings
from pe_sim.cli import main
from pe_sim.conformance import check_plant_adapter


NGSPICE = os.environ.get("PE_SIM_NGSPICE")
pytestmark = pytest.mark.skipif(
    not NGSPICE,
    reason="set PE_SIM_NGSPICE to run the real ngspice acceptance tests",
)

_PARAMETERS = {
    "input_voltage_v": 12.0,
    "inductance_h": 100e-6,
    "capacitance_f": 470e-6,
    "load_resistance_ohm": 10.0,
    "switching_frequency_hz": 100e3,
    "control_period_s": 10e-6,
}


def _plant(**overrides) -> NgspiceBuckPlant:
    values = dict(_PARAMETERS)
    values.update(overrides)
    return NgspiceBuckPlant(
        ngspice_executable=NGSPICE,
        timeout_s=120.0,
        **values,
    )


def test_real_backend_passes_the_plant_contract():
    report = check_plant_adapter(
        lambda: _plant(),
        action=0.5,
        duration_s=10e-6,
        action_bounds=(0.0, 1.0),
    )

    assert report.passed, report.failures


def test_real_backend_reports_a_known_executable_identity():
    evidence = NgspiceExecutableAdapter(NGSPICE, solver=NgspiceSolverSettings()).backend_provenance()

    assert evidence["status"] == "known", evidence
    assert evidence["version"]
    assert evidence["solver_settings"]["integration_method"] == "gear"
    assert evidence["executable_sha256"] and len(evidence["executable_sha256"]) == 64


def test_constant_duty_reaches_the_ideal_equilibrium_within_declared_tolerance():
    # For D=0.5, Vin=12 V, R=10 ohm, L=100 uH and Tsw=10 us the ideal CCM
    # ripple is 0.3 A peak-to-peak around 0.6 A.  A window starts at the
    # on-time edge, so the periodic steady-state current there is the valley
    # 0.45 A, not the average 0.6 A.
    plant = _plant()
    plant.reset({"inductor_current_a": 0.45, "output_voltage_v": 6.0})

    observation = None
    for _ in range(50):
        observation = plant.advance(0.5, 10e-6)

    assert observation is not None
    assert observation.measurement["vout"] == pytest.approx(6.0, rel=0.02)
    assert observation.measurement["inductor_current"] == pytest.approx(0.45, rel=0.10)
    assert observation.time_s == pytest.approx(500e-6, abs=1e-12)


def test_identical_runs_produce_identical_samples():
    def run() -> list[tuple[float, float, float]]:
        plant = _plant()
        plant.reset({"inductor_current_a": 0.6, "output_voltage_v": 6.0})
        rows = []
        for _ in range(5):
            observation = plant.advance(0.5, 10e-6)
            rows.append(
                (
                    observation.time_s,
                    observation.measurement["vout"],
                    observation.measurement["inductor_current"],
                )
            )
        return rows

    assert run() == run()


def test_pyspice_model_builder_executes_through_the_reviewed_adapter():
    pytest.importorskip("PySpice")
    from pe_sim.backends import PyspiceBuckNetlistBuilder

    plant = _plant(netlist_builder=PyspiceBuckNetlistBuilder())
    plant.reset({"inductor_current_a": 0.45, "output_voltage_v": 6.0})

    observation = plant.advance(0.5, 10e-6)

    assert observation.measurement["vout"] == pytest.approx(6.0, rel=0.02)
    assert observation.measurement["inductor_current"] == pytest.approx(0.45, rel=0.10)
    identity = plant.manifest_identity()
    assert identity["netlist_builder"]["name"] == "pyspice-model"
    assert identity["netlist_builder"]["execution"] == "external-ngspice-process"


def test_cli_publishes_a_qualified_package_with_known_backend(tmp_path, monkeypatch):
    pytest.importorskip("jsonschema")
    monkeypatch.chdir(tmp_path)
    config = Path(__file__).resolve().parent.parent / "examples" / "ngspice_buck_step" / "config.json"

    exit_code = main(
        ["run", str(config), "--backend", "ngspice-buck", "--ngspice", str(NGSPICE)]
    )

    assert exit_code == 0
    manifest_path = tmp_path / "runs" / "ngspice-buck-reference" / "manifest.json"
    assert manifest_path.is_file()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["status"] == "QUALIFIED"
    assert manifest["plant"]["identity"]["kind"] == "ngspice_switching_reference"
    backends = manifest["environment"]["backends"]
    assert backends and backends[0]["status"] == "known"
    assert backends[0]["version"]
    assert backends[0]["solver_settings"]

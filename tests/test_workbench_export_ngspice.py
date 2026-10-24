"""Workbench exporter tests for the switching-level family.

The exporter itself is registration-driven, so these fixtures record what a
real package records: the plant id, the adapter class, and the parameters the
reviewed descriptor renders.
"""

from __future__ import annotations

import pytest

from pe_sim.workbench_export import WorkbenchExportError, _topology_descriptor


def _manifest(
    kind: str,
    topology: str = "buck",
    *,
    plant_id: str = "ngspice-buck",
    plant_class: str = "NgspiceBuckPlant",
):
    return {
        "plant": {
            "id": plant_id,
            "identity": {
                "kind": kind,
                "topology": topology,
                "parameters": {
                    "input_voltage_v": 12.0,
                    "inductance_h": 100e-6,
                    "capacitance_f": 470e-6,
                    "load_resistance_ohm": 10.0,
                    "switching_frequency_hz": 100e3,
                    "control_period_s": 10e-6,
                },
                "implementation": {"class": plant_class},
            },
        }
    }


def test_switching_descriptor_is_exported_from_manifest_parameters():
    descriptor = _topology_descriptor(_manifest("ngspice_switching_reference"))

    assert descriptor.metadata["model_kind"] == "ngspice_switching_reference"
    assert descriptor.metadata["backend"] == "ngspice"
    assert descriptor.metadata["control_period_s"] == pytest.approx(10e-6)
    kinds = {component.kind for component in descriptor.components}
    assert "voltage_controlled_switch" in kinds


def test_switching_descriptor_requires_complete_parameters():
    manifest = _manifest("ngspice_switching_reference")
    del manifest["plant"]["identity"]["parameters"]["switching_frequency_hz"]

    with pytest.raises(WorkbenchExportError, match="manifest plant parameters are invalid"):
        _topology_descriptor(manifest)


def test_unknown_plant_family_fails_closed():
    manifest = _manifest(
        "unreviewed_backend", plant_id="unreviewed-backend", plant_class="UnreviewedPlant"
    )

    with pytest.raises(WorkbenchExportError, match="no reviewed workbench topology exporter"):
        _topology_descriptor(manifest)


def test_a_recorded_class_that_contradicts_the_registration_fails_closed():
    manifest = _manifest("ideal_averaged_reference", "boost", plant_class="BoostPlant")

    with pytest.raises(WorkbenchExportError, match="recorded class"):
        _topology_descriptor(manifest)


def test_published_switching_package_exports_the_three_contracts(tmp_path):
    import re

    from pe_sim.backends.ngspice_plants import NgspiceBuckPlant
    from pe_sim.backends.process import NgspiceRunResult
    from pe_sim.backends.wrdata import TraceTable
    from pe_sim.contracts import ExperimentSpec, Timebase
    from pe_sim.runtime import FakePIController, Runner
    from pe_sim.workbench_export import export_run_to_workbench_fixture

    tran = re.compile(r"^\.tran\s+(\S+)\s+(\S+)", re.MULTILINE)

    def solver(netlist: str) -> NgspiceRunResult:
        duration = float(tran.search(netlist).group(2))
        points = 21
        times = tuple(duration * index / (points - 1) for index in range(points))
        return NgspiceRunResult(
            returncode=0,
            stdout="",
            stderr="",
            log="",
            table=TraceTable(
                times,
                {
                    "v(vout)": tuple(3.0 + 2.0 * index / (points - 1) for index in range(points)),
                    "i(l1)": tuple(1.0 + index / (points - 1) for index in range(points)),
                    "v(sw)": tuple(12.0 if index % 4 < 2 else 0.0 for index in range(points)),
                },
            ),
            netlist_sha256="a" * 64,
            output_sha256="b" * 64,
            log_sha256="c" * 64,
            duration_s=0.0,
            command=("fake-ngspice",),
        )

    spec = ExperimentSpec(
        "workbench-ngspice",
        "workbench-ngspice",
        "ngspice-buck",
        "pi",
        Timebase(duration_s=20e-6, control_period_s=10e-6),
        output_dir=str(tmp_path / "runs"),
        seed=0,
    )
    plant = NgspiceBuckPlant(solver_callable=solver, control_period_s=10e-6)
    result = Runner().run(spec, plant, FakePIController(kp=0.2))
    assert result.status == "QUALIFIED"

    destination = tmp_path / "fixture"
    export = export_run_to_workbench_fixture(result.run_dir, destination)

    for name in ("topology.json", "trace.json", "outputs.json"):
        assert (destination / name).is_file()
    assert export.topology.metadata["model_kind"] == "ngspice_switching_reference"
    assert export.trace.provenance.manifest_sha256 == export.manifest_sha256

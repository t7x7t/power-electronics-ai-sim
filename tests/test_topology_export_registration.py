"""T2 completion: the workbench exporter is registration data, not a switch.

Before this increment `pe_sim.workbench_export` named topologies
(`plant_id == "buck"`), so adding a topology still required editing a module.
The exporter is now a declared field of `PlantRegistration`, and the export
decision is made from three declarations: `export_eligible`, `class_name`, and
`topology_exporter`.

Every negative case here fails **closed and before writing**: a package whose
plant is not registered, is not export-eligible, declares no exporter, records a
different class, or whose exporter returns the wrong object must not produce a
derived fixture.
"""

from __future__ import annotations

import json
from pathlib import Path
import shutil

import pytest

from pe_sim.builtin_plants import load_builtin_plants
from pe_sim.cli import main
from pe_sim.plant_registry import (
    PlantRegistration,
    clear_registrations,
    plant_catalog,
    register_plant,
)
from pe_sim.reference_plants import BuckPlant
from pe_sim.topology import TopologyDescriptor, buck_topology_descriptor
from pe_sim.workbench_export import WorkbenchExportError, export_run_to_workbench_fixture

_REPOSITORY = Path(__file__).resolve().parents[1]
_OLDER_PACKAGE = _REPOSITORY / "tests" / "fixtures" / "older_packages" / "v0.5.9-buck"

_CONTRACTS = {
    "safety": {
        "id": "default-safety",
        "hash": "c8af0de8cfeec979bf5fd17dbf1c819df90d6fc5a95fc3a2f53d84271ba0ef58",
    },
    "qualification": {
        "id": "default-qualification",
        "hash": "a3ef375ff21b886d76977854178c77a5bd397e551170fe13e4db9e606f9f121e",
    },
}


class DemoTopologyPlant(BuckPlant):
    """A distinct plant class that exists only in this test module.

    It reuses the reviewed averaged model so the package carries real
    observations, which is what the export path reads.  The point of the test is
    the registration, not the physics.
    """


_PLANT_CONFIG = {
    "input_voltage_v": 12.0,
    "inductance_h": 100e-6,
    "capacitance_f": 470e-6,
    "load_resistance_ohm": 10.0,
    "integration_step_s": 1e-6,
}


def _registration(**overrides) -> PlantRegistration:
    values = {
        "plant_id": "demo-topology",
        "backend_id": "demo-topology",
        "factory": lambda config, context: DemoTopologyPlant(**dict(config)),
        "class_name": "DemoTopologyPlant",
        "trust_level": "reference",
        "export_eligible": True,
        "topology_exporter": lambda parameters: buck_topology_descriptor(),
    }
    values.update(overrides)
    return PlantRegistration(**values)


@pytest.fixture(autouse=True)
def _registry_baseline():
    clear_registrations()
    yield
    clear_registrations()
    load_builtin_plants()


def _run_demo_plant(tmp_path: Path) -> Path:
    """Publish one small package with the runtime-registered plant."""

    register_plant(_registration())
    document = {
        "schema_version": "0.1",
        "experiment_id": "topology-data",
        "run_id": "demo-topology-run",
        "plant_id": "demo-topology",
        "controller_id": "pi",
        "plant_config": dict(_PLANT_CONFIG),
        "controller_config": {"kp": 1.0},
        "timebase": {"unit": "s", "control_period_s": 0.001, "duration_s": 0.002},
        "initial_state": {"mode": "cold_start"},
        "input_schedule": [{"time_s": 0.0, "vref": 1.0}],
        "seed": 0,
        "contracts": _CONTRACTS,
        "output": {"directory": str(tmp_path / "runs"), "retention": "keep"},
    }
    config = tmp_path / "config.json"
    config.write_text(json.dumps(document) + "\n", encoding="utf-8")

    assert main(["run", str(config)]) == 0
    return tmp_path / "runs" / "demo-topology-run"


def test_a_registered_exporter_is_enough_to_export(tmp_path, capsys):
    run_dir = _run_demo_plant(tmp_path)
    capsys.readouterr()

    exported = export_run_to_workbench_fixture(run_dir, tmp_path / "derived")

    assert set(exported.files) == {"topology.json", "trace.json", "outputs.json"}
    topology = TopologyDescriptor.from_dict(
        json.loads(exported.files["topology.json"].read_text(encoding="utf-8"))
    )
    assert topology.topology_id == "buck"
    # The exporter is an identity projection, not evidence of correctness.
    trace = json.loads(exported.files["trace.json"].read_text(encoding="utf-8"))
    assert trace["provenance"]["metadata"]["integrity"] == "verified"


def test_the_catalog_reports_whether_a_plant_is_displayable(tmp_path):
    load_builtin_plants()
    register_plant(_registration())
    entries = {item["plant_id"]: item for item in plant_catalog()["plants"]}

    assert entries["demo-topology"]["topology_export"] is True
    assert entries["fake"]["topology_export"] is False


def test_export_eligible_without_an_exporter_fails_closed(tmp_path, capsys):
    run_dir = _run_demo_plant(tmp_path)
    capsys.readouterr()
    clear_registrations()
    register_plant(_registration(topology_exporter=None))

    with pytest.raises(WorkbenchExportError, match="no workbench topology exporter"):
        export_run_to_workbench_fixture(run_dir, tmp_path / "derived")

    assert not (tmp_path / "derived").exists()


def test_export_ineligible_plant_fails_closed(tmp_path, capsys):
    run_dir = _run_demo_plant(tmp_path)
    capsys.readouterr()
    clear_registrations()
    register_plant(_registration(export_eligible=False))

    with pytest.raises(WorkbenchExportError, match="not eligible for workbench export"):
        export_run_to_workbench_fixture(run_dir, tmp_path / "derived")

    assert not (tmp_path / "derived").exists()


def test_a_recorded_class_mismatch_fails_closed(tmp_path, capsys):
    run_dir = _run_demo_plant(tmp_path)
    capsys.readouterr()
    clear_registrations()
    register_plant(_registration(class_name="SomeOtherPlant"))

    with pytest.raises(WorkbenchExportError, match="recorded class"):
        export_run_to_workbench_fixture(run_dir, tmp_path / "derived")

    assert not (tmp_path / "derived").exists()


def test_an_exporter_returning_the_wrong_object_fails_closed(tmp_path, capsys):
    run_dir = _run_demo_plant(tmp_path)
    capsys.readouterr()
    clear_registrations()
    register_plant(_registration(topology_exporter=lambda parameters: {"topology_id": "buck"}))

    with pytest.raises(WorkbenchExportError, match="expected a TopologyDescriptor"):
        export_run_to_workbench_fixture(run_dir, tmp_path / "derived")

    assert not (tmp_path / "derived").exists()


def test_an_unregistered_plant_fails_closed(tmp_path, capsys):
    run_dir = _run_demo_plant(tmp_path)
    capsys.readouterr()
    clear_registrations()

    with pytest.raises(WorkbenchExportError, match="demo-topology"):
        export_run_to_workbench_fixture(run_dir, tmp_path / "derived")

    assert not (tmp_path / "derived").exists()


def test_a_legacy_package_still_exports_through_its_registered_exporter(tmp_path):
    """A 0.5.9 package records the class, so the registration decides the export."""

    exported = export_run_to_workbench_fixture(_OLDER_PACKAGE, tmp_path / "derived")

    topology = TopologyDescriptor.from_dict(
        json.loads(exported.files["topology.json"].read_text(encoding="utf-8"))
    )
    assert topology.topology_id == "buck"
    assert set(exported.files) == {"topology.json", "trace.json", "outputs.json"}


def test_a_whole_run_package_exports_all_three_contracts(tmp_path, capsys):
    """The whole-run engine declares an exporter, so its evidence is displayable."""

    document = json.loads(
        (_REPOSITORY / "examples" / "whole_run_buck_step" / "config.json").read_text(encoding="utf-8")
    )
    document["run_id"] = "whole-run-export"
    document["output"]["directory"] = str(tmp_path / "runs")
    config = tmp_path / "whole-run.json"
    config.write_text(json.dumps(document) + "\n", encoding="utf-8")

    assert main(["run", str(config)]) == 0
    capsys.readouterr()
    run_dir = tmp_path / "runs" / "whole-run-export"

    exported = export_run_to_workbench_fixture(run_dir, tmp_path / "derived")

    assert set(exported.files) == {"topology.json", "trace.json", "outputs.json"}
    topology = TopologyDescriptor.from_dict(
        json.loads(exported.files["topology.json"].read_text(encoding="utf-8"))
    )
    assert topology.topology_id == "buck"
    assert topology.metadata["time_model"] == "whole_run"
    trace = json.loads(exported.files["trace.json"].read_text(encoding="utf-8"))
    signal_ids = {item["signal_id"] for item in trace["signals"]}
    assert signal_ids == {
        "plant.vout",
        "plant.inductor_current",
        "controller.duty",
    }


def test_the_exporter_module_names_no_topology():
    """The last topology switch is gone: the module is data-driven."""

    import ast

    for relative in (
        "src/pe_sim/workbench_export.py",
        "src/pe_sim/plant_registry.py",
        "src/pe_sim/runtime.py",
        "src/pe_sim/cli.py",
    ):
        tree = ast.parse((_REPOSITORY / relative).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Compare):
                targets = [node.left, *node.comparators]
                literals = {
                    item.value
                    for item in targets
                    if isinstance(item, ast.Constant) and isinstance(item.value, str)
                }
                assert not (literals & {"buck", "boost", "ngspice-buck"}), (
                    f"{relative} compares against a topology name: {sorted(literals)}"
                )

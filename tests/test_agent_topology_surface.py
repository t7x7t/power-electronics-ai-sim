"""The agent-facing topology surface: ``describe-devices``, ``validate-topology``,
``compile-topology``, ``import-spice``.

These tests go through the CLI because that is the interface an agent calls.
They hold the same three promises the specification surface holds, for the other
half of authoring a circuit:

- one declaration answers "what may I draw?" (``describe-devices``) and the same
  declaration is what validation enforces;
- validation and compilation are compile-time checks: they never build a plant,
  start a solver, or create a run directory;
- a refusal carries a structured issue list, so an agent repairs a document
  instead of parsing one flattened string.

``import-spice`` holds a fourth: a netlist that already exists reaches the same
descriptor contract, so importing never becomes a second execution path.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from pe_sim.builtin_plants import load_builtin_plants
from pe_sim.circuit_devices import CIRCUIT_DEVICE_SCHEMA, RENDERABLE_DEVICE_KINDS
from pe_sim.cli import main as cli_main
from pe_sim.plant_registry import clear_registrations
from pe_sim.topology import buck_topology_descriptor, ngspice_buck_topology_descriptor
from pe_sim.topology_validation import TOPOLOGY_VALIDATION_SCHEMA


@pytest.fixture(autouse=True)
def _isolated_registry():
    clear_registrations()
    yield
    clear_registrations()


def _plant():
    return SimpleNamespace(
        input_voltage_v=12.0,
        inductance_h=100e-6,
        capacitance_f=470e-6,
        load_resistance_ohm=10.0,
        switching_frequency_hz=100e3,
        control_period_s=10e-6,
        dead_time_s=100e-9,
    )


def _write(tmp_path: Path, document: dict) -> Path:
    """Write a document the way an agent would: no fingerprint, no metadata."""

    document = json.loads(json.dumps(document))
    document.pop("fingerprint", None)
    path = tmp_path / "topology.json"
    path.write_text(json.dumps(document, indent=2), encoding="utf-8")
    return path


def _buck_document() -> dict:
    return ngspice_buck_topology_descriptor(_plant()).to_dict()


# ---------------------------------------------------------------------------
# describe-devices
# ---------------------------------------------------------------------------


def test_describe_devices_prints_the_closed_vocabulary(capsys):
    assert cli_main(["describe-devices"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["schema"] == CIRCUIT_DEVICE_SCHEMA
    assert sorted(payload["renderable_kinds"]) == sorted(RENDERABLE_DEVICE_KINDS)
    assert payload["unknown_kinds"] == "rejected"
    assert [entry["kind"] for entry in payload["device_kinds"]] == sorted(
        entry["kind"] for entry in payload["device_kinds"]
    )


def test_describe_devices_answers_for_one_kind(capsys):
    assert cli_main(["describe-devices", "voltage_controlled_switch"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["kind"] == "voltage_controlled_switch"
    assert payload["renderable"] is True
    assert payload["ports"][2]["port_id"] == "control"
    assert payload["ports"][2]["net_kinds"] == ["control"]
    assert payload["parameter_schema"]["additionalProperties"] is False


def test_describe_devices_shows_the_non_renderable_boundary(capsys):
    assert cli_main(["describe-devices", "averaged_switch"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["renderable"] is False
    assert payload["spicetype"] is None
    assert "no element card" in payload["description"]


def test_describe_devices_rejects_an_unknown_kind(capsys):
    assert cli_main(["describe-devices", "nope"]) == 2
    payload = json.loads(capsys.readouterr().out)

    assert payload["status"] == "CONFIG_REJECTED"
    assert payload["error_code"] == "invalid_request"
    assert payload["issues"][0]["code"] == "unsupported_device_kind"
    assert payload["issues"][0]["path"] == "/kind"
    # ``KeyError`` must not leak its repr quoting into a prose message.
    assert "'nope'" in payload["error"]
    assert not payload["error"].startswith('"')


# ---------------------------------------------------------------------------
# validate-topology
# ---------------------------------------------------------------------------


def test_validate_topology_accepts_a_document_without_a_fingerprint(tmp_path, capsys):
    path = _write(tmp_path, _buck_document())

    assert cli_main(["validate-topology", str(path)]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["schema"] == TOPOLOGY_VALIDATION_SCHEMA
    assert payload["ok"] is True
    assert payload["declared_fingerprint"] is None
    assert len(payload["computed_fingerprint"]) == 64
    assert payload["issues"] == []


def test_validate_topology_reports_a_defect_with_its_pointer(tmp_path, capsys):
    document = _buck_document()
    document["components"][4]["parameters"]["indutance_h"] = 1e-4
    path = _write(tmp_path, document)

    assert cli_main(["validate-topology", str(path)]) == 2
    payload = json.loads(capsys.readouterr().out)

    assert payload["ok"] is False
    assert payload["error_count"] == 1
    issue = payload["issues"][0]
    assert issue["code"] == "unknown_parameter"
    assert issue["path"] == "/components/4/parameters/indutance_h"
    assert issue["suggestion"] == "inductance_h"


def test_validate_topology_names_the_file_it_could_not_read(tmp_path, capsys):
    path = tmp_path / "missing.json"

    assert cli_main(["validate-topology", str(path)]) == 2
    payload = json.loads(capsys.readouterr().out)

    assert payload["error_code"] == "invalid_request"
    assert payload["reason"] == "invalid_document"
    assert str(path) in payload["error"]


def test_validate_topology_creates_nothing(tmp_path, capsys):
    path = _write(tmp_path, _buck_document())

    assert cli_main(["validate-topology", str(path)]) == 0
    capsys.readouterr()

    assert sorted(item.name for item in tmp_path.iterdir()) == ["topology.json"]


# ---------------------------------------------------------------------------
# compile-topology
# ---------------------------------------------------------------------------


def test_compile_topology_prints_a_netlist(tmp_path, capsys):
    path = _write(tmp_path, _buck_document())

    assert cli_main(["compile-topology", str(path), "--duty", "0.5"]) == 0
    text = capsys.readouterr().out

    assert text.startswith("* pe-sim compiled circuit")
    assert "template: circuit-netlist-v1" in text
    assert text.rstrip().endswith(".end")


def test_compile_topology_json_carries_a_digest(tmp_path, capsys):
    path = _write(tmp_path, _buck_document())

    assert cli_main(["compile-topology", str(path), "--json"]) == 0
    netlist_preview = capsys.readouterr().out
    payload = json.loads(netlist_preview)

    assert payload["topology_id"] == "buck"
    assert len(payload["digest"]) == 64
    assert payload["duty"] == 0.5
    assert payload["netlist"].startswith("* pe-sim compiled circuit")


def test_compile_topology_writes_only_where_it_is_told(tmp_path, capsys):
    path = _write(tmp_path, _buck_document())
    destination = tmp_path / "out" / "circuit.cir"

    assert cli_main(["compile-topology", str(path), "--output", str(destination)]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["output"] == str(destination)
    assert destination.is_file()
    assert destination.read_text(encoding="utf-8").rstrip().endswith(".end")


def test_compile_topology_refuses_a_document_that_does_not_validate(tmp_path, capsys):
    document = _buck_document()
    document["components"][4]["parameters"].pop("inductance_h")
    path = _write(tmp_path, document)

    assert cli_main(["compile-topology", str(path)]) == 2
    payload = json.loads(capsys.readouterr().out)

    assert payload["status"] == "CONFIG_REJECTED"
    assert payload["validation"] == TOPOLOGY_VALIDATION_SCHEMA
    assert payload["issues"][0]["code"] == "missing_parameter"


def test_compile_topology_refuses_an_averaged_descriptor(tmp_path, capsys):
    path = _write(tmp_path, buck_topology_descriptor(_plant()).to_dict())

    assert cli_main(["compile-topology", str(path)]) == 2
    payload = json.loads(capsys.readouterr().out)

    assert payload["compile_code"] == "not_compilable"
    assert payload["details"]["kind"] == "averaged_switch"


def test_compile_topology_refuses_an_impossible_duty(tmp_path, capsys):
    path = _write(tmp_path, _buck_document())

    assert cli_main(["compile-topology", str(path), "--duty", "1.5"]) == 2
    payload = json.loads(capsys.readouterr().out)

    assert payload["compile_code"] == "invalid_duty"


def test_compile_topology_accepts_a_carrier_phase(tmp_path, capsys):
    path = _write(tmp_path, _buck_document())

    assert (
        cli_main(
            [
                "compile-topology",
                str(path),
                "--duty",
                "0.5",
                "--duration",
                "3e-6",
                "--carrier-phase",
                "3e-6",
                "--json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)

    assert payload["carrier_phase_s"] == pytest.approx(3e-6)
    assert payload["carrier_tail_s"] == pytest.approx(2e-6)
    assert "PWL(" in payload["netlist"]


def test_the_topology_surface_needs_no_registered_plant(tmp_path, capsys):
    """Compilation is a circuit question, so an empty registry must not matter."""

    path = _write(tmp_path, _buck_document())

    assert cli_main(["compile-topology", str(path)]) == 0
    capsys.readouterr()
    assert not (tmp_path / "runs").exists()


def test_the_topology_surface_works_with_the_shipped_registrations_loaded(
    tmp_path, capsys
):
    """Loading the composition root must not change these answers."""

    load_builtin_plants()
    path = _write(tmp_path, _buck_document())

    assert cli_main(["validate-topology", str(path)]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True


# ---------------------------------------------------------------------------
# import-spice
# ---------------------------------------------------------------------------


NETLIST = """* buck from a netlist
V1 vin 0 DC 12
Vpwm pwm 0 PULSE(0 1 0 1e-9 1e-9 5e-06 1e-05)
S1 vin sw pwm 0 SWMOD
D1 0 sw DMOD
L1 sw vout 100u
C1 vout 0 470u
R1 vout 0 10
.model SWMOD SW(Ron=1m Roff=1g Vt=0.5 Vh=0)
.model DMOD D(Is=1e-12 N=0.02)
.tran 1u 20m
.end
"""


def _netlist_file(tmp_path: Path, text: str = NETLIST) -> Path:
    path = tmp_path / "circuit.cir"
    path.write_text(text, encoding="utf-8")
    return path


def test_import_spice_writes_a_document_that_then_compiles(tmp_path, capsys):
    source = _netlist_file(tmp_path)
    destination = tmp_path / "out" / "topology.json"

    assert (
        cli_main(
            [
                "import-spice",
                str(source),
                "--topology-id",
                "from_netlist",
                "--output",
                str(destination),
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)

    assert payload["ok"] is True
    assert payload["topology_id"] == "from_netlist"
    assert payload["output"] == str(destination)
    assert destination.is_file()
    written = json.loads(destination.read_text(encoding="utf-8"))
    assert written["topology_id"] == "from_netlist"
    assert "fingerprint" not in written

    assert cli_main(["compile-topology", str(destination), "--json"]) == 0
    compiled = json.loads(capsys.readouterr().out)
    assert compiled["topology_id"] == "from_netlist"
    assert len(compiled["digest"]) == 64


def test_import_spice_refuses_a_netlist_it_cannot_express(tmp_path, capsys):
    source = _netlist_file(
        tmp_path, "* include\n.include other.cir\nV1 vin 0 DC 12\nR1 vin 0 10\n"
    )
    destination = tmp_path / "never.json"

    assert cli_main(["import-spice", str(source), "--output", str(destination)]) == 2
    payload = json.loads(capsys.readouterr().out)

    assert payload["ok"] is False
    assert payload["import_ok"] is False
    assert payload["descriptor"] is None
    assert payload["issues"][0]["code"] == "unsupported_directive"
    assert not destination.exists()


def test_import_spice_names_the_file_it_could_not_read(tmp_path, capsys):
    missing = tmp_path / "absent.cir"

    assert cli_main(["import-spice", str(missing)]) == 2
    payload = json.loads(capsys.readouterr().out)

    assert payload["status"] == "CONFIG_REJECTED"
    assert payload["error_code"] == "invalid_request"
    assert str(missing) in payload["error"]


def test_import_spice_creates_nothing_it_was_not_asked_for(tmp_path, capsys):
    source = _netlist_file(tmp_path)

    assert cli_main(["import-spice", str(source)]) == 0
    capsys.readouterr()

    assert not (tmp_path / "runs").exists()
    assert sorted(path.name for path in tmp_path.iterdir()) == ["circuit.cir"]

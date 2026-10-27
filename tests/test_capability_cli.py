"""Acceptance tests for the user-facing capability-analysis entry point.

The entry point is deliberately not a study kind: a capability analysis names
one plant and one operating point and asks a frequency- or loss-domain
question.  These tests exercise the document parser, the three analysis kinds,
the written report, and the CLI rejection envelope.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pe_sim.capability_analysis import (
    CAPABILITY_ANALYSIS_SCHEMA,
    CapabilityAnalysisError,
    parse_capability_document,
    run_capability_analysis,
)
from pe_sim.cli import main
from pe_sim.impedance_analysis import impedance_response
from pe_sim.model_capabilities import (
    OperatingPoint,
    load_builtin_model_capabilities,
    small_signal_model_for,
)
from pe_sim.reference_plants import BuckPlant


_PLANT = {
    "id": "buck",
    "config": {
        "input_voltage_v": 48.0,
        "inductance_h": 470e-6,
        "capacitance_f": 470e-6,
        "load_resistance_ohm": 10.0,
    },
}
_OPERATING_POINT = {
    "duty": 0.5,
    "input_voltage_v": 48.0,
    "load_resistance_ohm": 10.0,
}
_SWEEP = {"start_hz": 10.0, "stop_hz": 100000.0, "points": 241}


def _ac_document() -> dict:
    return {
        "schema": CAPABILITY_ANALYSIS_SCHEMA,
        "analysis_id": "buck-ac",
        "kind": "ac",
        "plant": _PLANT,
        "operating_point": _OPERATING_POINT,
        "sweep": _SWEEP,
        "controller": {
            "id": "type2",
            "config": {"gain": 3.0, "zero_hz": 500.0, "pole_hz": 50000.0},
            "control_period_s": 1e-4,
        },
    }


def _impedance_document() -> dict:
    load_builtin_model_capabilities()
    plant = BuckPlant(**(_PLANT["config"]))
    model = small_signal_model_for(
        plant,
        OperatingPoint(duty=0.5, input_voltage_v=48.0, load_resistance_ohm=10.0),
    )
    external = impedance_response(
        model,
        kind="output",
        start_hz=_SWEEP["start_hz"],
        stop_hz=_SWEEP["stop_hz"],
        points=_SWEEP["points"],
    )
    return {
        "schema": CAPABILITY_ANALYSIS_SCHEMA,
        "analysis_id": "buck-zin",
        "kind": "impedance",
        "plant": _PLANT,
        "operating_point": _OPERATING_POINT,
        "sweep": _SWEEP,
        "impedance": {"kind": "input", "external": external},
    }


def _loss_document() -> dict:
    return {
        "schema": CAPABILITY_ANALYSIS_SCHEMA,
        "analysis_id": "lossy-buck-loss",
        "kind": "loss",
        "plant": {
            "id": "lossy-buck",
            "config": {
                "input_voltage_v": 48.0,
                "inductance_h": 470e-6,
                "capacitance_f": 470e-6,
                "load_resistance_ohm": 10.0,
                "switch_resistance_ohm": 0.05,
                "inductor_resistance_ohm": 0.02,
                "diode_forward_voltage_v": 0.4,
                "switching_frequency_hz": 200000.0,
                "switch_output_charge_c": 20e-9,
                "gate_charge_c": 10e-9,
                "gate_drive_voltage_v": 10.0,
            },
        },
        "operating_point": _OPERATING_POINT,
    }


def _write(tmp_path: Path, document: dict) -> Path:
    path = tmp_path / "capability.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def test_ac_report_contains_frequency_loop_and_margins():
    report = run_capability_analysis(parse_capability_document(_ac_document()))

    assert report["schema"] == "pe-sim.capability-report.v1"
    assert report["kind"] == "ac"
    assert report["status"] == "available"
    assert report["plant"]["class"] == "BuckPlant"
    assert len(report["input_sha256"]) == 64
    assert set(report["results"]) == {
        "frequency_response",
        "loop_gain",
        "stability_margins",
    }
    margins = report["results"]["stability_margins"]
    assert margins["gain_crossover_hz"] == pytest.approx(23.048965488686484)
    assert margins["phase_margin_deg"] == pytest.approx(92.22143187622035)
    assert margins["margin_positive"] is True
    # The report must be a finite JSON artifact.
    json.dumps(report, allow_nan=False)


def test_impedance_report_contains_middlebrook_screen():
    report = run_capability_analysis(
        parse_capability_document(_impedance_document())
    )

    assert set(report["results"]) == {"impedance", "middlebrook"}
    impedance = report["results"]["impedance"]
    assert impedance["kind"] == "input_impedance"
    assert impedance["provenance"]["plant_id"] == "buck"
    middlebrook = report["results"]["middlebrook"]
    assert middlebrook["criterion"] in {"satisfied", "violated", "not_evaluable"}
    assert "necessary screen" in middlebrook["criterion_meaning"]
    json.dumps(report, allow_nan=False)


def test_loss_report_uses_the_declared_lossy_buck():
    report = run_capability_analysis(parse_capability_document(_loss_document()))

    breakdown = report["results"]["loss_model"]
    assert breakdown["efficiency"] == pytest.approx(0.9835572732296164)
    assert breakdown["provenance"]["capability_origin"] == "caller_registered"
    assert breakdown["provenance"]["linearization_point"]["duty"] == 0.5
    assert sum(breakdown["terms"].values()) == pytest.approx(
        breakdown["total_loss_w"]
    )


def test_capability_cli_writes_report_and_prints_it(tmp_path, capsys):
    document = _ac_document()
    document["output"] = {"directory": str(tmp_path / "out")}
    config = _write(tmp_path, document)

    exit_code = main(["capability", str(config)])

    assert exit_code == 0
    printed = json.loads(capsys.readouterr().out)
    report_path = Path(printed["report_path"])
    assert report_path.is_file()
    written = json.loads(report_path.read_text(encoding="utf-8"))
    assert written["analysis_id"] == "buck-ac"
    assert written["results"]["stability_margins"]["margin_positive"] is True


def test_unknown_plant_is_a_branchable_rejection(tmp_path, capsys):
    document = _ac_document()
    document["plant"] = {"id": "not-a-plant", "config": {}}
    config = _write(tmp_path, document)

    exit_code = main(["capability", str(config)])

    assert exit_code == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["error_code"] == "unknown_plant"
    assert payload["reason"] == "unknown_plant"


def test_ideal_buck_declares_no_loss_provider(tmp_path, capsys):
    document = {
        "schema": CAPABILITY_ANALYSIS_SCHEMA,
        "analysis_id": "ideal-loss",
        "kind": "loss",
        "plant": _PLANT,
        "operating_point": _OPERATING_POINT,
    }
    config = _write(tmp_path, document)

    exit_code = main(["capability", str(config)])

    assert exit_code == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["error_code"] == "invalid_request"
    assert payload["reason"] == "loss_model_not_declared"


def test_kind_specific_blocks_are_rejected():
    document = _ac_document()
    document["impedance"] = {"kind": "input"}
    with pytest.raises(CapabilityAnalysisError, match="must not declare"):
        parse_capability_document(document)


def test_controller_is_optional_for_ac():
    document = _ac_document()
    document.pop("controller")
    report = run_capability_analysis(parse_capability_document(document))
    assert set(report["results"]) == {"frequency_response"}


def test_loss_kind_rejects_a_sweep():
    document = _loss_document()
    document["sweep"] = _SWEEP
    with pytest.raises(CapabilityAnalysisError, match="must not declare a sweep"):
        parse_capability_document(document)

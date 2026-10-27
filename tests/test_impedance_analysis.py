"""Input/output impedance and the Middlebrook ratio.

Every numeric anchor is derived independently of the implementation:

* buck ``Zin(0) = R / D^2`` and boost ``Zin(0) = R (1 - D)^2`` follow from the
  ideal CCM average model;
* buck ``Zout(s) = sL || R || 1/(sC)`` because a fixed duty makes the inductor
  an AC short to an ideal source;
* the ratio cases are built from explicit magnitudes, so the criterion is
  checked against arithmetic rather than against the code under test.
"""

from __future__ import annotations

import cmath
import json
import math

import pytest

from pe_sim.impedance_analysis import (
    IMPEDANCE_SCHEMA,
    impedance_response,
    middlebrook_ratio,
)
from pe_sim.model_capabilities import (
    OperatingPoint,
    clear_model_capabilities,
    load_builtin_model_capabilities,
    small_signal_model_for,
)
from pe_sim.reference_plants import BoostPlant, BuckPlant
from pe_sim.time_model import RUNTIME_EVIDENCE_LEVELS

VIN = 12.0
INDUCTANCE = 1e-4
CAPACITANCE = 4.7e-4
RESISTANCE = 10.0


@pytest.fixture(autouse=True)
def _registry():
    clear_model_capabilities()
    load_builtin_model_capabilities()
    yield
    clear_model_capabilities()


def _buck(duty: float = 0.5) -> BuckPlant:
    plant = BuckPlant(
        input_voltage_v=VIN,
        inductance_h=INDUCTANCE,
        capacitance_f=CAPACITANCE,
        load_resistance_ohm=RESISTANCE,
        integration_step_s=1e-6,
    )
    plant.last_duty = duty
    return plant


def _boost(duty: float = 0.5) -> BoostPlant:
    plant = BoostPlant(
        input_voltage_v=VIN,
        inductance_h=INDUCTANCE,
        capacitance_f=CAPACITANCE,
        load_resistance_ohm=RESISTANCE,
        integration_step_s=1e-6,
    )
    plant.last_duty = duty
    return plant


def _buck_model(duty: float = 0.5):
    return small_signal_model_for(_buck(duty), OperatingPoint(duty=duty))


def _boost_model(duty: float = 0.5):
    return small_signal_model_for(_boost(duty), OperatingPoint(duty=duty))


def _buck_output_impedance(frequency_hz: float) -> complex:
    s = 1j * 2.0 * math.pi * frequency_hz
    admittance = 1.0 / (s * INDUCTANCE) + 1.0 / RESISTANCE + s * CAPACITANCE
    return 1.0 / admittance


def test_buck_input_impedance_matches_analytic_dc() -> None:
    model = _buck_model(0.5)
    admittance = model.transfer_function_for(0.0, output="input_current", input="vin")
    impedance = 1.0 / admittance
    assert impedance.real == pytest.approx(RESISTANCE / 0.5**2, rel=1e-12)
    assert abs(impedance.imag) < 1e-9 * abs(impedance.real)


def test_boost_input_impedance_matches_analytic_dc() -> None:
    model = _boost_model(0.5)
    admittance = model.transfer_function_for(0.0, output="input_current", input="vin")
    impedance = 1.0 / admittance
    expected = RESISTANCE * (1.0 - 0.5) ** 2
    assert impedance.real == pytest.approx(expected, rel=1e-12)


def test_input_impedance_sweep_approaches_the_dc_value() -> None:
    model = _buck_model(0.5)
    response = impedance_response(
        model, kind="input", start_hz=1.0, stop_hz=1e5, points=8
    )
    assert response["magnitude_ohm"][0] == pytest.approx(
        RESISTANCE / 0.25, rel=5e-3
    )
    assert response["kind"] == "input_impedance"


@pytest.mark.parametrize("frequency", [1.0, 50.0, 300.0, 3000.0, 50000.0])
def test_buck_output_impedance_matches_parallel_expression(frequency: float) -> None:
    model = _buck_model(0.5)
    value = model.transfer_function_for(
        frequency, output="vout", input="load_current"
    )
    expected = _buck_output_impedance(frequency)
    assert value == pytest.approx(expected, rel=1e-9)


def test_output_impedance_tends_to_zero_at_dc() -> None:
    model = _buck_model(0.5)
    value = model.transfer_function_for(0.0, output="vout", input="load_current")
    assert abs(value) < 1e-6


def test_output_impedance_peaks_at_the_load_resistance_at_resonance() -> None:
    """At ``1/(2 pi sqrt(LC))`` the L and C admittances cancel, leaving R."""

    model = _buck_model(0.5)
    f0 = 1.0 / (2.0 * math.pi * math.sqrt(INDUCTANCE * CAPACITANCE))
    value = model.transfer_function_for(f0, output="vout", input="load_current")
    assert value.real == pytest.approx(RESISTANCE, rel=1e-12)
    assert abs(value.imag) < 1e-9 * RESISTANCE


def test_transfer_matrix_agrees_with_the_legacy_single_port_view() -> None:
    model = _buck_model(0.5)
    for frequency in (0.0, 123.0, 5e3):
        matrix = model.transfer_matrix(frequency)
        assert matrix["vout"]["duty"] == model.transfer_function(frequency)


def test_transfer_function_for_unknown_port_lists_the_declared_names() -> None:
    model = _buck_model(0.5)
    with pytest.raises(KeyError) as excinfo:
        model.transfer_function_for(1.0, output="nope", input="duty")
    assert "vout" in str(excinfo.value)
    with pytest.raises(KeyError) as excinfo:
        model.transfer_function_for(1.0, output="vout", input="nope")
    assert "load_current" in str(excinfo.value)


def test_conflicting_feedthrough_declarations_are_rejected() -> None:
    model = _buck_model(0.5)
    with pytest.raises(ValueError, match="feedthrough"):
        type(model)(
            plant_id=model.plant_id,
            method=model.method,
            state_space=model.state_space,
            output_matrix=model.output_matrix,
            feedthrough=0.0,
            feedthrough_matrix=((1.0, 0.0, 0.0), (0.0, 0.0, 0.0)),
            input_names=model.input_names,
            output_names=model.output_names,
            operating_point=model.operating_point,
            parameters=model.parameters,
            assumptions=model.assumptions,
            verified_by=model.verified_by,
        )


def test_port_name_validation_rejects_bad_declarations() -> None:
    model = _buck_model(0.5)
    common = dict(
        plant_id=model.plant_id,
        method=model.method,
        state_space=model.state_space,
        output_matrix=model.output_matrix,
        feedthrough=0.0,
        output_names=model.output_names,
        operating_point=model.operating_point,
        parameters=model.parameters,
        assumptions=model.assumptions,
        verified_by=model.verified_by,
    )
    with pytest.raises(ValueError, match="exactly 3"):
        type(model)(**common, input_names=("duty", "vin"))
    with pytest.raises(ValueError, match="unique"):
        type(model)(
            **common, input_names=("duty", "vin", "vin")
        )
    with pytest.raises(ValueError, match="snake_case"):
        type(model)(
            **common, input_names=("duty", "Vin", "load_current")
        )


def test_impedance_kind_is_validated() -> None:
    model = _buck_model(0.5)
    with pytest.raises(ValueError, match="kind"):
        impedance_response(model, kind="transfer", start_hz=1.0, stop_hz=10.0, points=4)


def test_impedance_phase_is_continuous() -> None:
    response = impedance_response(
        _buck_model(0.5), kind="output", start_hz=1.0, stop_hz=1e6, points=64
    )
    phases = response["phase_deg"]
    assert max(
        abs(phases[index + 1] - phases[index]) for index in range(len(phases) - 1)
    ) < 180.0


def _synthetic_impedance(magnitudes, *, frequencies=None, kind="input_impedance"):
    frequencies = frequencies or [10.0 * (index + 1) for index in range(len(magnitudes))]
    return {
        "schema": IMPEDANCE_SCHEMA,
        "status": "available",
        "kind": kind,
        "frequencies_hz": frequencies,
        "magnitude_ohm": list(magnitudes),
        "provenance": {
            "plant_id": "buck",
            "linearization_point": {"duty": 0.5, "vout": 6.0, "il": 0.6},
            "method": "analytic",
            "capability_origin": "builtin",
            "assumptions": ["ccm_average_model"],
            "plant_verified_by": "tests/test_model_capabilities.py::test_x",
            "controller": None,
            "sweep": {
                "start_hz": 10.0,
                "stop_hz": 10.0 * len(magnitudes),
                "points": len(magnitudes),
                "spacing": "log10",
            },
            "verified_by": ["tests/test_impedance_analysis.py::test_x"],
        },
    }


def test_middlebrook_ratio_must_share_the_frequency_axis() -> None:
    plant = _synthetic_impedance([2.0, 3.0])
    external = _synthetic_impedance(
        [1.0, 1.0], frequencies=[10.0, 20.0]
    )
    external["frequencies_hz"] = [10.0, 21.0]
    with pytest.raises(ValueError, match="same frequency axis"):
        middlebrook_ratio(plant, external, kind="input")


def test_middlebrook_ratio_satisfied_and_violated() -> None:
    plant = _synthetic_impedance([2.0, 4.0])
    external = _synthetic_impedance([1.0, 1.0])
    satisfied = middlebrook_ratio(plant, external, kind="input")
    assert satisfied["criterion"] == "satisfied"
    assert satisfied["violation_count"] == 0
    assert satisfied["min_ratio_db"] == pytest.approx(20.0 * math.log10(2.0))

    weak = _synthetic_impedance([0.5, 4.0])
    violated = middlebrook_ratio(weak, external, kind="input")
    assert violated["criterion"] == "violated"
    assert violated["violation_count"] == 1
    assert violated["first_violation_hz"] == pytest.approx(10.0)
    assert violated["min_ratio_db"] == pytest.approx(20.0 * math.log10(0.5))


def test_middlebrook_ratio_is_not_evaluable_when_a_magnitude_is_zero() -> None:
    plant = _synthetic_impedance([2.0, 0.0])
    external = _synthetic_impedance([1.0, 1.0])
    result = middlebrook_ratio(plant, external, kind="input")
    assert result["criterion"] == "not_evaluable"
    assert "non_finite_ratio" in result["notes"]


def test_middlebrook_output_direction_inverts_the_ratio() -> None:
    load = _synthetic_impedance([4.0, 4.0])
    output = _synthetic_impedance([1.0, 2.0], kind="output_impedance")
    result = middlebrook_ratio(output, load, kind="output")
    assert result["criterion"] == "satisfied"
    assert result["min_ratio_db"] == pytest.approx(20.0 * math.log10(2.0))


def test_middlebrook_never_claims_stability() -> None:
    result = middlebrook_ratio(
        _synthetic_impedance([2.0]), _synthetic_impedance([1.0]), kind="input"
    )
    assert "stable" not in json.dumps(result).lower()
    assert "necessary screen" in result["criterion_meaning"]
    for forbidden in ("stable", "unstable", "safe"):
        assert forbidden not in result["criterion_meaning"]


def test_impedance_provenance_is_self_contained() -> None:
    response = impedance_response(
        _buck_model(0.5), kind="input", start_hz=1.0, stop_hz=1e5, points=8
    )
    provenance = response["provenance"]
    assert set(provenance) == {
        "plant_id",
        "linearization_point",
        "method",
        "capability_origin",
        "assumptions",
        "plant_verified_by",
        "controller",
        "sweep",
        "verified_by",
    }
    assert provenance["controller"] is None
    assert provenance["sweep"]["points"] == 8
    assert provenance["verified_by"] == [
        "tests/test_impedance_analysis.py::"
        "test_buck_input_impedance_matches_analytic_dc"
    ]
    assert provenance["plant_verified_by"].startswith("tests/test_model_capabilities.py::")


def test_impedance_payloads_carry_no_evidence_vocabulary() -> None:
    payloads = [
        impedance_response(_buck_model(0.5), kind=k, start_hz=1.0, stop_hz=1e4, points=8)
        for k in ("input", "output")
    ]
    payloads.append(
        middlebrook_ratio(payloads[0], payloads[0], kind="input")
    )
    for payload in payloads:
        text = json.dumps(payload, allow_nan=False)
        assert "evidence_level" not in text
        for level in RUNTIME_EVIDENCE_LEVELS:
            assert f'"{level}"' not in text

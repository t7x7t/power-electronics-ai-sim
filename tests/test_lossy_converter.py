"""Contract tests for the declared-loss averaged CCM buck model.

The model is deliberately narrow: it declares conduction and switching loss
terms for an averaged converter and reports one analytic operating point.  It
is not a device-level or thermal model, and the tests below pin that boundary
as much as they pin the arithmetic.
"""

from __future__ import annotations

import json

import pytest

from pe_sim.contracts import PlantObservation
from pe_sim.lossy_converter import LossyBuckPlant, register_loss_provider
from pe_sim.model_capabilities import (
    CapabilityUnavailable,
    LossBreakdown,
    OperatingPoint,
    clear_model_capabilities,
    loss_model_for,
    registered_model_capabilities,
)

_BASE = dict(
    input_voltage_v=48.0,
    # 470 uH keeps the 200 ohm light-load point in CCM at 200 kHz:
    # delta_iL ~= 0.129 A < 2 * I_L ~= 0.238 A.
    inductance_h=470e-6,
    capacitance_f=470e-6,
    load_resistance_ohm=10.0,
    integration_step_s=1e-6,
    switch_resistance_ohm=0.05,
    inductor_resistance_ohm=0.02,
    diode_forward_voltage_v=0.4,
    switching_frequency_hz=200e3,
    switch_output_charge_c=20e-9,
    gate_charge_c=10e-9,
    gate_drive_voltage_v=10.0,
)

_TERM_KEYS = {
    "conduction_switch_w",
    "conduction_inductor_w",
    "conduction_diode_w",
    "switching_qoss_w",
    "gate_drive_w",
}


@pytest.fixture(autouse=True)
def _isolated_capability_registry():
    clear_model_capabilities()
    yield
    clear_model_capabilities()


def _plant(**overrides: float) -> LossyBuckPlant:
    parameters = dict(_BASE)
    parameters.update(overrides)
    return LossyBuckPlant(**parameters)


def _lossless_plant() -> LossyBuckPlant:
    return _plant(
        switch_resistance_ohm=0.0,
        inductor_resistance_ohm=0.0,
        diode_forward_voltage_v=0.0,
        switch_output_charge_c=0.0,
        gate_charge_c=0.0,
        gate_drive_voltage_v=0.0,
    )


def _operating_point(duty: float = 0.5) -> OperatingPoint:
    return OperatingPoint(duty=duty)


def _efficiency(load_resistance_ohm: float) -> float:
    return _plant(load_resistance_ohm=load_resistance_ohm).loss_model(
        _operating_point()
    ).efficiency


def _conduction_share(load_resistance_ohm: float) -> float:
    breakdown = _plant(load_resistance_ohm=load_resistance_ohm).loss_model(
        _operating_point()
    )
    conduction = (
        breakdown.terms["conduction_switch_w"]
        + breakdown.terms["conduction_inductor_w"]
        + breakdown.terms["conduction_diode_w"]
    )
    return conduction / breakdown.total_loss_w


# ---------------------------------------------------------------------------
# Lossless declaration and the value-object guard
# ---------------------------------------------------------------------------


def test_zero_loss_parameters_declare_lossless() -> None:
    breakdown = _lossless_plant().loss_model(_operating_point())

    assert breakdown.declared_lossless is True
    assert breakdown.total_loss_w == 0.0
    assert breakdown.efficiency == 1.0
    assert set(breakdown.terms) == _TERM_KEYS
    assert all(value == 0.0 for value in breakdown.terms.values())


def test_inferred_lossless_breakdown_is_rejected() -> None:
    with pytest.raises(ValueError, match="declared_lossless"):
        LossBreakdown(
            plant_id="lossy-buck",
            method="analytic",
            terms={"conduction_switch_w": 0.0},
            total_loss_w=0.0,
            output_power_w=10.0,
            input_power_w=10.0,
            efficiency=1.0,
            assumptions=("unit_test_fixture",),
            verified_by="tests/test_lossy_converter.py::test_inferred_lossless_breakdown_is_rejected",
        )


# ---------------------------------------------------------------------------
# Loss arithmetic
# ---------------------------------------------------------------------------


def test_loss_terms_sum_to_the_power_balance() -> None:
    breakdown = _plant().loss_model(_operating_point())

    assert breakdown.declared_lossless is False
    assert breakdown.efficiency < 1.0
    # The provider emits the arithmetic identity exactly; the value object
    # only has to tolerate 1e-9 for third-party providers.
    assert breakdown.total_loss_w == sum(breakdown.terms.values())
    assert breakdown.input_power_w == (
        breakdown.output_power_w + breakdown.total_loss_w
    )
    assert breakdown.efficiency == (
        breakdown.output_power_w / breakdown.input_power_w
    )

    # The projection must be re-validatable by the same invariants.
    payload = breakdown.to_dict()
    provenance = payload["provenance"]
    replayed = LossBreakdown(
        plant_id=payload["plant_id"],
        method=payload["method"],
        terms=payload["terms"],
        total_loss_w=payload["total_loss_w"],
        output_power_w=payload["output_power_w"],
        input_power_w=payload["input_power_w"],
        efficiency=payload["efficiency"],
        assumptions=tuple(payload["assumptions"]),
        verified_by=payload["verified_by"],
        declared_lossless=payload["declared_lossless"],
        operating_point=provenance["linearization_point"],
        origin=provenance["capability_origin"],
        plant_verified_by=provenance["plant_verified_by"],
    )
    assert replayed.to_dict() == payload


def test_loss_model_declares_the_evaluated_operating_point() -> None:
    plant = _plant()
    breakdown = plant.loss_model(_operating_point())
    steady = plant.analytical_steady_state(0.5)
    provenance = breakdown.to_dict()["provenance"]

    assert breakdown.origin == "caller_registered"
    assert dict(breakdown.operating_point)["duty"] == pytest.approx(0.5)
    assert dict(breakdown.operating_point)["vout"] == pytest.approx(
        steady["output_voltage_v"]
    )
    assert dict(breakdown.operating_point)["il"] == pytest.approx(
        steady["inductor_current_a"]
    )
    assert provenance["capability_origin"] == "caller_registered"
    assert provenance["plant_verified_by"] == (
        "tests/test_lossy_converter.py::test_loss_terms_sum_to_the_power_balance"
    )
    assert provenance["linearization_point"] == dict(breakdown.operating_point)
    assert provenance["verified_by"] == [breakdown.verified_by]


def test_loss_model_term_keys_match_the_contract() -> None:
    breakdown = _plant().loss_model(_operating_point())

    assert set(breakdown.terms) == _TERM_KEYS


def test_higher_switch_resistance_lowers_efficiency() -> None:
    low = _plant(switch_resistance_ohm=0.05).loss_model(_operating_point())
    high = _plant(switch_resistance_ohm=0.10).loss_model(_operating_point())

    assert high.efficiency < low.efficiency


def test_light_load_raises_efficiency_until_conduction_dominates() -> None:
    light = _efficiency(200.0)
    medium = _efficiency(50.0)
    full = _efficiency(5.0)
    overload = _efficiency(0.5)

    # Fixed switching/gate losses dominate at light load; conduction losses
    # dominate at overload.  The efficiency is therefore not monotonic.
    assert light < medium < full
    assert overload < full
    assert _conduction_share(200.0) < _conduction_share(5.0)


# ---------------------------------------------------------------------------
# Analytical operating point and time-domain agreement
# ---------------------------------------------------------------------------


def test_analytical_steady_state_matches_the_equation_and_load_current() -> None:
    plant = _plant()
    steady = plant.analytical_steady_state(0.5)

    expected_voltage = (0.5 * 48.0 - 0.5 * 0.4) / (
        1.0 + (0.5 * 0.05 + 0.02) / 10.0
    )
    assert steady["output_voltage_v"] == pytest.approx(expected_voltage, rel=1e-12)
    assert steady["output_voltage_v"] > 0.0
    assert steady["inductor_current_a"] == pytest.approx(
        steady["output_voltage_v"] / 10.0, rel=1e-12
    )
    assert steady["inductor_current_a"] > 0.0


@pytest.mark.parametrize("duty", [0.0, -0.1, 1.1, float("nan"), float("inf")])
def test_analytical_steady_state_rejects_invalid_or_negative_solutions(
    duty: float,
) -> None:
    with pytest.raises(ValueError):
        _plant().analytical_steady_state(duty)


def test_advance_converges_to_the_analytical_steady_state() -> None:
    plant = _plant(capacitance_f=100e-6, integration_step_s=2e-6)
    plant.reset({})

    observation = plant.advance(0.5, 0.05)
    steady = plant.analytical_steady_state(0.5)

    assert observation.truth["vout"] == pytest.approx(
        steady["output_voltage_v"], rel=1e-3
    )
    assert observation.truth["inductor_current"] == pytest.approx(
        steady["inductor_current_a"], rel=1e-3
    )


def test_snapshot_restore_round_trip() -> None:
    plant = _plant()
    plant.reset({"inductor_current_a": 1.25, "output_voltage_v": 5.5})
    snapshot = plant.snapshot()

    clone = _plant()
    clone.restore(snapshot)

    assert clone.snapshot() == snapshot
    assert clone.observe().truth == plant.observe().truth


# ---------------------------------------------------------------------------
# Protocol and registration
# ---------------------------------------------------------------------------


def test_capabilities_backend_id_and_observation_shape() -> None:
    plant = _plant()

    assert plant.backend_id == "lossy-buck"
    assert plant.capabilities() == {
        "continuous_time",
        "external_input",
        "snapshot",
        "loss_model",
    }
    observation = plant.observe()
    assert isinstance(observation, PlantObservation)
    assert {
        "vout",
        "inductor_current",
        "duty",
        "input_voltage",
        "load_resistance",
    } <= set(observation.truth)


def test_register_loss_provider_is_idempotent() -> None:
    register_loss_provider()
    first = registered_model_capabilities()

    register_loss_provider()

    assert registered_model_capabilities() == first
    assert first["lossy-buck"] == ("loss_model",)


def test_loss_model_requires_registration() -> None:
    plant = _plant()

    with pytest.raises(CapabilityUnavailable) as caught:
        loss_model_for(plant, _operating_point())
    assert caught.value.reason == "loss_model_not_declared"

    register_loss_provider()
    breakdown = loss_model_for(plant, _operating_point())
    assert breakdown.plant_id == "lossy-buck"
    assert breakdown.efficiency < 1.0


def test_registered_provider_rejects_foreign_plant_types() -> None:
    class _ForeignPlant:
        backend_id = "lossy-buck"

    register_loss_provider()

    with pytest.raises(CapabilityUnavailable) as caught:
        loss_model_for(_ForeignPlant(), _operating_point())
    assert caught.value.reason == "loss_model_not_declared"


def test_manifest_identity_is_deterministic_and_parameter_sensitive() -> None:
    plant = _plant()

    first = plant.manifest_identity()
    second = plant.manifest_identity()

    assert first == second
    assert first["parameters"]["switch_resistance_ohm"] == 0.05
    assert first["implementation"]["review"]["status"] == "not_pinned"

    changed = _plant(switch_resistance_ohm=0.06).manifest_identity()
    assert changed["sha256"] != first["sha256"]


def test_payloads_are_json_serializable() -> None:
    plant = _plant()
    payloads = (
        plant.loss_model(_operating_point()).to_dict(),
        plant.manifest_identity(),
        plant.backend_provenance(),
        dict(plant.snapshot()),
        dict(plant.observe().truth),
    )

    for payload in payloads:
        json.dumps(payload, allow_nan=False)


# ---------------------------------------------------------------------------
# Parameter validation and the efficiency ceiling
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name,value",
    [
        ("switch_resistance_ohm", -1e-3),
        ("inductor_resistance_ohm", -1.0),
        ("diode_forward_voltage_v", -0.1),
        ("switch_output_charge_c", -1e-9),
        ("gate_charge_c", -1e-9),
        ("gate_drive_voltage_v", -1.0),
        ("switch_resistance_ohm", float("nan")),
        ("inductor_resistance_ohm", float("inf")),
        ("diode_forward_voltage_v", float("-inf")),
        ("switching_frequency_hz", 0.0),
        ("switching_frequency_hz", -1.0),
        ("switching_frequency_hz", float("inf")),
        ("switch_resistance_ohm", True),
    ],
)
def test_invalid_loss_parameters_are_rejected(name: str, value: float) -> None:
    with pytest.raises(ValueError):
        _plant(**{name: value})


def test_efficiency_never_exceeds_one_with_extreme_parameters() -> None:
    plant = _plant(
        load_resistance_ohm=0.01,
        switch_resistance_ohm=1e3,
        inductor_resistance_ohm=1e3,
        diode_forward_voltage_v=10.0,
        switch_output_charge_c=1e-6,
        gate_charge_c=1e-6,
        gate_drive_voltage_v=20.0,
        switching_frequency_hz=1e6,
    )
    breakdown = plant.loss_model(_operating_point())

    assert breakdown.input_power_w > 0.0
    assert 0.0 <= breakdown.efficiency < 1.0
    assert breakdown.total_loss_w > 0.0

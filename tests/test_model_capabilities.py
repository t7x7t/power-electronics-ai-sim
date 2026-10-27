"""Tests for the optional model-capability seam (docs/60 C4, first slice).

This module proves the registration/lookup rules and the analytic buck/boost
small-signal models.  AC sweeps and stability margins are exercised in
``tests/test_ac_analysis.py``; every capability result here only self-reports
``method`` / ``assumptions`` / ``verified_by`` and never an evidence level.
"""

from __future__ import annotations

import json
import math

import pytest

from pe_sim.backends.whole_run_engine import LinearStateSpace
from pe_sim.model_capabilities import (
    CAPABILITY_ORIGINS,
    CAPABILITY_SCHEMA,
    CAPABILITY_UNAVAILABLE_REASONS,
    CapabilityUnavailable,
    LossBreakdown,
    OperatingPoint,
    SmallSignalModel,
    capability_provenance,
    clear_model_capabilities,
    load_builtin_model_capabilities,
    loss_model_for,
    register_loss_model,
    register_small_signal_model,
    registered_model_capabilities,
    small_signal_model_for,
    validate_capability_provenance,
)
from pe_sim.reference_plants import BoostPlant, BuckPlant
from pe_sim.time_model import RUNTIME_EVIDENCE_LEVELS


@pytest.fixture(autouse=True)
def _isolated_registry():
    clear_model_capabilities()
    yield
    clear_model_capabilities()


def _buck(
    *,
    input_voltage_v: float = 12.0,
    inductance_h: float = 100e-6,
    capacitance_f: float = 470e-6,
    load_resistance_ohm: float = 10.0,
) -> BuckPlant:
    return BuckPlant(
        input_voltage_v=input_voltage_v,
        inductance_h=inductance_h,
        capacitance_f=capacitance_f,
        load_resistance_ohm=load_resistance_ohm,
        integration_step_s=1e-6,
    )


def _boost() -> BoostPlant:
    return BoostPlant(
        input_voltage_v=12.0,
        inductance_h=100e-6,
        capacitance_f=470e-6,
        load_resistance_ohm=10.0,
        integration_step_s=1e-6,
    )


def _operating_point(duty: float = 0.5, **fields: float) -> OperatingPoint:
    return OperatingPoint(duty=duty, **fields)


def _unit_model(
    plant_id: str,
    *,
    provider_marker: float = 1.0,
    origin: str = "caller_registered",
) -> SmallSignalModel:
    """A one-state model ``G(s) = 1/s`` used only to exercise the registry."""

    return SmallSignalModel(
        plant_id=plant_id,
        method="analytic",
        state_space=LinearStateSpace(((0.0,),), ((1.0,),)),
        output_matrix=((1.0,),),
        feedthrough=0.0,
        operating_point={"duty": 0.5, "vout": 1.0, "il": 0.5},
        parameters={"gain": 1.0, "provider_marker": provider_marker},
        assumptions=("unit_test_fixture",),
        verified_by="tests/test_model_capabilities.py::_unit_model",
        origin=origin,
    )


def _unit_loss(plant_id: str, *, origin: str = "caller_registered") -> LossBreakdown:
    return LossBreakdown(
        plant_id=plant_id,
        method="analytic",
        terms={"conduction_w": 0.5},
        total_loss_w=0.5,
        output_power_w=9.5,
        input_power_w=10.0,
        efficiency=0.95,
        assumptions=("unit_test_fixture",),
        verified_by="tests/test_model_capabilities.py::_unit_loss",
        origin=origin,
    )


def _factory(plant_id: str, *, provider_marker: float = 1.0):
    def factory(plant, operating_point):
        del plant, operating_point
        return _unit_model(plant_id, provider_marker=provider_marker)

    return factory


class _KeyedPlant:
    def __init__(self, backend_id: str) -> None:
        self.backend_id = backend_id


def test_unregistered_small_signal_model_fails_closed() -> None:
    with pytest.raises(CapabilityUnavailable) as caught:
        small_signal_model_for(_buck(), _operating_point())

    assert caught.value.reason == "small_signal_model_not_declared"
    assert str(caught.value)
    assert "BuckPlant" in str(caught.value)


def test_unregistered_loss_model_fails_closed() -> None:
    with pytest.raises(CapabilityUnavailable) as caught:
        loss_model_for(_buck(), _operating_point())

    assert caught.value.reason == "loss_model_not_declared"
    assert "BuckPlant" in str(caught.value)


def test_capability_unavailable_reason_vocabulary_is_closed() -> None:
    required = {
        "small_signal_model_not_declared",
        "loss_model_not_declared",
        "controller_transfer_function_not_declared",
        "model_plant_id_mismatch",
        "operating_point_not_declared",
        "small_signal_parameters_not_declared",
        "model_origin_not_permitted",
    }
    assert required <= CAPABILITY_UNAVAILABLE_REASONS

    with pytest.raises(ValueError, match="reason"):
        CapabilityUnavailable("not_a_declared_reason", "message")


def test_duplicate_registration_requires_replace() -> None:
    register_small_signal_model("fake-key", _factory("fake-key", provider_marker=1.0))
    plant = _KeyedPlant("fake-key")

    first = small_signal_model_for(plant, _operating_point())
    assert first.plant_id == "fake-key"
    assert first.parameters["provider_marker"] == 1.0
    with pytest.raises(ValueError):
        register_small_signal_model(
            "fake-key", _factory("fake-key", provider_marker=2.0)
        )

    register_small_signal_model(
        "fake-key", _factory("fake-key", provider_marker=2.0), replace=True
    )
    second = small_signal_model_for(plant, _operating_point())
    assert second.plant_id == "fake-key"
    assert second.parameters["provider_marker"] == 2.0


def test_factory_returning_wrong_small_signal_type_raises_type_error() -> None:
    register_small_signal_model("fake-key", lambda plant, operating_point: object())

    with pytest.raises(TypeError):
        small_signal_model_for(_KeyedPlant("fake-key"), _operating_point())


def test_factory_returning_wrong_loss_type_raises_type_error() -> None:
    register_loss_model("fake-loss", lambda plant, operating_point: {"efficiency": 1.0})

    with pytest.raises(TypeError):
        loss_model_for(_KeyedPlant("fake-loss"), _operating_point())


def test_builtin_buck_analytic_model_contract() -> None:
    load_builtin_model_capabilities()
    plant = _buck()
    duty = 0.5
    model = small_signal_model_for(plant, _operating_point(duty))

    assert model.plant_id == "buck"
    assert model.method == "analytic"
    assert {"ccm_average_model", "ideal_components_no_loss"}.issubset(model.assumptions)
    assert isinstance(model.state_space, LinearStateSpace)
    assert model.input_names == ("duty", "vin", "load_current")
    assert model.output_names == ("vout", "input_current")
    assert model.state_space.input_size == 3
    assert len(model.output_matrix) == 2
    assert all(len(row) == model.state_space.state_size for row in model.output_matrix)
    assert model.feedthrough == 0.0

    inductance = plant.inductance_h
    capacitance = plant.capacitance_f
    resistance = plant.load_resistance_ohm
    matrix_a = model.state_space.matrix_a
    assert matrix_a[0][0] == pytest.approx(0.0)
    assert matrix_a[0][1] == pytest.approx(-1.0 / inductance)
    assert matrix_a[1][0] == pytest.approx(1.0 / capacitance)
    assert matrix_a[1][1] == pytest.approx(-1.0 / (resistance * capacitance))
    assert model.state_space.input_matrix[0][0] == pytest.approx(
        plant.input_voltage_v / inductance
    )
    assert model.state_space.input_matrix[1][0] == pytest.approx(0.0)
    assert model.state_space.input_matrix[0][1] == pytest.approx(duty / inductance)
    assert model.state_space.input_matrix[0][2] == pytest.approx(0.0)
    assert model.state_space.input_matrix[1][2] == pytest.approx(1.0 / capacitance)
    assert model.output_matrix == ((0.0, 1.0), (duty, 0.0))

    assert model.operating_point["duty"] == pytest.approx(duty)
    assert model.operating_point["vout"] == pytest.approx(
        duty * plant.input_voltage_v
    )
    assert model.operating_point["il"] == pytest.approx(
        duty * plant.input_voltage_v / plant.load_resistance_ohm
    )
    assert model.parameters["inductance_h"] == pytest.approx(inductance)


def test_buck_small_signal_model_dc_gain_and_resonance() -> None:
    load_builtin_model_capabilities()
    plant = _buck()
    model = small_signal_model_for(plant, _operating_point())

    dc = model.transfer_function(0.0)
    assert dc.imag == pytest.approx(0.0, abs=1e-12)
    assert dc.real == pytest.approx(plant.input_voltage_v, rel=1e-9)

    resonant_hz = 1.0 / (
        2.0 * math.pi * math.sqrt(plant.inductance_h * plant.capacitance_f)
    )
    peak = abs(model.transfer_function(resonant_hz))
    assert peak > abs(dc)
    assert peak > abs(model.transfer_function(100.0 * resonant_hz))


def test_boost_small_signal_model_dc_gain() -> None:
    load_builtin_model_capabilities()
    plant = _boost()
    duty = 0.4
    model = small_signal_model_for(plant, _operating_point(duty))

    independent_dc = plant.input_voltage_v / (1.0 - duty) ** 2
    dc = model.transfer_function(0.0)
    assert dc.imag == pytest.approx(0.0, abs=1e-12)
    assert dc.real == pytest.approx(independent_dc, rel=1e-9)

    steady_state = plant.analytical_steady_state(duty)
    assert model.operating_point["vout"] == pytest.approx(
        steady_state["output_voltage_v"]
    )
    assert model.operating_point["il"] == pytest.approx(
        steady_state["inductor_current_a"]
    )
    assert model.method == "analytic"
    assert {"ccm_average_model", "ideal_components_no_loss"}.issubset(model.assumptions)


def test_boost_small_signal_matrices_match_linearized_average_equations() -> None:
    load_builtin_model_capabilities()
    plant = _boost()
    duty = 0.4
    model = small_signal_model_for(plant, _operating_point(duty))

    conduction = 1.0 - duty
    inductance = plant.inductance_h
    capacitance = plant.capacitance_f
    resistance = plant.load_resistance_ohm
    output_voltage = model.operating_point["vout"]
    inductor_current = model.operating_point["il"]

    matrix_a = model.state_space.matrix_a
    assert matrix_a[0][0] == pytest.approx(0.0)
    assert matrix_a[0][1] == pytest.approx(-conduction / inductance)
    assert matrix_a[1][0] == pytest.approx(conduction / capacitance)
    assert matrix_a[1][1] == pytest.approx(-1.0 / (resistance * capacitance))
    assert model.state_space.input_matrix[0][0] == pytest.approx(
        output_voltage / inductance
    )
    assert model.state_space.input_matrix[1][0] == pytest.approx(
        -inductor_current / capacitance
    )
    assert model.state_space.input_matrix[0][1] == pytest.approx(1.0 / inductance)
    assert model.state_space.input_matrix[1][2] == pytest.approx(1.0 / capacitance)
    assert model.output_matrix == ((0.0, 1.0), (1.0, 0.0))


@pytest.mark.parametrize("plant_factory", [_buck, _boost])
def test_builtin_loss_models_are_unavailable_not_efficiency_one(plant_factory) -> None:
    load_builtin_model_capabilities()

    with pytest.raises(CapabilityUnavailable) as caught:
        loss_model_for(plant_factory(), _operating_point(duty=0.4))

    assert caught.value.reason == "loss_model_not_declared"
    assert "loss" in str(caught.value).lower()


class _DuckBuckWithoutSteadyState:
    backend_id = "buck"
    input_voltage_v = 12.0
    inductance_h = 100e-6
    capacitance_f = 470e-6
    load_resistance_ohm = 10.0


def test_operating_point_fallback_when_plant_has_no_analytical_steady_state() -> None:
    load_builtin_model_capabilities()
    model = small_signal_model_for(
        _DuckBuckWithoutSteadyState(),
        OperatingPoint(duty=0.4, input_voltage_v=12.0, load_resistance_ohm=10.0),
    )

    assert model.operating_point["vout"] == pytest.approx(4.8)
    assert model.operating_point["il"] == pytest.approx(0.48)
    assert model.transfer_function(0.0).real == pytest.approx(12.0, rel=1e-9)


def test_operating_point_fallback_without_declared_voltage_or_load_fails_closed() -> None:
    load_builtin_model_capabilities()

    with pytest.raises(CapabilityUnavailable) as caught:
        small_signal_model_for(
            _DuckBuckWithoutSteadyState(), OperatingPoint(duty=0.4)
        )

    assert caught.value.reason == "operating_point_not_declared"


class _ClassNamedPlant:
    backend_id = "not-registered-anywhere"


class _NowherePlant:
    backend_id = "nowhere-backend"


def test_lookup_falls_back_from_backend_id_to_class_name() -> None:
    register_small_signal_model("_ClassNamedPlant", _factory("_ClassNamedPlant"))

    model = small_signal_model_for(_ClassNamedPlant(), _operating_point())
    assert model.plant_id == "_ClassNamedPlant"


def test_unregistered_message_names_both_lookup_keys() -> None:
    with pytest.raises(CapabilityUnavailable) as caught:
        small_signal_model_for(_NowherePlant(), _operating_point())

    message = str(caught.value)
    assert "nowhere-backend" in message
    assert "_NowherePlant" in message


def test_builtin_loader_does_not_overwrite_existing_registration() -> None:
    register_small_signal_model("buck", _factory("buck", provider_marker=7.0))

    load_builtin_model_capabilities()

    model = small_signal_model_for(_KeyedPlant("buck"), _operating_point())
    assert model.plant_id == "buck"
    assert model.parameters["provider_marker"] == 7.0


def test_registered_model_capabilities_is_idempotent_and_clearable() -> None:
    load_builtin_model_capabilities()
    first = registered_model_capabilities()
    load_builtin_model_capabilities()

    assert registered_model_capabilities() == first
    assert first["buck"] == ("small_signal_model",)
    assert first["boost"] == ("small_signal_model",)

    register_loss_model("fake-loss", lambda plant, operating_point: _unit_loss("loss"))
    capabilities = registered_model_capabilities()
    assert capabilities["fake-loss"] == ("loss_model",)

    clear_model_capabilities()
    assert registered_model_capabilities() == {}


def test_loss_model_for_returns_registered_provider() -> None:
    register_loss_model(
        "fake-loss", lambda plant, operating_point: _unit_loss("fake-loss")
    )
    breakdown = loss_model_for(_KeyedPlant("fake-loss"), _operating_point())

    assert breakdown.plant_id == "fake-loss"
    assert breakdown.efficiency == pytest.approx(0.95)


def test_to_dict_payloads_are_json_serializable() -> None:
    load_builtin_model_capabilities()
    model = small_signal_model_for(_buck(), _operating_point())
    operating_point = OperatingPoint(
        duty=0.5, input_voltage_v=12.0, load_resistance_ohm=10.0
    )
    breakdown = _unit_loss("buck")

    model_payload = model.to_dict()
    assert model_payload["schema"] == CAPABILITY_SCHEMA
    assert model_payload["plant_id"] == "buck"
    assert json.loads(json.dumps(model_payload, allow_nan=False)) == model_payload
    assert json.loads(
        json.dumps(operating_point.to_dict(), allow_nan=False)
    ) == operating_point.to_dict()
    loss_payload = breakdown.to_dict()
    assert loss_payload["schema"] == CAPABILITY_SCHEMA
    assert json.loads(json.dumps(loss_payload, allow_nan=False)) == loss_payload


@pytest.mark.parametrize("duty", [-0.1, 1.1, float("nan"), float("inf")])
def test_operating_point_rejects_invalid_duty(duty: float) -> None:
    with pytest.raises(ValueError):
        OperatingPoint(duty=duty)


def test_small_signal_model_rejects_non_single_row_output() -> None:
    with pytest.raises(ValueError):
        SmallSignalModel(
            plant_id="invalid",
            method="analytic",
            state_space=LinearStateSpace(((0.0,),), ((1.0,),)),
            output_matrix=((1.0,), (1.0,)),
            feedthrough=0.0,
            operating_point={"duty": 0.5, "vout": 1.0, "il": 0.5},
            parameters={"gain": 1.0},
            assumptions=("unit_test_fixture",),
            verified_by="tests/test_model_capabilities.py",
        )


def test_small_signal_model_accepts_a_rectangular_multi_output_matrix() -> None:
    model = SmallSignalModel(
        plant_id="multi-output",
        method="analytic",
        state_space=LinearStateSpace(
            ((0.0, 1.0), (0.0, 0.0)),
            ((0.0,), (1.0,)),
        ),
        output_matrix=((1.0, 2.0), (3.0, 4.0)),
        feedthrough=0.0,
        input_names=("duty",),
        output_names=("a", "b"),
        operating_point={"duty": 0.5, "vout": 1.0, "il": 0.5},
        parameters={"gain": 1.0},
        assumptions=("unit_test_fixture",),
        verified_by=(
            "tests/test_model_capabilities.py::"
            "test_small_signal_model_accepts_a_rectangular_multi_output_matrix"
        ),
    )

    assert model.output_names == ("a", "b")


def test_small_signal_model_rejects_ragged_output_matrix_rows() -> None:
    with pytest.raises(ValueError, match="row length"):
        SmallSignalModel(
            plant_id="ragged",
            method="analytic",
            state_space=LinearStateSpace(
                ((0.0, 1.0), (0.0, 0.0)),
                ((0.0,), (1.0,)),
            ),
            output_matrix=((1.0, 2.0), (3.0,)),
            feedthrough=0.0,
            input_names=("duty",),
            output_names=("a", "b"),
            operating_point={"duty": 0.5, "vout": 1.0, "il": 0.5},
            parameters={"gain": 1.0},
            assumptions=("unit_test_fixture",),
            verified_by=(
                "tests/test_model_capabilities.py::"
                "test_small_signal_model_rejects_ragged_output_matrix_rows"
            ),
        )


# ---------------------------------------------------------------------------
# Provider origin and self-certification boundary
# ---------------------------------------------------------------------------


def _model_kwargs(**overrides):
    values = dict(
        plant_id="unit",
        method="analytic",
        state_space=LinearStateSpace(((0.0,),), ((1.0,),)),
        output_matrix=((1.0,),),
        feedthrough=0.0,
        operating_point={"duty": 0.5, "vout": 1.0, "il": 0.5},
        parameters={"gain": 1.0},
        assumptions=("unit_test_fixture",),
        verified_by="tests/test_model_capabilities.py::_unit_model",
    )
    values.update(overrides)
    return values


@pytest.mark.parametrize("verified_by", ["comparable", "tests/nope.py", ""])
def test_small_signal_model_rejects_unshapeable_verified_by(verified_by: str) -> None:
    with pytest.raises(ValueError, match="tests/<file>\\.py::<test_name>"):
        SmallSignalModel(**_model_kwargs(verified_by=verified_by))


def test_verified_by_rejects_evidence_level_tokens() -> None:
    for test_name in ("test_comparable_case", "test_diagnostic_only_case"):
        with pytest.raises(ValueError, match="evidence-level"):
            SmallSignalModel(
                **_model_kwargs(
                    verified_by=f"tests/test_model_capabilities.py::{test_name}"
                )
            )


def test_verified_by_allows_words_that_contain_evidence_levels() -> None:
    model = SmallSignalModel(
        **_model_kwargs(
            verified_by="tests/test_model_capabilities.py::test_unlimited_registry_case"
        )
    )

    assert "unlimited" in model.verified_by


@pytest.mark.parametrize("evidence_level", RUNTIME_EVIDENCE_LEVELS)
def test_small_signal_model_rejects_evidence_level_assumptions(
    evidence_level: str,
) -> None:
    with pytest.raises(ValueError, match="evidence-level"):
        SmallSignalModel(**_model_kwargs(assumptions=(evidence_level,)))


def test_small_signal_model_origin_defaults_to_caller_registered() -> None:
    model = _unit_model("origin-default")

    assert model.origin == "caller_registered"
    assert model.origin in CAPABILITY_ORIGINS


def test_builtin_small_signal_models_declare_builtin_origin() -> None:
    load_builtin_model_capabilities()

    buck = small_signal_model_for(_buck(), _operating_point())
    boost = small_signal_model_for(_boost(), _operating_point(duty=0.4))

    assert buck.origin == "builtin"
    assert boost.origin == "builtin"


def test_small_signal_model_rejects_unknown_origin() -> None:
    with pytest.raises(ValueError, match="origin must be one of"):
        SmallSignalModel(**_model_kwargs(origin="certified"))


def test_caller_factory_cannot_claim_builtin_small_signal_origin() -> None:
    register_small_signal_model(
        "fake-builtin",
        lambda plant, operating_point: _unit_model(
            "fake-builtin", origin="builtin"
        ),
    )

    with pytest.raises(CapabilityUnavailable) as caught:
        small_signal_model_for(_KeyedPlant("fake-builtin"), _operating_point())

    assert caught.value.reason == "model_origin_not_permitted"
    assert "builtin" in str(caught.value)


def test_caller_factory_cannot_claim_builtin_loss_origin() -> None:
    register_loss_model(
        "fake-loss-builtin",
        lambda plant, operating_point: _unit_loss(
            "fake-loss-builtin", origin="builtin"
        ),
    )

    with pytest.raises(CapabilityUnavailable) as caught:
        loss_model_for(_KeyedPlant("fake-loss-builtin"), _operating_point())

    assert caught.value.reason == "model_origin_not_permitted"
    assert "builtin" in str(caught.value)


def test_builtin_buck_reports_builtin_capability_origin() -> None:
    load_builtin_model_capabilities()
    model = small_signal_model_for(_buck(), _operating_point())

    provenance = capability_provenance(
        model,
        sweep=None,
        verified_by=(
            "tests/test_model_capabilities.py::"
            "test_builtin_buck_reports_builtin_capability_origin",
        ),
    )

    assert provenance["capability_origin"] == "builtin"


# ---------------------------------------------------------------------------
# Plant identity consistency
# ---------------------------------------------------------------------------


def test_small_signal_model_plant_id_mismatch_fails_closed() -> None:
    register_small_signal_model("declared-key", _factory("other-plant"))

    with pytest.raises(CapabilityUnavailable) as caught:
        small_signal_model_for(_KeyedPlant("declared-key"), _operating_point())

    assert caught.value.reason == "model_plant_id_mismatch"
    assert "declared-key" in str(caught.value)
    assert "other-plant" in str(caught.value)


def test_loss_model_plant_id_mismatch_fails_closed() -> None:
    register_loss_model(
        "loss-key", lambda plant, operating_point: _unit_loss("other-loss")
    )

    with pytest.raises(CapabilityUnavailable) as caught:
        loss_model_for(_KeyedPlant("loss-key"), _operating_point())

    assert caught.value.reason == "model_plant_id_mismatch"
    assert "loss-key" in str(caught.value)
    assert "other-loss" in str(caught.value)


# ---------------------------------------------------------------------------
# LossBreakdown invariants
# ---------------------------------------------------------------------------


def _loss_kwargs(**overrides):
    values = dict(
        plant_id="loss-invariant",
        method="analytic",
        terms={"conduction_w": 0.5},
        total_loss_w=0.5,
        output_power_w=9.5,
        input_power_w=10.0,
        efficiency=0.95,
        assumptions=("unit_test_fixture",),
        verified_by="tests/test_model_capabilities.py::_unit_loss",
    )
    values.update(overrides)
    return values


@pytest.mark.parametrize(
    "overrides",
    [
        # total_loss_w does not equal sum(terms).
        {"total_loss_w": 0.6, "output_power_w": 9.4, "efficiency": 0.94},
        # input_power_w does not equal output_power_w + total_loss_w.
        {"output_power_w": 9.4, "efficiency": 0.94},
        # efficiency does not equal output_power_w / input_power_w.
        {"efficiency": 0.90},
        # method is outside LINEARIZATION_METHODS.
        {"method": "certified"},
        # efficiency == 1.0 without declared_lossless.
        {
            "terms": {"ideal_w": 0.0},
            "total_loss_w": 0.0,
            "output_power_w": 10.0,
            "efficiency": 1.0,
        },
    ],
)
def test_loss_breakdown_rejects_inconsistent_values(overrides) -> None:
    with pytest.raises(ValueError):
        LossBreakdown(**_loss_kwargs(**overrides))


def test_loss_breakdown_accepts_consistent_values() -> None:
    breakdown = LossBreakdown(**_loss_kwargs())

    assert breakdown.efficiency == pytest.approx(0.95)
    assert breakdown.declared_lossless is False
    assert breakdown.to_dict()["declared_lossless"] is False


def test_loss_breakdown_to_dict_carries_capability_provenance() -> None:
    operating_point = {"duty": 0.5, "vout": 9.5, "il": 0.95}
    breakdown = LossBreakdown(
        **_loss_kwargs(
            operating_point=operating_point,
            origin="caller_registered",
            plant_verified_by="tests/test_model_capabilities.py::_unit_loss",
        )
    )

    payload = breakdown.to_dict()
    provenance = payload["provenance"]

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
    assert provenance["plant_id"] == "loss-invariant"
    assert provenance["linearization_point"] == operating_point
    assert provenance["method"] == "analytic"
    assert provenance["capability_origin"] == "caller_registered"
    assert provenance["assumptions"] == ["unit_test_fixture"]
    assert provenance["plant_verified_by"] == (
        "tests/test_model_capabilities.py::_unit_loss"
    )
    assert provenance["controller"] is None
    assert provenance["sweep"] is None
    assert provenance["verified_by"] == [breakdown.verified_by]


def test_loss_breakdown_provenance_defaults_to_no_linearization_point() -> None:
    payload = LossBreakdown(**_loss_kwargs()).to_dict()

    assert payload["provenance"]["linearization_point"] is None
    assert payload["provenance"]["capability_origin"] == "caller_registered"
    assert payload["provenance"]["plant_verified_by"] is None


def test_loss_breakdown_rejects_unknown_origin() -> None:
    with pytest.raises(ValueError, match="origin must be one of"):
        LossBreakdown(**_loss_kwargs(origin="certified"))


def test_loss_breakdown_accepts_declared_lossless() -> None:
    breakdown = LossBreakdown(
        **_loss_kwargs(
            terms={"ideal_w": 0.0},
            total_loss_w=0.0,
            output_power_w=10.0,
            efficiency=1.0,
            declared_lossless=True,
        )
    )

    assert breakdown.declared_lossless is True


def test_loss_breakdown_rejects_declared_lossless_with_losses() -> None:
    with pytest.raises(ValueError, match="declared_lossless"):
        LossBreakdown(
            **_loss_kwargs(
                terms={"conduction_w": 1.0},
                total_loss_w=1.0,
                output_power_w=9.0,
                input_power_w=10.0,
                efficiency=0.9,
                declared_lossless=True,
            )
        )


def test_loss_breakdown_rejects_declared_lossless_with_non_unit_efficiency() -> None:
    with pytest.raises(ValueError, match="efficiency"):
        LossBreakdown(
            **_loss_kwargs(
                terms={"ideal_w": 0.0},
                total_loss_w=0.0,
                output_power_w=10.0,
                input_power_w=10.0,
                efficiency=1.0 - 1e-11,
                declared_lossless=True,
            )
        )


def test_loss_breakdown_rejects_negative_output_power() -> None:
    with pytest.raises(ValueError, match="output_power_w"):
        LossBreakdown(
            **_loss_kwargs(
                terms={"conduction_w": 11.0},
                total_loss_w=11.0,
                output_power_w=-1.0,
                input_power_w=10.0,
                efficiency=-0.1,
            )
        )


@pytest.mark.parametrize("declared_lossless", [True, False])
def test_loss_breakdown_rejects_negative_efficiency(declared_lossless) -> None:
    with pytest.raises(ValueError, match="efficiency"):
        LossBreakdown(
            **_loss_kwargs(
                terms={"conduction_w": 9.0},
                total_loss_w=9.0,
                output_power_w=1.0,
                input_power_w=10.0,
                efficiency=-0.1,
                declared_lossless=declared_lossless,
            )
        )


def test_validate_capability_provenance_accepts_core_fields() -> None:
    validated = validate_capability_provenance(
        {
            "plant_id": "buck",
            "method": "analytic",
            "capability_origin": "builtin",
            "assumptions": ("ccm_average_model",),
            "verified_by": [
                "tests/test_model_capabilities.py::test_x"
            ],
            "plant_verified_by": None,
        }
    )

    assert validated["plant_id"] == "buck"
    assert validated["method"] == "analytic"
    assert validated["capability_origin"] == "builtin"
    assert validated["assumptions"] == ["ccm_average_model"]
    assert validated["verified_by"] == [
        "tests/test_model_capabilities.py::test_x"
    ]
    assert validated["plant_verified_by"] is None


@pytest.mark.parametrize(
    "verified_by",
    [
        "tests/test_model_capabilities.py::test_x",
        ["comparable"],
        ["tests/test_model_capabilities.py"],
    ],
)
def test_validate_capability_provenance_rejects_bad_verified_by(
    verified_by,
) -> None:
    with pytest.raises(ValueError, match="verified_by"):
        validate_capability_provenance(
            {
                "plant_id": "buck",
                "method": "analytic",
                "capability_origin": "builtin",
                "assumptions": ("ccm_average_model",),
                "verified_by": verified_by,
                "plant_verified_by": None,
            }
        )


# ---------------------------------------------------------------------------
# Steady-state failure normalization
# ---------------------------------------------------------------------------


class _ExplodingSteadyStatePlant:
    backend_id = "buck"
    input_voltage_v = 12.0
    inductance_h = 100e-6
    capacitance_f = 470e-6
    load_resistance_ohm = 10.0

    def analytical_steady_state(self, duty: float):
        raise ZeroDivisionError(f"singular equilibrium at duty={duty!r}")


def test_analytical_steady_state_exception_becomes_capability_unavailable() -> None:
    load_builtin_model_capabilities()

    with pytest.raises(CapabilityUnavailable) as caught:
        small_signal_model_for(_ExplodingSteadyStatePlant(), _operating_point())

    assert caught.value.reason == "operating_point_not_declared"
    assert "ZeroDivisionError" in str(caught.value)

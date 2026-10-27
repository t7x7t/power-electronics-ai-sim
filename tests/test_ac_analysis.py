"""Tests for the AC / loop-gain / margin route (docs/60 C4, first slice).

Everything here is standard library plus the project's own reference plants and
controllers.  The module independently recomputes transfer functions with
Cramer's rule and Tustin evaluation so the production Gaussian-elimination path
is checked against a second implementation, not against itself.

``stability_margins`` reports numbers, not verdicts.  The assertions below
therefore check signed margins and the three-state ``margin_positive`` field,
never a ``stable``/``unstable`` label.
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from pe_sim.ac_analysis import (
    AC_RESULT_SCHEMA,
    frequency_response,
    loop_gain_response,
    stability_margins,
)
from pe_sim.backends.whole_run_engine import LinearStateSpace
from pe_sim.controllers import Type2Controller
from pe_sim.model_capabilities import (
    CapabilityUnavailable,
    OperatingPoint,
    SmallSignalModel,
    clear_model_capabilities,
    load_builtin_model_capabilities,
    register_small_signal_model,
    small_signal_model_for,
)
from pe_sim.reference_plants import BoostPlant, BuckPlant
from pe_sim.runtime import FakePIController


@pytest.fixture(autouse=True)
def _builtin_capabilities():
    clear_model_capabilities()
    load_builtin_model_capabilities()
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


def _buck_model():
    return small_signal_model_for(_buck(), OperatingPoint(duty=0.5))


def _type2_controller() -> Type2Controller:
    return Type2Controller(
        gain=1.0,
        zero_hz=500.0,
        pole_hz=50_000.0,
        control_period_s=1e-4,
    )


def _boost() -> BoostPlant:
    return BoostPlant(
        input_voltage_v=12.0,
        inductance_h=100e-6,
        capacitance_f=470e-6,
        load_resistance_ohm=10.0,
        integration_step_s=1e-6,
    )


class _KeyedPlant:
    def __init__(self, backend_id: str) -> None:
        self.backend_id = backend_id


def _caller_unit_model(plant_id: str) -> SmallSignalModel:
    return SmallSignalModel(
        plant_id=plant_id,
        method="analytic",
        state_space=LinearStateSpace(((0.0,),), ((1.0,),)),
        output_matrix=((1.0,),),
        feedthrough=0.0,
        operating_point={"duty": 0.5, "vout": 1.0, "il": 0.5},
        parameters={"gain": 1.0},
        assumptions=("unit_test_fixture",),
        verified_by="tests/test_ac_analysis.py::_caller_unit_model",
    )


def _manual_loop(
    frequencies: list[float],
    magnitude_db: list[float],
    phase_deg: list[float],
) -> dict[str, object]:
    return {
        "schema": AC_RESULT_SCHEMA,
        "status": "available",
        "kind": "loop_gain",
        "frequencies_hz": list(frequencies),
        "magnitude_db": list(magnitude_db),
        "phase_deg": list(phase_deg),
        "method": "analytic",
        "assumptions": ["synthetic_loop_for_margin_interpolation"],
    }


def _polyval_desc(coefficients: list[float], z: complex) -> complex:
    value = 0j
    for coefficient in coefficients:
        value = value * z + coefficient
    return value


def _cramer_transfer(model, frequency_hz: float) -> complex:
    s = complex(0.0, 2.0 * math.pi * frequency_hz)
    matrix_a = model.state_space.matrix_a
    input_matrix = model.state_space.input_matrix
    m00 = s - matrix_a[0][0]
    m01 = -matrix_a[0][1]
    m10 = -matrix_a[1][0]
    m11 = s - matrix_a[1][1]
    determinant = m00 * m11 - m01 * m10
    b0 = input_matrix[0][0]
    b1 = input_matrix[1][0]
    x0 = (b0 * m11 - b1 * m01) / determinant
    x1 = (m00 * b1 - m10 * b0) / determinant
    row = model.output_matrix[0]
    return row[0] * x0 + row[1] * x1 + model.feedthrough


def _interpolated_peak_hz(response: dict[str, object]) -> tuple[float, int]:
    frequencies = response["frequencies_hz"]
    magnitude_db = response["magnitude_db"]
    index = max(range(len(magnitude_db)), key=lambda item: magnitude_db[item])
    if index == 0 or index == len(magnitude_db) - 1:
        return frequencies[index], index
    x0 = math.log10(frequencies[index - 1])
    x1 = math.log10(frequencies[index])
    x2 = math.log10(frequencies[index + 1])
    y0 = magnitude_db[index - 1]
    y1 = magnitude_db[index]
    y2 = magnitude_db[index + 1]
    denominator = y0 - 2.0 * y1 + y2
    if denominator == 0.0:
        return frequencies[index], index
    offset = 0.5 * (x2 - x1) * (y0 - y2) / denominator
    return 10.0 ** (x1 + offset), index


def _interpolated_crossing(
    frequencies: list[float],
    values: list[float],
    target: float,
    start_index: int,
) -> float:
    for index in range(start_index, len(values) - 1):
        low = values[index]
        high = values[index + 1]
        if low == high:
            continue
        if (low - target) * (high - target) <= 0.0:
            fraction = (target - low) / (high - low)
            return 10.0 ** (
                math.log10(frequencies[index])
                + fraction
                * (math.log10(frequencies[index + 1]) - math.log10(frequencies[index]))
            )
    raise AssertionError(f"no crossing of {target} found")


def test_frequency_response_schema_shape_and_log_spacing() -> None:
    model = _buck_model()
    response = frequency_response(model, start_hz=10.0, stop_hz=100_000.0, points=101)

    assert response["schema"] == AC_RESULT_SCHEMA
    assert response["status"] == "available"
    assert response["kind"] == "plant_transfer_function"
    assert response["plant_id"] == "buck"
    assert response["method"] == model.method
    assert response["linearization_point"] == dict(model.operating_point)
    assert response["assumptions"] == list(model.assumptions)
    assert response["plant_verified_by"] == model.verified_by
    assert response["verified_by"] == [
        "tests/test_ac_analysis.py::"
        "test_frequency_response_declares_self_contained_provenance"
    ]
    assert response["sweep"] == {
        "start_hz": 10.0,
        "stop_hz": 100_000.0,
        "points": 101,
        "spacing": "log10",
    }

    frequencies = response["frequencies_hz"]
    assert len(frequencies) == 101
    assert frequencies[0] == pytest.approx(10.0)
    assert frequencies[-1] == pytest.approx(100_000.0)
    step = math.log10(frequencies[-1]) - math.log10(frequencies[-2])
    for lower, upper in zip(frequencies, frequencies[1:]):
        assert math.log10(upper) - math.log10(lower) == pytest.approx(step)

    for key in ("frequencies_hz", "magnitude", "magnitude_db", "phase_deg"):
        assert len(response[key]) == 101
        assert all(math.isfinite(value) for value in response[key])

    for magnitude, magnitude_db in zip(response["magnitude"], response["magnitude_db"]):
        assert magnitude == pytest.approx(10.0 ** (magnitude_db / 20.0))


def test_frequency_response_phase_is_unwrapped() -> None:
    model = _buck_model()
    response = frequency_response(model, start_hz=10.0, stop_hz=100_000.0, points=301)
    phases = response["phase_deg"]

    assert -180.0 < phases[0] <= 180.0
    for previous, current in zip(phases, phases[1:]):
        assert abs(current - previous) < 180.0


def test_buck_bode_peak_matches_analytic_resonance() -> None:
    plant = _buck()
    model = small_signal_model_for(plant, OperatingPoint(duty=0.5))
    response = frequency_response(model, start_hz=10.0, stop_hz=10_000.0, points=512)

    peak_hz, peak_index = _interpolated_peak_hz(response)
    resonant_hz = 1.0 / (
        2.0 * math.pi * math.sqrt(plant.inductance_h * plant.capacitance_f)
    )
    assert abs(peak_hz - resonant_hz) / resonant_hz < 0.05

    dc_db = 20.0 * math.log10(abs(model.transfer_function(0.0)))
    assert response["magnitude_db"][peak_index] > dc_db
    assert response["magnitude_db"][peak_index] > response["magnitude_db"][-1]


def test_buck_bode_minus_three_db_matches_analytic_cutoff() -> None:
    plant = _buck()
    model = small_signal_model_for(plant, OperatingPoint(duty=0.5))
    response = frequency_response(model, start_hz=10.0, stop_hz=10_000.0, points=512)

    _, peak_index = _interpolated_peak_hz(response)
    dc_db = 20.0 * math.log10(abs(model.transfer_function(0.0)))
    cutoff_hz = _interpolated_crossing(
        response["frequencies_hz"], response["magnitude_db"], dc_db - 3.0, peak_index
    )

    damping = (1.0 / (2.0 * plant.load_resistance_ohm)) * math.sqrt(
        plant.inductance_h / plant.capacitance_f
    )
    resonant_hz = 1.0 / (
        2.0 * math.pi * math.sqrt(plant.inductance_h * plant.capacitance_f)
    )
    # (1 - x)^2 + 4*zeta^2*x = 2 with x = (f / f_res)^2.
    discriminant = (4.0 * damping * damping - 2.0) ** 2 + 4.0
    x = (2.0 - 4.0 * damping * damping + math.sqrt(discriminant)) / 2.0
    analytic_cutoff_hz = resonant_hz * math.sqrt(x)

    assert abs(cutoff_hz - analytic_cutoff_hz) / analytic_cutoff_hz < 0.01


@pytest.mark.parametrize("frequency_hz", [1.0, 734.0, 100_000.0])
def test_transfer_function_matches_independent_cramer_solution(
    frequency_hz: float,
) -> None:
    model = _buck_model()

    direct = model.transfer_function(frequency_hz)
    independent = _cramer_transfer(model, frequency_hz)
    assert abs(direct - independent) < 1e-10


def test_loop_gain_response_declares_tustin_mapping_and_controller() -> None:
    model = _buck_model()
    controller = _type2_controller()
    response = loop_gain_response(
        model, controller, start_hz=1.0, stop_hz=100_000.0, points=241
    )

    assert response["schema"] == AC_RESULT_SCHEMA
    assert response["status"] == "available"
    assert response["kind"] == "loop_gain"
    assert response["method"] == model.method
    assert response["controller"] == {
        "class": "Type2Controller",
        "sample_period_s": 1e-4,
        "mapping": "tustin_continuous_equivalent",
    }
    assert "tustin_continuous_equivalent" in response["assumptions"]
    assert "loop_gain_excludes_sampling_delay" in response["assumptions"]
    for key in ("frequencies_hz", "magnitude", "magnitude_db", "phase_deg"):
        assert all(math.isfinite(value) for value in response[key])


def test_loop_gain_response_requires_controller_coefficients() -> None:
    model = _buck_model()

    with pytest.raises(CapabilityUnavailable) as caught:
        loop_gain_response(
            model,
            FakePIController(kp=0.2),
            start_hz=10.0,
            stop_hz=1_000.0,
            points=10,
        )

    assert caught.value.reason == "controller_transfer_function_not_declared"


def test_loop_gain_response_requires_controller_sample_period() -> None:
    class _CoefficientsWithoutPeriod:
        coefficients = {"numerator": [1.0, 0.0], "denominator": [1.0, -0.5]}

    with pytest.raises(CapabilityUnavailable) as caught:
        loop_gain_response(
            _buck_model(),
            _CoefficientsWithoutPeriod(),
            start_hz=10.0,
            stop_hz=1_000.0,
            points=10,
        )

    assert caught.value.reason == "controller_transfer_function_not_declared"


def test_loop_gain_matches_plant_times_tustin_controller() -> None:
    model = _buck_model()
    controller = _type2_controller()
    frequency_hz = 1_000.0
    response = loop_gain_response(
        model, controller, start_hz=frequency_hz, stop_hz=10.0 * frequency_hz, points=2
    )

    sample_period = controller.sample_period_s
    s = complex(0.0, 2.0 * math.pi * frequency_hz)
    z = (1.0 + s * sample_period / 2.0) / (1.0 - s * sample_period / 2.0)
    numerator = list(controller.coefficients["b"])
    denominator = [1.0, *controller.coefficients["a"]]
    controller_factor = _polyval_desc(numerator, z) / _polyval_desc(denominator, z)
    expected = model.transfer_function(frequency_hz) * controller_factor

    assert response["magnitude"][0] == pytest.approx(abs(expected), rel=1e-9)
    expected_phase = math.degrees(math.atan2(expected.imag, expected.real))
    assert response["phase_deg"][0] == pytest.approx(expected_phase, abs=1e-9)


def test_stability_margins_first_order_integrator_loop() -> None:
    gain = 2.0 * math.pi * 1_000.0
    frequencies = [10.0 ** (5.0 * index / 400.0) for index in range(401)]
    magnitude_db = [
        20.0 * math.log10(gain / (2.0 * math.pi * frequency))
        for frequency in frequencies
    ]
    phases = [-90.0] * len(frequencies)

    margins = stability_margins(_manual_loop(frequencies, magnitude_db, phases))

    assert margins["schema"] == AC_RESULT_SCHEMA
    assert margins["status"] == "available"
    assert margins["kind"] == "stability_margins"
    assert margins["gain_crossover_hz"] == pytest.approx(1_000.0, rel=1e-9)
    assert margins["phase_margin_deg"] == pytest.approx(90.0, abs=1e-9)
    assert margins["phase_crossover_hz"] is None
    assert margins["gain_margin_db"] is None
    assert "no_phase_crossover" in margins["notes"]
    assert margins["margin_positive"] is None
    assert margins["method"] == "analytic"
    assert margins["assumptions"] == ["synthetic_loop_for_margin_interpolation"]


def test_stability_margins_first_order_lag_loop_is_near_ninety_degrees() -> None:
    corner_rad_s = 2.0 * math.pi * 1.0
    gain = 1_000.0 * corner_rad_s
    angular_frequencies = [
        2.0 * math.pi * 10.0 ** (5.0 * index / 400.0) for index in range(401)
    ]
    frequencies = [omega / (2.0 * math.pi) for omega in angular_frequencies]
    magnitude_db = [
        20.0 * math.log10(gain / math.hypot(omega, corner_rad_s))
        for omega in angular_frequencies
    ]
    phases = [
        -math.degrees(math.atan2(omega, corner_rad_s))
        for omega in angular_frequencies
    ]

    margins = stability_margins(_manual_loop(frequencies, magnitude_db, phases))

    crossover_rad_s = math.sqrt(gain * gain - corner_rad_s * corner_rad_s)
    expected_crossover_hz = crossover_rad_s / (2.0 * math.pi)
    expected_phase_margin = 90.0 + math.degrees(
        math.atan2(corner_rad_s, crossover_rad_s)
    )
    assert margins["gain_crossover_hz"] == pytest.approx(
        expected_crossover_hz, rel=1e-6
    )
    assert margins["phase_margin_deg"] == pytest.approx(
        expected_phase_margin, abs=1e-4
    )
    assert margins["phase_margin_deg"] == pytest.approx(90.0, abs=0.1)
    assert margins["phase_crossover_hz"] is None
    assert margins["gain_margin_db"] is None
    assert "no_phase_crossover" in margins["notes"]


def test_stability_margins_interpolates_gain_crossover() -> None:
    frequencies = [10.0, 100.0]
    magnitude_db = [-20.0, 20.0]
    phases = [-60.0, -120.0]

    margins = stability_margins(_manual_loop(frequencies, magnitude_db, phases))

    assert margins["gain_crossover_hz"] == pytest.approx(10.0 ** 1.5, abs=1e-6)
    assert margins["phase_margin_deg"] == pytest.approx(90.0, abs=1e-6)
    assert margins["phase_crossover_hz"] is None
    assert margins["gain_margin_db"] is None
    assert "no_phase_crossover" in margins["notes"]
    assert margins["margin_positive"] is None


def test_stability_margins_interpolates_phase_crossover() -> None:
    frequencies = [10.0, 100.0]
    magnitude_db = [10.0, 30.0]
    phases = [-90.0, -270.0]

    margins = stability_margins(_manual_loop(frequencies, magnitude_db, phases))

    assert margins["gain_crossover_hz"] is None
    assert margins["phase_margin_deg"] is None
    assert "no_gain_crossover" in margins["notes"]
    assert margins["phase_crossover_hz"] == pytest.approx(10.0 ** 1.5, abs=1e-6)
    assert margins["gain_margin_db"] == pytest.approx(-20.0, abs=1e-6)
    assert margins["margin_positive"] is None


def test_stability_margins_reports_both_margins_without_stability_claim() -> None:
    frequencies = [1.0, 10.0, 100.0, 1_000.0]
    magnitude_db = [40.0, 10.0, -10.0, -40.0]
    phases = [-90.0, -120.0, -200.0, -260.0]

    margins = stability_margins(_manual_loop(frequencies, magnitude_db, phases))

    assert margins["gain_crossover_hz"] == pytest.approx(10.0 ** 1.5, abs=1e-6)
    assert margins["phase_margin_deg"] == pytest.approx(20.0, abs=1e-6)
    assert margins["phase_crossover_hz"] == pytest.approx(10.0 ** 1.75, abs=1e-6)
    assert margins["gain_margin_db"] == pytest.approx(5.0, abs=1e-6)
    assert margins["margin_positive"] is True
    assert "stable" not in margins
    assert "unstable" not in margins


def test_stability_margins_treats_zero_margin_as_not_positive() -> None:
    frequencies = [1.0, 10.0, 100.0]
    magnitude_db = [10.0, 0.0, -10.0]
    phases = [-90.0, -180.0, -270.0]

    margins = stability_margins(_manual_loop(frequencies, magnitude_db, phases))

    assert margins["gain_crossover_hz"] == pytest.approx(10.0)
    assert margins["phase_margin_deg"] == pytest.approx(0.0, abs=1e-12)
    assert margins["phase_crossover_hz"] == pytest.approx(10.0)
    assert margins["gain_margin_db"] == pytest.approx(0.0, abs=1e-12)
    assert margins["margin_positive"] is False


def test_stability_margins_rejects_plant_transfer_function_response() -> None:
    response = frequency_response(
        _buck_model(), start_hz=10.0, stop_hz=1_000.0, points=10
    )

    with pytest.raises(ValueError):
        stability_margins(response)


@pytest.mark.parametrize(
    "overrides",
    [
        {"start_hz": 0.0},
        {"start_hz": -1.0},
        {"stop_hz": 10.0},
        {"stop_hz": 5.0},
        {"points": 1},
        {"points": 513},
        {"points": 2.5},
        {"points": True},
    ],
)
def test_frequency_response_rejects_invalid_sweep(overrides) -> None:
    arguments = {"start_hz": 10.0, "stop_hz": 1_000.0, "points": 10}
    arguments.update(overrides)

    with pytest.raises(ValueError, match="start_hz|stop_hz|points"):
        frequency_response(_buck_model(), **arguments)


def test_loop_gain_response_validates_the_same_sweep() -> None:
    with pytest.raises(ValueError, match="start_hz|stop_hz|points"):
        loop_gain_response(
            _buck_model(), _type2_controller(), start_hz=0.0, stop_hz=1_000.0, points=10
        )


# ---------------------------------------------------------------------------
# Provenance chain
# ---------------------------------------------------------------------------


def test_frequency_response_declares_self_contained_provenance() -> None:
    model = _buck_model()
    response = frequency_response(
        model, start_hz=10.0, stop_hz=10_000.0, points=10
    )
    provenance = response["provenance"]

    assert provenance["plant_id"] == "buck"
    assert provenance["linearization_point"] == dict(model.operating_point)
    assert provenance["method"] == model.method
    assert provenance["capability_origin"] == "builtin"
    assert provenance["assumptions"] == list(model.assumptions)
    assert provenance["plant_verified_by"] == model.verified_by
    assert provenance["controller"] is None
    assert provenance["sweep"] == {
        "start_hz": 10.0,
        "stop_hz": 10_000.0,
        "points": 10,
        "spacing": "log10",
    }
    assert provenance["verified_by"] == [
        "tests/test_ac_analysis.py::"
        "test_frequency_response_declares_self_contained_provenance"
    ]


def test_loop_gain_provenance_is_self_contained() -> None:
    model = _buck_model()
    controller = _type2_controller()
    response = loop_gain_response(
        model, controller, start_hz=10.0, stop_hz=100_000.0, points=241
    )
    provenance = response["provenance"]

    assert provenance["verified_by"] == [
        "tests/test_ac_analysis.py::test_loop_gain_provenance_is_self_contained"
    ]
    assert provenance["plant_verified_by"] == model.verified_by
    assert provenance["verified_by"] != [provenance["plant_verified_by"]]
    assert provenance["capability_origin"] == "builtin"
    assert provenance["sweep"] == {
        "start_hz": 10.0,
        "stop_hz": 100_000.0,
        "points": 241,
        "spacing": "log10",
    }

    controller_record = provenance["controller"]
    assert controller_record["class"] == "Type2Controller"
    assert controller_record["sample_period_s"] == pytest.approx(1e-4)
    assert controller_record["mapping"] == "tustin_continuous_equivalent"
    assert controller_record["coefficient_shape"] == "b_a"
    assert controller_record["coefficients"]["b"] == list(
        controller.coefficients["b"]
    )
    # The declared ``a`` is preserved; the implicit leading 1.0 is not inserted.
    assert controller_record["coefficients"]["a"] == list(
        controller.coefficients["a"]
    )
    assert len(controller_record["coefficients"]["a"]) == len(
        controller.coefficients["b"]
    ) - 1

    for item in (
        "tustin_continuous_equivalent",
        "loop_gain_excludes_sampling_delay",
        "zero_order_hold_phase_not_modelled",
        "discrete_time_aliasing_not_modelled",
    ):
        assert item in provenance["assumptions"]


def test_provenance_records_capability_origin() -> None:
    builtin_response = frequency_response(
        _buck_model(), start_hz=10.0, stop_hz=1_000.0, points=10
    )
    assert builtin_response["provenance"]["capability_origin"] == "builtin"
    assert (
        "capability_provider_is_caller_registered"
        not in builtin_response["provenance"]["assumptions"]
    )

    register_small_signal_model(
        "caller-origin",
        lambda plant, operating_point: _caller_unit_model("caller-origin"),
    )
    caller_model = small_signal_model_for(
        _KeyedPlant("caller-origin"), OperatingPoint(duty=0.5)
    )
    caller_response = frequency_response(
        caller_model, start_hz=10.0, stop_hz=1_000.0, points=10
    )

    assert caller_response["provenance"]["capability_origin"] == "caller_registered"
    assert (
        "capability_provider_is_caller_registered"
        in caller_response["provenance"]["assumptions"]
    )
    assert (
        "capability_provider_is_caller_registered"
        in caller_response["assumptions"]
    )


def test_stability_margins_inherits_provenance_and_records_interpolation() -> None:
    loop = loop_gain_response(
        _buck_model(), _type2_controller(), start_hz=10.0, stop_hz=100_000.0, points=241
    )
    margins = stability_margins(loop)

    assert margins["provenance"] == loop["provenance"]
    assert margins["provenance"]["sweep"] == loop["provenance"]["sweep"]
    assert margins["provenance"]["controller"] == loop["provenance"]["controller"]
    assert margins["interpolation"] == "log10_frequency_linear_db_and_degrees"
    assert "phase_unwrap_may_alias_when_sweep_is_coarse" not in margins["notes"]

    coarse_loop = loop_gain_response(
        _buck_model(), _type2_controller(), start_hz=10.0, stop_hz=100_000.0, points=8
    )
    coarse_margins = stability_margins(coarse_loop)

    assert "phase_unwrap_may_alias_when_sweep_is_coarse" in coarse_margins["notes"]
    assert coarse_margins["provenance"]["sweep"]["points"] == 8


def test_gain_margin_depends_on_sweep_resolution_but_is_recorded() -> None:
    model = _buck_model()
    controller = _type2_controller()
    coarse = loop_gain_response(
        model, controller, start_hz=1.0, stop_hz=1_000_000.0, points=241
    )
    fine = loop_gain_response(
        model, controller, start_hz=1.0, stop_hz=1_000_000.0, points=512
    )

    coarse_margins = stability_margins(coarse)
    fine_margins = stability_margins(fine)

    assert coarse_margins["gain_margin_db"] is not None
    assert fine_margins["gain_margin_db"] is not None
    assert coarse_margins["gain_margin_db"] != fine_margins["gain_margin_db"]
    assert coarse_margins["provenance"]["sweep"]["points"] == 241
    assert fine_margins["provenance"]["sweep"]["points"] == 512


# ---------------------------------------------------------------------------
# Controller shape ambiguity and error messages
# ---------------------------------------------------------------------------


def test_loop_gain_rejects_ambiguous_coefficient_shapes() -> None:
    class _AmbiguousController:
        sample_period_s = 1e-4
        coefficients = {
            "numerator": [1.0, 0.0],
            "denominator": [1.0, -0.5],
            "b": [1.0, 0.0],
            "a": [-0.5],
        }

    with pytest.raises(ValueError, match="ambiguous"):
        loop_gain_response(
            _buck_model(),
            _AmbiguousController(),
            start_hz=10.0,
            stop_hz=1_000.0,
            points=10,
        )


def test_frequency_response_zero_magnitude_error_names_the_frequency() -> None:
    model = SmallSignalModel(
        plant_id="zero-gain",
        method="analytic",
        state_space=LinearStateSpace(((0.0,),), ((1.0,),)),
        output_matrix=((0.0,),),
        feedthrough=0.0,
        operating_point={"duty": 0.5, "vout": 1.0, "il": 0.5},
        parameters={"gain": 0.0},
        assumptions=("unit_test_fixture",),
        verified_by=(
            "tests/test_ac_analysis.py::"
            "test_frequency_response_zero_magnitude_error_names_the_frequency"
        ),
    )

    with pytest.raises(ValueError, match="10\\.0 Hz"):
        frequency_response(model, start_hz=10.0, stop_hz=100.0, points=2)


# ---------------------------------------------------------------------------
# Declared verification ids
# ---------------------------------------------------------------------------


def test_declared_verified_by_identifiers_exist() -> None:
    repository_root = Path(__file__).resolve().parents[1]
    identifiers: set[str] = set()

    for plant, duty in ((_buck(), 0.5), (_boost(), 0.4)):
        model = small_signal_model_for(plant, OperatingPoint(duty=duty))
        identifiers.add(model.verified_by)

        response = frequency_response(
            model, start_hz=10.0, stop_hz=1_000.0, points=10
        )
        identifiers.update(response["provenance"]["verified_by"])

        loop = loop_gain_response(
            model, _type2_controller(), start_hz=10.0, stop_hz=10_000.0, points=24
        )
        identifiers.update(loop["provenance"]["verified_by"])

        margins = stability_margins(loop)
        identifiers.update(margins["provenance"]["verified_by"])

    for identifier in sorted(identifiers):
        path_part, separator, test_name = identifier.partition("::")
        assert separator and test_name, identifier
        path = repository_root / path_part
        assert path.is_file(), f"{identifier} points at a missing file {path}"
        source = path.read_text(encoding="utf-8")
        base_name = test_name.split("[", 1)[0]
        assert f"def {base_name}(" in source, (
            f"{identifier} points at a missing test"
        )

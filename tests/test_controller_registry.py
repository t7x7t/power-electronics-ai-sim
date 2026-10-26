"""Controller registry and discrete compensator behaviour (ADR 0007)."""

from __future__ import annotations

import cmath
import json
import math
from pathlib import Path

import pytest

from pe_sim.builtin_controllers import load_builtin_controllers
from pe_sim.cli import main
from pe_sim.controller_registry import (
    ControllerBuildContext,
    ControllerRegistration,
    clear_controllers,
    register_controller,
    registered_controllers,
    resolve_controller,
)
from pe_sim.controllers import (
    BiquadController,
    DiscreteCompensator,
    Type2Controller,
    Type3Controller,
)
from pe_sim.runtime import FakePIController

REPOSITORY = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _isolated_controller_registry():
    clear_controllers()
    load_builtin_controllers()
    yield
    clear_controllers()
    load_builtin_controllers()


def _context(period_s: float = 1e-3) -> ControllerBuildContext:
    return ControllerBuildContext(control_period_s=period_s)


def _observe(controller, measurement: float, vref: float, time_s: float, period_s: float = 0.1):
    return controller.observe(
        {"vout": measurement},
        {"vref": vref},
        {"sample_time_s": time_s, "control_period_s": period_s},
        {},
    )


def _poly_mul(left, right):
    result = [0.0] * (len(left) + len(right) - 1)
    for left_index, left_value in enumerate(left):
        for right_index, right_value in enumerate(right):
            result[left_index + right_index] += left_value * right_value
    return result


def _tustin_pair(frequency_hz: float, period_s: float) -> tuple[float, float]:
    k = 2.0 / period_s
    w = 2.0 * math.pi * frequency_hz
    return 1.0 + k / w, 1.0 - k / w


def _reference_type2(gain, zero_hz, pole_hz, period_s):
    k = 2.0 / period_s
    az, bz = _tustin_pair(zero_hz, period_s)
    ap, bp = _tustin_pair(pole_hz, period_s)
    numerator = [gain / k * value for value in _poly_mul([az, bz], [1.0, 1.0])]
    denominator = _poly_mul([ap, bp], [1.0, -1.0])
    d0 = denominator[0]
    return [value / d0 for value in numerator], [value / d0 for value in denominator[1:]]


def _reference_type3(gain, zero1_hz, zero2_hz, pole1_hz, pole2_hz, period_s):
    k = 2.0 / period_s
    az1, bz1 = _tustin_pair(zero1_hz, period_s)
    az2, bz2 = _tustin_pair(zero2_hz, period_s)
    ap1, bp1 = _tustin_pair(pole1_hz, period_s)
    ap2, bp2 = _tustin_pair(pole2_hz, period_s)
    zeros = _poly_mul([az1, bz1], [az2, bz2])
    poles = _poly_mul([ap1, bp1], [ap2, bp2])
    numerator = [gain / k * value for value in _poly_mul(zeros, [1.0, 1.0])]
    denominator = _poly_mul(poles, [1.0, -1.0])
    d0 = denominator[0]
    return [value / d0 for value in numerator], [value / d0 for value in denominator[1:]]


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_registry_resolves_pi_and_pid_and_enforces_their_key_sets():
    pi = resolve_controller("pi", {"kp": 0.5, "ki": 2.0}, _context())
    pid = resolve_controller(
        "pid", {"kp": 0.5, "ki": 2.0, "kd": 0.01}, _context()
    )

    assert isinstance(pi, FakePIController)
    assert isinstance(pid, FakePIController)
    # A PID key set copied onto ``pi`` is refused rather than ignored.
    with pytest.raises(ValueError, match="controller_config"):
        resolve_controller("pi", {"kp": 0.5, "kd": 0.01}, _context())


def test_unknown_controller_id_names_the_registered_ids():
    with pytest.raises(ValueError) as excinfo:
        resolve_controller("lag-compensator", {}, _context())

    message = str(excinfo.value)
    assert "controller_id" in message
    assert "registered" in message
    for registered in ("biquad", "pi", "pid", "type2", "type3"):
        assert registered in message


def test_duplicate_controller_registration_is_rejected():
    def factory(config, context):
        del config, context
        return object()

    registration = ControllerRegistration(
        controller_id="probe-compensator",
        factory=factory,
        config_keys=frozenset({"gain"}),
        class_name="object",
    )
    register_controller(registration)

    with pytest.raises(ValueError, match="already registered"):
        register_controller(registration)

    replacement = ControllerRegistration(
        controller_id="probe-compensator",
        factory=factory,
        config_keys=frozenset({"gain"}),
        class_name="object",
    )
    register_controller(replacement, replace=True)
    assert registered_controllers()["probe-compensator"] == replacement


@pytest.mark.parametrize("control_period_s", [0.0, -1.0, float("nan"), float("inf")])
def test_build_context_requires_a_positive_finite_control_period(control_period_s):
    with pytest.raises(ValueError, match="control_period_s"):
        ControllerBuildContext(control_period_s=control_period_s)


# ---------------------------------------------------------------------------
# Type II / Type III designs
# ---------------------------------------------------------------------------


def test_type2_coefficients_match_an_independent_tustin_mapping():
    period_s = 1e-3
    controller = Type2Controller.from_config(
        {"gain": 2.0, "zero_hz": 200.0, "pole_hz": 2000.0},
        control_period_s=period_s,
    )
    expected_b, expected_a = _reference_type2(2.0, 200.0, 2000.0, period_s)

    assert controller.coefficients["b"] == pytest.approx(expected_b)
    assert controller.coefficients["a"] == pytest.approx(expected_a)
    assert controller.gain == 2.0
    assert controller.zero_hz == 200.0
    assert controller.pole_hz == 2000.0
    assert controller.sample_period_s == period_s


def test_type3_coefficients_match_an_independent_tustin_mapping():
    period_s = 1e-3
    parameters = {
        "gain": 3.0,
        "zero1_hz": 100.0,
        "zero2_hz": 500.0,
        "pole1_hz": 2000.0,
        "pole2_hz": 5000.0,
    }
    controller = Type3Controller.from_config(parameters, control_period_s=period_s)
    expected_b, expected_a = _reference_type3(
        3.0, 100.0, 500.0, 2000.0, 5000.0, period_s
    )

    assert len(controller.coefficients["b"]) == 4
    assert len(controller.coefficients["a"]) == 3
    assert controller.coefficients["b"] == pytest.approx(expected_b)
    assert controller.coefficients["a"] == pytest.approx(expected_a)


def test_biquad_matches_an_independent_difference_equation_for_ten_steps():
    period_s = 1e-3
    controller = BiquadController.from_config(
        {
            "b0": 0.4,
            "b1": -0.15,
            "b2": 0.05,
            "a1": 0.3,
            "a2": -0.08,
            "output_min": -10.0,
            "output_max": 10.0,
        },
        control_period_s=period_s,
    )
    x_history = [0.0, 0.0]
    y_history = [0.0, 0.0]

    for step, error in enumerate(
        (0.5, 0.4, 0.3, 0.2, 0.1, 0.0, -0.1, -0.2, -0.3, -0.4)
    ):
        request = _observe(
            controller,
            measurement=1.0 - error,
            vref=1.0,
            time_s=step * period_s,
            period_s=period_s,
        )
        expected = (
            0.4 * error
            - 0.15 * x_history[0]
            + 0.05 * x_history[1]
            - 0.3 * y_history[0]
            + 0.08 * y_history[1]
        )
        expected = min(max(expected, -10.0), 10.0)
        assert request.value == pytest.approx(expected)
        assert request.state_update["output"] == pytest.approx(expected)
        x_history = [error, x_history[0]]
        y_history = [expected, y_history[0]]


def test_type2_digital_gain_tracks_the_continuous_design_below_nyquist():
    period_s = 1e-4
    gain, zero_hz, pole_hz = 2.0, 200.0, 2000.0
    controller = Type2Controller.from_config(
        {"gain": gain, "zero_hz": zero_hz, "pole_hz": pole_hz},
        control_period_s=period_s,
    )
    frequency_hz = 20.0
    w = 2.0 * math.pi * frequency_hz
    z_inverse = cmath.exp(-1j * w * period_s)
    b = controller.coefficients["b"]
    a = controller.coefficients["a"]
    digital = abs((b[0] + b[1] * z_inverse + b[2] * z_inverse**2)
                  / (1.0 + a[0] * z_inverse + a[1] * z_inverse**2))
    continuous = abs(
        gain
        * (1.0 + 1j * w / (2.0 * math.pi * zero_hz))
        / ((1j * w) * (1.0 + 1j * w / (2.0 * math.pi * pole_hz)))
    )

    assert abs(digital - continuous) / continuous < 0.05


# ---------------------------------------------------------------------------
# Saturation, snapshot and restore
# ---------------------------------------------------------------------------


def test_output_saturation_stores_the_applied_value_and_recovers():
    controller = BiquadController.from_config(
        {
            "b0": 2.0,
            "b1": 0.0,
            "b2": 0.0,
            "a1": 0.5,
            "a2": 0.0,
            "output_min": 0.0,
            "output_max": 1.0,
        },
        control_period_s=1e-3,
    )

    for step in range(10):
        request = _observe(
            controller, measurement=0.0, vref=1.0, time_s=step * 1e-3
        )
        assert request.value == pytest.approx(1.0)
        assert request.state_update["output"] == pytest.approx(1.0)
        # The delay line holds the applied output, never the unclamped 3.5..
        # request, so the compensator cannot remember an impossible action.
        assert request.state_update["y1"] == pytest.approx(1.0)

    assert controller.snapshot()["history"]["y"][0] == pytest.approx(1.0)

    for step in range(10, 20):
        request = _observe(
            controller, measurement=2.0, vref=1.0, time_s=step * 1e-3
        )
        if request.value < 1.0:
            break
    else:  # pragma: no cover - the failure message is the assertion
        pytest.fail("controller did not leave positive saturation")

    assert request.value == pytest.approx(0.0)


def test_snapshot_restore_round_trip_preserves_ten_subsequent_outputs():
    period_s = 1e-3
    parameters = {
        "gain": 1.0,
        "zero1_hz": 100.0,
        "zero2_hz": 500.0,
        "pole1_hz": 2000.0,
        "pole2_hz": 5000.0,
    }
    measurements = [1.0, 0.9, 0.7, 0.8, 1.1, 1.2, 1.0, 0.6, 0.9, 1.3, 1.1, 0.8, 1.0, 1.2, 0.7]
    original = Type3Controller.from_config(parameters, control_period_s=period_s)
    for step, measurement in enumerate(measurements[:5]):
        _observe(original, measurement, 1.0, step * period_s, period_s)
    checkpoint = original.snapshot()

    resumed = Type3Controller.from_config(parameters, control_period_s=period_s)
    resumed.restore(checkpoint)

    for step, measurement in enumerate(measurements[5:], start=5):
        expected = _observe(original, measurement, 1.0, step * period_s, period_s)
        actual = _observe(resumed, measurement, 1.0, step * period_s, period_s)
        assert actual.value == pytest.approx(expected.value)
        assert actual.state_update == pytest.approx(expected.state_update)


def test_restoring_a_snapshot_from_a_different_law_is_refused():
    parameters = {
        "gain": 1.0,
        "zero1_hz": 100.0,
        "zero2_hz": 500.0,
        "pole1_hz": 2000.0,
        "pole2_hz": 5000.0,
    }
    original = Type3Controller.from_config(parameters, control_period_s=1e-3)
    checkpoint = original.snapshot()

    different = Type3Controller.from_config(
        {**parameters, "gain": 2.0}, control_period_s=1e-3
    )
    with pytest.raises(ValueError, match="coefficients"):
        different.restore(checkpoint)


def test_discrete_compensator_capabilities_declare_discrete_control():
    controller = DiscreteCompensator([1.0, 0.0], [0.0])

    assert controller.capabilities() == {"discrete_control"}


# ---------------------------------------------------------------------------
# CLI end to end
# ---------------------------------------------------------------------------


def _experiment(tmp_path: Path, controller_id: str, controller_config: dict) -> Path:
    document = {
        "schema_version": "0.1",
        "experiment_id": "controller_registry",
        "run_id": f"{controller_id}-cli",
        "plant_id": "fake",
        "controller_id": controller_id,
        "plant_config": {},
        "controller_config": controller_config,
        "timebase": {"unit": "s", "control_period_s": 0.001, "duration_s": 0.1},
        "initial_state": {"mode": "cold_start"},
        "input_schedule": [{"time_s": 0.0, "vref": 1.0}],
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
    path = tmp_path / f"{controller_id}.json"
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    return path


def test_cli_runs_a_type2_controller_and_records_its_coefficients(
    tmp_path, capsys, monkeypatch
):
    config = _experiment(
        tmp_path,
        "type2",
        {
            "gain": 2.0,
            "zero_hz": 200.0,
            "pole_hz": 2000.0,
            "output_min": 0.0,
            "output_max": 1.0,
        },
    )
    monkeypatch.chdir(tmp_path)

    assert main(["run", str(config)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] in {"RUN_OK", "QUALIFIED"}

    manifest = json.loads(
        (tmp_path / "runs" / "type2-cli" / "manifest.json").read_text(encoding="utf-8")
    )
    identity = manifest["controller"]["identity"]
    configuration = identity.get("configuration", {})
    assert identity["class"] == "Type2Controller"
    assert configuration["gain"] == pytest.approx(2.0)
    assert configuration["zero_hz"] == pytest.approx(200.0)
    assert configuration["pole_hz"] == pytest.approx(2000.0)
    assert len(configuration["coefficients"]["b"]) == 3
    assert len(configuration["coefficients"]["a"]) == 2

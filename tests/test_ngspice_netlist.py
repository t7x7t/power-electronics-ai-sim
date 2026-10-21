"""Deterministic netlist generation tests; no ngspice installation required."""

from __future__ import annotations

import re

import pytest

from pe_sim.backends.netlist import (
    NETLIST_TEMPLATE_VERSION,
    BuckNetlistParameters,
    NetlistParameterError,
    default_sample_step_s,
    netlist_digest,
    render_buck_netlist,
)
from pe_sim.backends.settings import NgspiceSolverSettings


def _parameters(**overrides):
    values = {
        "input_voltage_v": 12.0,
        "inductance_h": 100e-6,
        "capacitance_f": 470e-6,
        "load_resistance_ohm": 10.0,
        "switching_frequency_hz": 100e3,
    }
    values.update(overrides)
    return BuckNetlistParameters(**values)


def test_render_is_deterministic_and_contains_reviewed_elements():
    first = render_buck_netlist(_parameters(), duty=0.4, duration_s=10e-6, solver=NgspiceSolverSettings())
    second = render_buck_netlist(_parameters(), duty=0.4, duration_s=10e-6, solver=NgspiceSolverSettings())

    assert first == second
    assert netlist_digest(first) == netlist_digest(second)
    assert NETLIST_TEMPLATE_VERSION in first
    for token in (
        "V1 vin 0 DC {Vin}",
        "S1 vin sw pwm 0 SWMOD",
        "D1 0 sw DMOD",
        "L1 sw vout {L} IC={IL0}",
        "C1 vout 0 {C} IC={VC0}",
        "R1 vout 0 {Rload}",
        ".tran",
        "uic",
        "wrdata output.dat v(vout) i(L1) v(sw)",
    ):
        assert token in first


def test_duty_changes_identity_and_pulse_width():
    low = render_buck_netlist(_parameters(), duty=0.2, duration_s=10e-6, solver=NgspiceSolverSettings())
    high = render_buck_netlist(_parameters(), duty=0.8, duration_s=10e-6, solver=NgspiceSolverSettings())

    assert netlist_digest(low) != netlist_digest(high)
    assert ".param D=2.000000000000e-01" in low
    assert ".param D=8.000000000000e-01" in high


def test_initial_conditions_are_rendered():
    text = render_buck_netlist(
        _parameters(initial_inductor_current_a=1.25, initial_output_voltage_v=3.5),
        duty=0.5,
        duration_s=10e-6,
        solver=NgspiceSolverSettings(),
    )

    assert ".param IL0=1.250000000000e+00" in text
    assert ".param VC0=3.500000000000e+00" in text


def test_netlist_contains_no_local_path_or_timestamp():
    text = render_buck_netlist(_parameters(), duty=0.5, duration_s=10e-6, solver=NgspiceSolverSettings())

    assert "D:\\" not in text
    assert "C:\\" not in text
    assert not re.search(r"\b20\d\d-\d\d-\d\d\b", text)


def test_invalid_electrical_parameters_fail_closed():
    with pytest.raises(NetlistParameterError):
        _parameters(input_voltage_v=0.0)
    with pytest.raises(NetlistParameterError):
        _parameters(inductance_h=-1e-6)
    with pytest.raises(NetlistParameterError):
        _parameters(switching_frequency_hz=float("nan"))


def test_invalid_duty_duration_and_step_fail_closed():
    parameters = _parameters()
    with pytest.raises(NetlistParameterError):
        render_buck_netlist(parameters, duty=1.2, duration_s=10e-6, solver=NgspiceSolverSettings())
    with pytest.raises(NetlistParameterError):
        render_buck_netlist(parameters, duty=-0.1, duration_s=10e-6, solver=NgspiceSolverSettings())
    with pytest.raises(NetlistParameterError):
        render_buck_netlist(parameters, duty=0.5, duration_s=0.0, solver=NgspiceSolverSettings())
    with pytest.raises(NetlistParameterError):
        render_buck_netlist(
            parameters,
            duty=0.5,
            duration_s=10e-6,
            solver=NgspiceSolverSettings(),
            sample_step_s=0.0,
        )


def test_default_sample_step_respects_the_window_and_switching_period():
    parameters = _parameters(switching_frequency_hz=100e3)
    step = default_sample_step_s(parameters, 10e-6)

    assert 0 < step <= 10e-7
    assert step <= parameters.switching_period_s / 200.0 + 1e-15


def test_solver_settings_are_rendered_explicitly():
    solver = NgspiceSolverSettings(integration_method="trap", reltol=1e-4, temperature_c=50.0, num_digits=10)
    text = render_buck_netlist(_parameters(), duty=0.5, duration_s=10e-6, solver=solver)

    assert "reltol=0.0001" in text
    assert "method=trap" in text
    assert ".temp 5.000000000000e+01" in text
    assert "set numdgt=10" in text

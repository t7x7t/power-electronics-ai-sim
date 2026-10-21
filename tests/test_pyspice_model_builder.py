"""Optional PySpice model-builder tests.

The PySpice-dependent test is skipped unless PySpice is installed.  The core
test suite therefore runs without the GPLv3 optional dependency.
"""

from __future__ import annotations

import importlib.util

import pytest

from pe_sim.backends.netlist import BuckNetlistParameters
from pe_sim.backends.netlist_builder import (
    NativeBuckNetlistBuilder,
    PyspiceBuckNetlistBuilder,
    PyspiceUnavailableError,
)
from pe_sim.backends.settings import NgspiceSolverSettings


def _parameters() -> BuckNetlistParameters:
    return BuckNetlistParameters(
        input_voltage_v=12.0,
        inductance_h=100e-6,
        capacitance_f=470e-6,
        load_resistance_ohm=10.0,
        switching_frequency_hz=100e3,
        initial_inductor_current_a=0.45,
        initial_output_voltage_v=6.0,
    )


def test_native_builder_is_deterministic():
    builder = NativeBuckNetlistBuilder()
    first = builder.render(
        _parameters(),
        duty=0.5,
        duration_s=10e-6,
        solver=NgspiceSolverSettings(),
    )
    second = builder.render(
        _parameters(),
        duty=0.5,
        duration_s=10e-6,
        solver=NgspiceSolverSettings(),
    )

    assert first == second
    assert builder.provenance()["execution"] == "external-ngspice-process"


def test_missing_pyspice_is_reported_explicitly():
    if importlib.util.find_spec("PySpice") is not None:
        pytest.skip("PySpice is installed in this environment")

    builder = PyspiceBuckNetlistBuilder()
    with pytest.raises(PyspiceUnavailableError):
        builder.check_available()
    with pytest.raises(PyspiceUnavailableError):
        builder.render(
            _parameters(),
            duty=0.5,
            duration_s=10e-6,
            solver=NgspiceSolverSettings(),
        )


def test_pyspice_builder_renders_the_reviewed_circuit():
    pytest.importorskip("PySpice")
    builder = PyspiceBuckNetlistBuilder()

    text = builder.render(
        _parameters(),
        duty=0.5,
        duration_s=10e-6,
        solver=NgspiceSolverSettings(),
    )

    assert "S1 vin sw pwm 0 SWMOD" in text
    assert "D1 0 sw DMOD" in text
    assert "L1 sw vout" in text
    assert "C1 vout 0" in text
    assert "ic=0.45" in text
    assert "ic=0.45A" not in text
    assert ".tran" in text and "uic" in text
    assert "wrdata output.dat v(vout) i(L1) v(sw)" in text
    provenance = builder.provenance()
    assert provenance["pyspice_version"]
    assert provenance["execution"] == "external-ngspice-process"
    assert provenance["native_pyspice_execution"] is False

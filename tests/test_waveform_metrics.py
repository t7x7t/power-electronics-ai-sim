"""Performance numbers measured from a solved window.

Two kinds of test.  The synthetic ones pin the arithmetic of the energy balance
exactly, because a real solver cannot be asked for a known answer.  The gated
one pins the physics: a switch with more resistance must measure more loss.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from pe_sim.circuit_compile import compile_topology
from pe_sim.topology import ngspice_buck_topology_descriptor
from pe_sim.topology_validation import build_descriptor
from pe_sim.waveform_metrics import (
    WAVEFORM_METRICS_SCHEMA,
    summarise_power,
    waveform_metrics,
)


NGSPICE = os.environ.get("PE_SIM_NGSPICE")
needs_ngspice = pytest.mark.skipif(
    not NGSPICE,
    reason="set PE_SIM_NGSPICE to run the measured-power acceptance test",
)

VIN = 12.0
L_H = 100e-6
C_F = 470e-6
R_OHM = 10.0
FSW = 100e3
DUTY = 0.5


@dataclass(frozen=True)
class _Table:
    """The shape ``waveform_metrics`` expects: a time column and named signals."""

    time_s: tuple[float, ...]
    signals: dict

    @property
    def points(self) -> int:
        return len(self.time_s)


def _table(duration: float, samples: int, **signals) -> _Table:
    times = tuple(duration * index / (samples - 1) for index in range(samples))
    return _Table(time_s=times, signals={name: tuple(values) for name, values in signals.items()})


def _descriptor(**overrides) -> dict:
    plant = SimpleNamespace(
        input_voltage_v=VIN,
        inductance_h=L_H,
        capacitance_f=C_F,
        load_resistance_ohm=R_OHM,
        switching_frequency_hz=FSW,
        control_period_s=10e-6,
        dead_time_s=100e-9,
        **overrides,
    )
    return ngspice_buck_topology_descriptor(plant).to_dict()


def _element_names() -> dict[str, str]:
    """The names the compiler assigns, read from a real compilation."""

    compiled = compile_topology(
        build_descriptor(_descriptor()), duty=DUTY, duration_s=1e-3
    )
    return dict(compiled.element_names)


# ---------------------------------------------------------------------------
# Arithmetic: the balance must close exactly on constructed numbers
# ---------------------------------------------------------------------------


def test_the_energy_balance_is_an_identity():
    """input = output + storage + loss, on numbers chosen so it can be checked."""

    samples = 11
    times = tuple(2e-3 * index / (samples - 1) for index in range(samples))
    table = _Table(
        time_s=times,
        signals={
            "v(vin)": tuple(VIN for _ in times),
            "v(vout)": tuple(6.0 for _ in times),
            "v(pwm)": tuple(1.0 for _ in times),
            # i(V1) is positive into the source, so a delivered 0.5 A reads -0.5.
            "i(v1)": tuple(-0.5 for _ in times),
            "i(l1)": tuple(0.5 for _ in times),
            "i(v2)": tuple(0.0 for _ in times),
        },
    )
    document = _descriptor()
    descriptor = build_descriptor(document)

    metrics = waveform_metrics(
        descriptor,
        table,
        element_names={"vin_source": "V1", "pwm_source": "V2", "inductor": "L1"},
    )
    power = metrics["power"]

    assert metrics["schema"] == WAVEFORM_METRICS_SCHEMA
    assert power["input_w"]["value"] == pytest.approx(VIN * 0.5)
    assert power["output_w"]["value"] == pytest.approx(6.0**2 / R_OHM)
    assert power["storage_rate_w"]["value"] == pytest.approx(0.0)
    assert power["loss_w"]["value"] == pytest.approx(VIN * 0.5 - 6.0**2 / R_OHM)
    assert power["efficiency"]["value"] == pytest.approx(
        (6.0**2 / R_OHM) / (VIN * 0.5)
    )
    assert metrics["quality"]["window_is_operating_point"] is True
    assert metrics["quality"]["loss_is_physical"] is True


def test_a_window_that_fills_a_capacitor_is_not_an_operating_point():
    """A charging capacitor is not loss, and the efficiency is withheld."""

    samples = 11
    times = tuple(2e-3 * index / (samples - 1) for index in range(samples))
    voltage = tuple(6.0 * index / (samples - 1) for index in range(samples))
    table = _Table(
        time_s=times,
        signals={
            "v(vin)": tuple(VIN for _ in times),
            "v(vout)": voltage,
            "i(v1)": tuple(-0.6 for _ in times),
        },
    )
    metrics = waveform_metrics(
        build_descriptor(_descriptor()),
        table,
        element_names={"vin_source": "V1"},
    )

    stored = 0.5 * C_F * 6.0**2
    assert metrics["storage"]["change_j"] == pytest.approx(stored, rel=1e-9)
    assert metrics["storage"]["rate_w"] == pytest.approx(stored / 2e-3)
    assert metrics["quality"]["window_is_operating_point"] is False
    assert metrics["power"]["efficiency"]["value"] is None
    assert "not an operating point" in metrics["power"]["efficiency"]["reason"]
    # The loss is still reported, and it is smaller than input minus output.
    assert metrics["power"]["loss_w"]["value"] == pytest.approx(
        VIN * 0.6 - sum(v * v / R_OHM for v in voltage) / samples - stored / 2e-3
    )


def test_a_missing_signal_is_a_reason_not_a_zero():
    samples = 5
    times = tuple(1e-3 * index / (samples - 1) for index in range(samples))
    table = _Table(
        time_s=times,
        signals={
            "v(vin)": tuple(VIN for _ in times),
            "v(vout)": tuple(6.0 for _ in times),
        },
    )
    metrics = waveform_metrics(
        build_descriptor(_descriptor()), table, element_names={"vin_source": "V1"}
    )

    assert metrics["power"]["input_w"]["value"] is None
    # The per-source entry carries the detail; the aggregate only says it is
    # incomplete, because a client should read the component list to find out why.
    assert "not in this table" in metrics["sources"][0]["power_w"]["reason"]
    assert metrics["power"]["efficiency"]["value"] is None
    # A capacitor's energy needs only its terminal voltages, which *are* here; an
    # inductor's needs its element current, which is not.
    assert metrics["storage"]["unmeasured"] == ["inductor"]
    assert metrics["storage"]["components"][1]["energy_j"] == pytest.approx(
        0.5 * C_F * 6.0**2
    )


def test_the_observed_duty_counts_samples_above_half_the_amplitude():
    samples = 11
    times = tuple(1e-5 * index / (samples - 1) for index in range(samples))
    high = [1.0] * 6 + [0.0] * 5
    table = _Table(
        time_s=times,
        signals={
            "v(vin)": tuple(VIN for _ in times),
            "v(vout)": tuple(6.0 for _ in times),
            "v(pwm)": tuple(high),
        },
    )
    metrics = waveform_metrics(
        build_descriptor(_descriptor()), table, element_names={"vin_source": "V1"}
    )

    duty = metrics["carriers"][0]
    assert duty["observed_duty"]["value"] == pytest.approx(6 / 11)
    assert duty["switching_frequency_hz"] == pytest.approx(FSW)
    assert duty["switching_period_s"] == pytest.approx(1.0 / FSW)


def test_the_summary_keeps_unmeasured_visible():
    metrics = waveform_metrics(
        build_descriptor(_descriptor()),
        _Table(
            time_s=(0.0, 1e-3),
            signals={"v(vin)": (VIN, VIN), "v(vout)": (6.0, 6.0)},
        ),
        element_names={"vin_source": "V1"},
    )

    text = summarise_power(metrics["power"])

    assert "input=unmeasured" in text
    assert "efficiency=unmeasured" in text


def test_a_descriptor_without_a_reference_net_is_refused_cleanly():
    document = _descriptor()
    for net in document["nets"]:
        if net["kind"] == "reference":
            net["kind"] = "electrical"

    metrics = waveform_metrics(
        build_descriptor(document),
        _Table(time_s=(0.0, 1e-3), signals={"v(vin)": (VIN, VIN)}),
        element_names={"vin_source": "V1"},
    )

    assert metrics["ok"] is False
    assert "reference net" in metrics["reason"]
    assert metrics["power"]["input_w"]["value"] is None


# ---------------------------------------------------------------------------
# Physics: more switch resistance must measure more loss
# ---------------------------------------------------------------------------


def _solve(netlist: str) -> _Table:
    work = Path(tempfile.mkdtemp(prefix="pe-sim-metrics-"))
    (work / "circuit.cir").write_text(netlist, encoding="ascii")
    completed = subprocess.run(
        [str(NGSPICE), "-b", "circuit.cir"],
        cwd=work,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, (completed.stderr or completed.stdout)[-2000:]
    rows = [
        line.split()
        for line in (work / "output.dat").read_text(encoding="ascii").splitlines()
        if line.strip()
    ]
    header = [name.lower() for name in rows[0]]
    times = tuple(float(row[0]) for row in rows[1:] if row)
    signals = {
        name: tuple(float(row[index]) for row in rows[1:] if row)
        for index, name in enumerate(header)
        if index > 0
    }
    return _Table(time_s=times, signals=signals)


def _measure(ron: float | None) -> dict:
    document = _descriptor()
    descriptor = build_descriptor(document)
    switch = next(
        component.component_id
        for component in descriptor.components
        if component.kind == "voltage_controlled_switch"
    )
    compiled = compile_topology(
        descriptor,
        duty=DUTY,
        duration_s=200e-6,
        initial_conditions={"inductor": 0.62, "capacitor": 5.95},
        parameter_overrides=(
            None if ron is None else {switch: {"on_resistance_ohm": ron}}
        ),
    )
    return waveform_metrics(
        descriptor, _solve(compiled.netlist), element_names=dict(compiled.element_names)
    )


@needs_ngspice
def test_a_more_resistive_switch_measures_more_loss():
    """The measurement must respond to a real physical change, not to a formula."""

    ideal = _measure(None)
    lossy = _measure(0.5)

    ideal_loss = ideal["power"]["loss_w"]["value"]
    lossy_loss = lossy["power"]["loss_w"]["value"]

    assert lossy_loss - ideal_loss > 0.05
    assert ideal["carriers"][0]["observed_duty"]["value"] == pytest.approx(
        DUTY, abs=0.01
    )
    # The balance is an identity on real data too.
    for metrics in (ideal, lossy):
        power = metrics["power"]
        assert power["input_w"]["value"] == pytest.approx(
            power["output_w"]["value"]
            + power["storage_rate_w"]["value"]
            + power["loss_w"]["value"]
        )


@needs_ngspice
def test_an_operating_point_window_publishes_an_efficiency_below_one():
    metrics = _measure(0.5)

    efficiency = metrics["power"]["efficiency"]
    assert metrics["quality"]["window_is_operating_point"] is True
    assert efficiency["value"] is not None
    assert 0.0 < efficiency["value"] < 1.0

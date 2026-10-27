"""Deterministic synchronous-Buck netlist and ngspice plant adapter.

The reference circuit has a high-side and a low-side ideal switch, an inductor,
a capacitor and a resistive load.  There is deliberately no freewheeling diode:
the low-side switch is the synchronous rectifier.  Both gates are driven by
explicit absolute-time PWL voltage sources, so a solve segment may start at any
carrier phase and the dead time is visible in the rendered text rather than
implied by a PULSE source.

This is a switching-level reference adapter.  It is not a device, thermal, loss,
gate-drive, or product model, and it carries no claim of device-level accuracy.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import inspect
import json
import math
from pathlib import Path
import sys
from typing import Any, Callable, Mapping

from ..contracts import PlantObservation
from ..identity import (
    implementation_digest,
    implementation_sources,
    register_solve_path,
    review_status,
)
from ..model_claim import (
    ModelClaim,
    NumericalLimit,
    OperatingRange,
    ParameterProvenance,
    ParameterRecord,
)
from .netlist import (
    BuckNetlistParameters,
    NetlistParameterError,
    default_sample_step_s,
)
from .ngspice_plants import NgspiceBuckPlant
from .process import NgspiceRunResult
from .settings import NgspiceSolverSettings
from .wrdata import TraceTable


SYNC_NETLIST_TEMPLATE_VERSION = "ngspice-sync-buck-v1"

_EDGE_S = 1e-9
_IL_SIGNALS = ("i(l1)", "l1#branch", "il")

# Detection thresholds for classify_conduction_regime().  They are classifier
# settings, not device physics: 1e-9 A decides "at zero", 1e-6 A decides
# "meaningfully active", and 1e-9 in duty decides "the carrier is not driving".
_ZERO_CURRENT_A = 1e-9
_ACTIVE_CURRENT_A = 1e-6
_DUTY_EPSILON = 1e-9
#: A trace that starts one solver step after t=0 (or ends one step early) still
#: describes almost a full switching cycle.  Accept a tail that covers at least
#: this fraction of a period; rejecting it would label a complete window as
#: ``unknown`` for a sub-nanosecond boundary offset.
_PARTIAL_CYCLE_FRACTION = 0.05


def _positive(name: str, value: float) -> float:
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise NetlistParameterError(f"{name} must be finite and positive")
    return number


def _non_negative(name: str, value: float) -> float:
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise NetlistParameterError(f"{name} must be finite and non-negative")
    return number


def _format(value: float) -> str:
    """Stable scientific formatting shared by every generated netlist line."""

    return f"{float(value):.12e}"


@dataclass(frozen=True)
class SyncBuckNetlistParameters:
    """Electrical parameters of the synchronous-Buck reference.

    The field set mirrors :class:`BuckNetlistParameters`; ``dead_time_s`` is the
    additional timing parameter.  The two diode fields are accepted for
    interface parity but are not rendered: this circuit has no diode.  All
    values are reference-model assumptions, not device or thermal data.
    """

    input_voltage_v: float
    inductance_h: float
    capacitance_f: float
    load_resistance_ohm: float
    switching_frequency_hz: float
    dead_time_s: float
    switch_on_resistance_ohm: float = 1e-3
    switch_off_resistance_ohm: float = 1e9
    diode_saturation_current_a: float = 1e-12
    diode_emission_coefficient: float = 0.02
    initial_inductor_current_a: float = 0.0
    initial_output_voltage_v: float = 0.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "input_voltage_v", _positive("input_voltage_v", self.input_voltage_v))
        object.__setattr__(self, "inductance_h", _positive("inductance_h", self.inductance_h))
        object.__setattr__(self, "capacitance_f", _positive("capacitance_f", self.capacitance_f))
        object.__setattr__(self, "load_resistance_ohm", _positive("load_resistance_ohm", self.load_resistance_ohm))
        object.__setattr__(self, "switching_frequency_hz", _positive("switching_frequency_hz", self.switching_frequency_hz))
        dead_time = _positive("dead_time_s", self.dead_time_s)
        switching_period = 1.0 / self.switching_frequency_hz
        if dead_time >= switching_period / 2.0:
            raise NetlistParameterError("dead_time_s must be smaller than switching_period_s/2")
        object.__setattr__(self, "dead_time_s", dead_time)
        object.__setattr__(self, "switch_on_resistance_ohm", _positive("switch_on_resistance_ohm", self.switch_on_resistance_ohm))
        object.__setattr__(self, "switch_off_resistance_ohm", _positive("switch_off_resistance_ohm", self.switch_off_resistance_ohm))
        object.__setattr__(self, "diode_saturation_current_a", _positive("diode_saturation_current_a", self.diode_saturation_current_a))
        object.__setattr__(self, "diode_emission_coefficient", _positive("diode_emission_coefficient", self.diode_emission_coefficient))
        object.__setattr__(self, "initial_inductor_current_a", _non_negative("initial_inductor_current_a", self.initial_inductor_current_a))
        object.__setattr__(self, "initial_output_voltage_v", _non_negative("initial_output_voltage_v", self.initial_output_voltage_v))

    @property
    def switching_period_s(self) -> float:
        return 1.0 / self.switching_frequency_hz

    def to_dict(self) -> dict[str, Any]:
        return {
            "input_voltage_v": self.input_voltage_v,
            "inductance_h": self.inductance_h,
            "capacitance_f": self.capacitance_f,
            "load_resistance_ohm": self.load_resistance_ohm,
            "switching_frequency_hz": self.switching_frequency_hz,
            "dead_time_s": self.dead_time_s,
            "switch_on_resistance_ohm": self.switch_on_resistance_ohm,
            "switch_off_resistance_ohm": self.switch_off_resistance_ohm,
            "diode_saturation_current_a": self.diode_saturation_current_a,
            "diode_emission_coefficient": self.diode_emission_coefficient,
            "initial_inductor_current_a": self.initial_inductor_current_a,
            "initial_output_voltage_v": self.initial_output_voltage_v,
        }


def _normalise_phase(carrier_phase_s: float, switching_period_s: float) -> float:
    phase = float(carrier_phase_s)
    if not math.isfinite(phase):
        raise NetlistParameterError("carrier_phase_s must be finite")
    phase %= switching_period_s
    # Treat a phase within floating-point noise of one period as zero; leaving
    # it as Tsw would start the waveform one whole period later.
    if switching_period_s - phase <= 1e-12 * max(1.0, switching_period_s):
        phase = 0.0
    return phase


def _gate_value(
    time_s: float,
    *,
    phase_s: float,
    switching_period_s: float,
    hs_width_s: float,
    dead_time_s: float,
    low_side: bool,
) -> float:
    """Evaluate one ideal gate waveform at a local time in [0, duration]."""

    tau = (float(time_s) + phase_s) % switching_period_s
    if switching_period_s - tau <= 1e-15 * max(1.0, switching_period_s):
        tau = 0.0
    if low_side:
        start_s = hs_width_s + dead_time_s
        stop_s = switching_period_s - dead_time_s
    else:
        start_s = 0.0
        stop_s = hs_width_s
    if stop_s <= start_s:
        return 0.0
    rise = min(max((tau - start_s) / _EDGE_S, 0.0), 1.0)
    fall = min(max((stop_s - tau) / _EDGE_S, 0.0), 1.0)
    return min(rise, fall)


def _pwl_breakpoints(
    *,
    phase_s: float,
    switching_period_s: float,
    hs_width_s: float,
    dead_time_s: float,
    duration_s: float,
) -> tuple[float, ...]:
    """Return strictly increasing absolute-time breakpoints covering the window."""

    points: list[float] = [0.0, duration_s]
    first_cycle = math.floor(phase_s / switching_period_s) - 1
    last_cycle = math.ceil((duration_s + phase_s) / switching_period_s) + 1
    for cycle in range(first_cycle, last_cycle + 1):
        start = cycle * switching_period_s - phase_s
        candidates = (
            start,
            start + _EDGE_S,
            start + hs_width_s - _EDGE_S,
            start + hs_width_s,
            start + hs_width_s + dead_time_s,
            start + hs_width_s + dead_time_s + _EDGE_S,
            start + switching_period_s - dead_time_s - _EDGE_S,
            start + switching_period_s - dead_time_s,
            start + switching_period_s,
        )
        for candidate in candidates:
            if -1e-15 <= candidate <= duration_s + 1e-15:
                points.append(min(max(candidate, 0.0), duration_s))
    points.sort()
    unique: list[float] = []
    for point in points:
        if not unique or point > unique[-1] + 1e-15:
            unique.append(point)
    if len(unique) < 2:
        raise NetlistParameterError("duration_s is below the template time resolution")
    # The endpoint must remain exact even if the dedupe threshold swallowed it.
    if unique[-1] != duration_s:
        unique[-1] = duration_s
    return tuple(unique)


def _pwl_lines(
    prefix: str,
    points: tuple[float, ...],
    values: list[float],
    *,
    pairs_per_line: int = 6,
) -> list[str]:
    """Render a wrapped SPICE PWL expression using ``+`` continuations."""

    pairs = [f"{_format(time_s)} {_format(value)}" for time_s, value in zip(points, values)]
    lines: list[str] = []
    for index in range(0, len(pairs), pairs_per_line):
        chunk = " ".join(pairs[index : index + pairs_per_line])
        lines.append(f"{prefix}{chunk}" if index == 0 else f"+ {chunk}")
    lines[-1] += ")"
    return lines


def render_sync_buck_netlist(
    parameters: SyncBuckNetlistParameters,
    *,
    duty: float,
    duration_s: float,
    solver: NgspiceSolverSettings,
    sample_step_s: float | None = None,
    carrier_phase_s: float = 0.0,
) -> str:
    """Render one zero-order-hold synchronous-Buck solve segment.

    Both gates are explicit absolute-time PWL sources.  For every switching
    cycle the high-side on-width is ``max(0, duty*Tsw - dead_time)`` and the
    low-side on-width is ``max(0, Tsw - 2*dead_time - hs_width)``.  The intervals
    ``[hs_width, hs_width + dead_time)`` and ``[Tsw - dead_time, Tsw)`` have both
    gates low.  Every gate edge is a 1 ns linear transition, represented as two
    PWL breakpoints.

    ``carrier_phase_s`` is the phase of the global carrier at the segment start
    (the segment start modulo the switching period).  The PWL sources therefore
    carry the correct initial value and the remaining part of a pulse that began
    before the segment; no PULSE source and no integer control-period ratio is
    involved.
    """

    if not isinstance(parameters, SyncBuckNetlistParameters):
        raise NetlistParameterError("parameters must be SyncBuckNetlistParameters")
    duty_value = float(duty)
    if not math.isfinite(duty_value) or not 0.0 <= duty_value <= 1.0:
        raise NetlistParameterError("duty must be finite and in [0, 1]")
    duration = _positive("duration_s", duration_s)
    if not isinstance(solver, NgspiceSolverSettings):
        raise NetlistParameterError("solver must be NgspiceSolverSettings")

    switching_period = parameters.switching_period_s
    phase = _normalise_phase(carrier_phase_s, switching_period)
    hs_width = max(0.0, duty_value * switching_period - parameters.dead_time_s)
    points = _pwl_breakpoints(
        phase_s=phase,
        switching_period_s=switching_period,
        hs_width_s=hs_width,
        dead_time_s=parameters.dead_time_s,
        duration_s=duration,
    )
    hs_values = [
        _gate_value(
            time_s,
            phase_s=phase,
            switching_period_s=switching_period,
            hs_width_s=hs_width,
            dead_time_s=parameters.dead_time_s,
            low_side=False,
        )
        for time_s in points
    ]
    ls_values = [
        _gate_value(
            time_s,
            phase_s=phase,
            switching_period_s=switching_period,
            hs_width_s=hs_width,
            dead_time_s=parameters.dead_time_s,
            low_side=True,
        )
        for time_s in points
    ]

    step = (
        default_sample_step_s(parameters, duration)
        if sample_step_s is None
        else _positive("sample_step_s", sample_step_s)
    )
    step = min(step, duration / 10.0)

    lines: list[str] = [
        "* pe-sim ngspice synchronous buck reference",
        f"* template: {SYNC_NETLIST_TEMPLATE_VERSION}",
        f".param Vin={_format(parameters.input_voltage_v)}",
        f".param Tsw={_format(switching_period)}",
        f".param D={_format(duty_value)}",
        f".param DT={_format(parameters.dead_time_s)}",
        f".param L={_format(parameters.inductance_h)}",
        f".param C={_format(parameters.capacitance_f)}",
        f".param Rload={_format(parameters.load_resistance_ohm)}",
        f".param Ron={_format(parameters.switch_on_resistance_ohm)}",
        f".param Roff={_format(parameters.switch_off_resistance_ohm)}",
        f".param IL0={_format(parameters.initial_inductor_current_a)}",
        f".param VC0={_format(parameters.initial_output_voltage_v)}",
        "",
        "V1 vin 0 DC {Vin}",
        *_pwl_lines("Vghs hs_gate 0 PWL(", points, hs_values),
        *_pwl_lines("Vgls ls_gate 0 PWL(", points, ls_values),
        "S1 vin sw hs_gate 0 SWMOD",
        "S2 sw 0 ls_gate 0 SWMOD",
        "L1 sw vout {L} IC={IL0}",
        "C1 vout 0 {C} IC={VC0}",
        "R1 vout 0 {Rload}",
        ".model SWMOD SW(Ron={Ron} Roff={Roff} Vt=0.5 Vh=0)",
        solver.options_line(),
        f".temp {_format(solver.temperature_c)}",
        f".tran {_format(step)} {_format(duration)} 0 {_format(step)} uic",
        ".control",
        "set filetype=ascii",
        "set wr_singlescale",
        "set wr_vecnames",
        f"set numdgt={solver.num_digits}",
        "run",
        "wrdata output.dat v(vout) i(L1) v(sw)",
        ".endc",
        ".end",
        "",
    ]
    return "\n".join(lines)


@dataclass(frozen=True)
class NativeSyncBuckNetlistBuilder:
    """Project-owned deterministic synchronous-Buck netlist renderer."""

    dead_time_s: float
    name: str = "native-sync-netlist"

    def __post_init__(self) -> None:
        object.__setattr__(self, "dead_time_s", _positive("dead_time_s", self.dead_time_s))

    def render(
        self,
        parameters: BuckNetlistParameters,
        *,
        duty: float,
        duration_s: float,
        solver: NgspiceSolverSettings,
        sample_step_s: float | None = None,
        carrier_phase_s: float = 0.0,
    ) -> str:
        if not isinstance(parameters, SyncBuckNetlistParameters):
            raise NetlistParameterError("parameters must be SyncBuckNetlistParameters")
        tolerance = 1e-15 * max(1.0, self.dead_time_s)
        if abs(parameters.dead_time_s - self.dead_time_s) > tolerance:
            raise NetlistParameterError("builder dead_time_s does not match the netlist parameters")
        return render_sync_buck_netlist(
            parameters,
            duty=duty,
            duration_s=duration_s,
            solver=solver,
            sample_step_s=sample_step_s,
            carrier_phase_s=carrier_phase_s,
        )

    def provenance(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "template": SYNC_NETLIST_TEMPLATE_VERSION,
            "execution": "external-ngspice-process",
            "dead_time_s": self.dead_time_s,
        }


class _RegimeLabel(str):
    """Categorical label stored in the observation's numeric truth mapping.

    ``PlantObservation.truth`` is a stable ``Mapping[str, float]`` and its
    constructor rejects non-numeric values.  The conduction regime is a
    categorical fact, not a physical quantity, so this string subclass keeps the
    label text for equality, ``str()`` and JSON while providing a numeric
    projection for the existing finiteness check.  It encodes no measurement.
    """

    def __float__(self) -> float:
        return 0.0


def _classify_cycle(values: list[float]) -> str:
    peak = max(values)
    trough = min(values)
    magnitude = max(abs(peak), abs(trough))
    if magnitude <= _ZERO_CURRENT_A:
        return "idle"
    if trough < -_ZERO_CURRENT_A:
        return "forced_ccm"
    if peak > _ACTIVE_CURRENT_A and trough <= _ZERO_CURRENT_A:
        return "dcm"
    if trough > _ZERO_CURRENT_A:
        return "ccm"
    return "unknown"


def classify_conduction_regime(
    table: TraceTable,
    *,
    switching_period_s: float,
    duty: float,
    dead_time_s: float = 0.0,
) -> str:
    """Classify the inductor-current regime of one solved window.

    The trace is split into consecutive switching-period chunks starting at the
    first printed sample.  Only chunks with at least two samples are used.  The
    result is one of ``ccm``, ``dcm``, ``forced_ccm``, ``idle``, ``burst`` or
    ``unknown``:

    - ``idle``: the duty is effectively zero, or every used current is at zero;
    - ``forced_ccm``: at least one sample is negative beyond the zero threshold,
      which is possible in a synchronous rectifier when it sinks reverse current;
    - ``dcm``: the current touches the zero plateau and still has an active peak;
    - ``ccm``: every used current stays above the zero threshold;
    - ``burst``: the same trace contains both idle and active switching cycles;
    - ``unknown``: the signal is absent, the window is shorter than one switching
      period, or no complete chunk has enough samples.

    The thresholds are explicit detection settings, not device physics:
    ``1e-9 A`` decides "at zero", ``1e-6 A`` decides "meaningfully active", and
    ``1e-9`` in duty decides that the carrier is not driving.  A different
    threshold policy would produce a different label for the same waveform; the
    label is therefore an observation, not a calibrated measurement.
    """

    period = float(switching_period_s)
    if not math.isfinite(period) or period <= 0:
        raise ValueError("switching_period_s must be finite and positive")
    duty_value = float(duty)
    if not math.isfinite(duty_value) or not 0.0 <= duty_value <= 1.0:
        raise ValueError("duty must be finite and in [0, 1]")
    dead_time = float(dead_time_s)
    if not math.isfinite(dead_time) or dead_time < 0:
        raise ValueError("dead_time_s must be finite and non-negative")
    if dead_time >= period / 2.0:
        raise ValueError("dead_time_s must be smaller than switching_period_s/2")

    signal = next((name for name in _IL_SIGNALS if name in table.signals), None)
    if signal is None:
        return "unknown"
    times = table.time_s
    values = table.signals[signal]
    if len(times) < 2:
        return "unknown"
    if duty_value <= _DUTY_EPSILON:
        return "idle"
    start_s = times[0]
    end_s = times[-1]
    tolerance = 1e-12 * max(1.0, period)
    if end_s - start_s < period * (1.0 - _PARTIAL_CYCLE_FRACTION):
        return "unknown"

    chunks: dict[int, list[float]] = {}
    for time_s, value in zip(times, values):
        index = math.floor((time_s - start_s) / period)
        chunks.setdefault(index, []).append(value)

    cycle_regimes: list[str] = []
    for index, chunk_values in chunks.items():
        chunk_end = start_s + (index + 1) * period
        if chunk_end > end_s + tolerance:
            covered = end_s - (chunk_end - period)
            if covered < period * (1.0 - _PARTIAL_CYCLE_FRACTION):
                continue
        if len(chunk_values) < 2:
            continue
        cycle_regimes.append(_classify_cycle(chunk_values))

    if not cycle_regimes:
        return "unknown"
    if all(regime == "idle" for regime in cycle_regimes):
        return "idle"
    if "idle" in cycle_regimes and any(regime != "idle" for regime in cycle_regimes):
        return "burst"
    if "forced_ccm" in cycle_regimes:
        return "forced_ccm"
    if all(regime == "ccm" for regime in cycle_regimes):
        return "ccm"
    if all(regime == "dcm" for regime in cycle_regimes):
        return "dcm"
    if "dcm" in cycle_regimes:
        # A mixed CCM/DCM window is reported as the lighter observed regime
        # rather than claiming continuous conduction everywhere.
        return "dcm"
    return "unknown"


class NgspiceSyncBuckPlant(NgspiceBuckPlant):
    """Synchronous-Buck switching reference solved by ngspice.

    The model has ideal high-side and low-side switches, explicit dead time on
    both transitions, no freewheeling diode, no gate-drive resistance and no
    device parasitics.  Dead time is a timing parameter, not a body-diode model.
    """

    topology = "buck"
    model_kind = "ngspice_sync_buck_reference"
    model_level = "L2"
    backend_id = "ngspice-sync-buck"

    def __init__(
        self,
        *,
        dead_time_s: float,
        input_voltage_v: float = 12.0,
        inductance_h: float = 100e-6,
        capacitance_f: float = 470e-6,
        load_resistance_ohm: float = 10.0,
        switching_frequency_hz: float = 100e3,
        control_period_s: float = 10e-6,
        switch_on_resistance_ohm: float = 1e-3,
        switch_off_resistance_ohm: float = 1e9,
        diode_saturation_current_a: float = 1e-12,
        diode_emission_coefficient: float = 0.02,
        solver: NgspiceSolverSettings | None = None,
        ngspice_executable: str | Path | None = None,
        timeout_s: float = 30.0,
        sample_step_s: float | None = None,
        keep_artifacts: bool = False,
        work_root: str | Path | None = None,
        solver_callable: Callable[[str], NgspiceRunResult] | None = None,
    ) -> None:
        dead_time = _positive("dead_time_s", dead_time_s)
        frequency = _positive("switching_frequency_hz", switching_frequency_hz)
        switching_period = 1.0 / frequency
        if dead_time >= switching_period / 2.0:
            raise ValueError("dead_time_s must be smaller than switching_period_s/2")
        self.dead_time_s = dead_time
        super().__init__(
            input_voltage_v=input_voltage_v,
            inductance_h=inductance_h,
            capacitance_f=capacitance_f,
            load_resistance_ohm=load_resistance_ohm,
            switching_frequency_hz=frequency,
            control_period_s=control_period_s,
            switch_on_resistance_ohm=switch_on_resistance_ohm,
            switch_off_resistance_ohm=switch_off_resistance_ohm,
            diode_saturation_current_a=diode_saturation_current_a,
            diode_emission_coefficient=diode_emission_coefficient,
            solver=solver,
            ngspice_executable=ngspice_executable,
            timeout_s=timeout_s,
            sample_step_s=sample_step_s,
            keep_artifacts=keep_artifacts,
            work_root=work_root,
            solver_callable=solver_callable,
            netlist_builder=NativeSyncBuckNetlistBuilder(dead_time_s=dead_time),
        )
        self._current_regime = "unknown"
        self._regimes_observed: set[str] = set()
        self._burst_detected = False
        self._sync_identity_cache: dict[str, Any] | None = None

    @property
    def switching_period_s(self) -> float:
        return 1.0 / self.switching_frequency_hz

    # ------------------------------------------------------------------
    # Observation and regime evidence
    # ------------------------------------------------------------------
    def observe(self) -> PlantObservation:
        base = super().observe()
        truth: dict[str, Any] = dict(base.truth)
        truth["conduction_regime"] = _RegimeLabel(self._current_regime)
        return PlantObservation(
            time_s=base.time_s,
            measurement=dict(base.measurement),
            truth=truth,
            age_steps=base.age_steps,
            measurement_units=dict(base.measurement_units),
        )

    def _solve_window(self, duty: float, duration_s: float):
        cache = super()._solve_window(duty, duration_s)
        regime = classify_conduction_regime(
            cache.table,
            switching_period_s=self.switching_period_s,
            duty=duty,
            dead_time_s=self.dead_time_s,
        )
        self._current_regime = regime
        if regime != "unknown":
            self._regimes_observed.add(regime)
        if regime == "burst":
            self._burst_detected = True
        return cache

    def backend_evidence(self) -> dict[str, Any]:
        evidence = super().backend_evidence()
        evidence.update(
            {
                "conduction_regime": self._current_regime,
                "regimes_observed": sorted(self._regimes_observed),
                "burst_detected": self._burst_detected,
                "dead_time_s": self.dead_time_s,
            }
        )
        return evidence

    # ------------------------------------------------------------------
    # Snapshot and identity
    # ------------------------------------------------------------------
    def snapshot(self) -> Mapping[str, Any]:
        snapshot = dict(super().snapshot())
        snapshot["netlist_template_version"] = SYNC_NETLIST_TEMPLATE_VERSION
        snapshot["dead_time_s"] = self.dead_time_s
        return snapshot

    def restore(self, snapshot: Mapping[str, Any]) -> Mapping[str, Any]:
        data = dict(snapshot)
        template = data.pop("netlist_template_version", SYNC_NETLIST_TEMPLATE_VERSION)
        if str(template) != SYNC_NETLIST_TEMPLATE_VERSION:
            raise ValueError("snapshot netlist template version does not match plant")
        dead_time = float(data.pop("dead_time_s", self.dead_time_s))
        if not math.isfinite(dead_time) or abs(dead_time - self.dead_time_s) > 1e-15 * max(1.0, self.dead_time_s):
            raise ValueError("snapshot dead_time_s does not match plant")
        # The parent restore uses NETLIST_TEMPLATE_VERSION as its default.  The
        # template key is removed above after the synchronous check, so the
        # inherited lifecycle logic can validate the remaining fields unchanged.
        result = super().restore(data)
        self._current_regime = "unknown"
        return result

    def manifest_identity(self) -> dict[str, Any]:
        if self._sync_identity_cache is None:
            module = sys.modules[type(self).__module__]
            sources = implementation_sources(self.backend_id)
            digest = implementation_digest(self.backend_id)
            implementation = {
                "module": type(self).__module__,
                "class": type(self).__qualname__,
                "source_sha256": hashlib.sha256(inspect.getsource(module).encode("utf-8")).hexdigest(),
                "netlist_source_sha256": sources.get("sync_buck.py"),
                "solve_path_modules": sources,
                "sha256": digest,
                "review": review_status(self.backend_id, digest),
            }
            backend = self.backend_provenance()
            identity: dict[str, Any] = {
                "level": self.model_level,
                "kind": self.model_kind,
                "backend_id": self.backend_id,
                "topology": self.topology,
                "parameters": {
                    "input_voltage_v": self.input_voltage_v,
                    "inductance_h": self.inductance_h,
                    "capacitance_f": self.capacitance_f,
                    "load_resistance_ohm": self.load_resistance_ohm,
                    "switching_frequency_hz": self.switching_frequency_hz,
                    "control_period_s": self.control_period_s,
                    "switch_on_resistance_ohm": self.switch_on_resistance_ohm,
                    "switch_off_resistance_ohm": self.switch_off_resistance_ohm,
                    "dead_time_s": self.dead_time_s,
                },
                "netlist_template": SYNC_NETLIST_TEMPLATE_VERSION,
                "netlist_builder": self._netlist_builder.provenance(),
                "solver_settings": self.solver.to_dict(),
                "capabilities": sorted(self.capabilities()),
                "implementation": implementation,
                "backend": {
                    "status": backend.get("status"),
                    "version": backend.get("version"),
                    "executable_sha256": backend.get("executable_sha256"),
                },
            }
            identity["sha256"] = hashlib.sha256(
                json.dumps(identity, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
            ).hexdigest()
            self._sync_identity_cache = identity
        return dict(self._sync_identity_cache)

    # ------------------------------------------------------------------
    # Model declaration
    # ------------------------------------------------------------------
    def validity_envelope(self) -> ModelClaim:
        parameters = self._parameters()
        switching_period_s = self.switching_period_s
        effective_step_s = (
            self.sample_step_s
            if self.sample_step_s is not None
            else default_sample_step_s(parameters, self.control_period_s)
        )
        samples_per_switching_period = switching_period_s / effective_step_s
        switching_periods_per_control_period = self.control_period_s * self.switching_frequency_hz
        return ModelClaim(
            claim_level="L2-switching-reference",
            topology=self.topology,
            model_form="switching_netlist",
            assumptions=(
                "ideal voltage-controlled high-side and low-side switches with explicit on/off resistance",
                "complementary gate timing with an explicit dead time on both transitions",
                "no freewheeling diode; reverse inductor current flows through the low-side switch while it is on",
                "1 ns linear gate transitions with no gate-drive resistance or gate charge",
                "linear inductor and capacitor (no saturation, ESR, or ESL)",
                "no switch capacitance, reverse recovery, thermal state, or temperature-dependent device parameters",
                "constant duty ratio within each solved segment; external inputs may change only at declared change points",
            ),
            operating_ranges=(
                OperatingRange("duty", unit="1", minimum=0.0, maximum=1.0, basis="template_domain"),
                OperatingRange("input_voltage_v", unit="V", minimum=0.0, minimum_exclusive=True, basis="equation_domain"),
                OperatingRange("load_resistance_ohm", unit="ohm", minimum=0.0, minimum_exclusive=True, basis="equation_domain"),
                OperatingRange(
                    "dead_time_s",
                    unit="s",
                    minimum=0.0,
                    minimum_exclusive=True,
                    maximum=switching_period_s / 2.0,
                    maximum_exclusive=True,
                    basis="template_domain",
                ),
            ),
            numerical_limits=(
                NumericalLimit(
                    "samples_per_switching_period.min",
                    20.0,
                    "1",
                    strength="advisory",
                    basis="resolution",
                    note="printed wrdata resolution, not solver accuracy",
                ),
                NumericalLimit(
                    "dead_time.edge_ratio",
                    1.0,
                    "1",
                    strength="advisory",
                    basis="resolution",
                    note="gate edges are 1 ns; a dead time near that value is at the edge resolution",
                ),
                NumericalLimit(
                    "load.min_off_resistance_ratio",
                    1e3,
                    "1",
                    strength="advisory",
                    basis="structural",
                    note="load resistance should stay well below the switch off resistance",
                ),
            ),
            limitations=(
                "ideal switches with Ron/Roff, no freewheeling diode, no gate-drive resistance or gate charge",
                "dead time is a timing parameter; no body-diode or reverse-recovery model is present",
                "no thermal model and no temperature coefficients",
                "no ESR/ESL, magnetic saturation, switch capacitance, or device self-heating",
                "restore is state-equivalent, not bit-identical",
                "waveform values between printed wrdata points are linearly interpolated",
            ),
            uncheckable_rules=(
                {"rule_id": "envelope.numerical.max_step", "reason": "solver_option_not_applied_by_netlist_template"},
                {"rule_id": "envelope.thermal.state", "reason": "no_thermal_state_modelled"},
                {"rule_id": "envelope.dead_time.body_diode", "reason": "no_body_diode_model"},
                {"rule_id": "envelope.restore.bit_identical", "reason": "ngspice_solver_state_not_persisted"},
            ),
            actuator={"name": "duty", "unit": "normalized"},
            static_parameters={
                "input_voltage_v": self.input_voltage_v,
                "inductance_h": self.inductance_h,
                "capacitance_f": self.capacitance_f,
                "load_resistance_ohm": self.load_resistance_ohm,
                "switching_frequency_hz": self.switching_frequency_hz,
                "control_period_s": self.control_period_s,
                "switch_on_resistance_ohm": self.switch_on_resistance_ohm,
                "switch_off_resistance_ohm": self.switch_off_resistance_ohm,
                "dead_time_s": self.dead_time_s,
                "switching_period_s": switching_period_s,
                "switching_periods_per_control_period": switching_periods_per_control_period,
                "samples_per_switching_period": samples_per_switching_period,
                "pulse_edge_s": _EDGE_S,
                "load_off_resistance_ratio": self.switch_off_resistance_ohm / self.load_resistance_ohm,
            },
        )

    def parameter_provenance(self) -> ParameterProvenance:
        values = (
            ("input_voltage_v", self.input_voltage_v, "V"),
            ("inductance_h", self.inductance_h, "H"),
            ("capacitance_f", self.capacitance_f, "F"),
            ("load_resistance_ohm", self.load_resistance_ohm, "ohm"),
            ("switching_frequency_hz", self.switching_frequency_hz, "Hz"),
            ("control_period_s", self.control_period_s, "s"),
            ("switch_on_resistance_ohm", self.switch_on_resistance_ohm, "ohm"),
            ("switch_off_resistance_ohm", self.switch_off_resistance_ohm, "ohm"),
            ("dead_time_s", self.dead_time_s, "s"),
        )
        records = tuple(
            ParameterRecord(
                name=name,
                value=value,
                unit=unit,
                source="assumption",
                reference="sync_buck.py constructor/default",
                review_status="unreviewed",
                note="reference-model value; not a device, thermal, or calibrated parameter",
            )
            for name, value, unit in values
        )
        if self.sample_step_s is not None:
            records = records + (
                ParameterRecord(
                    name="sample_step_s",
                    value=self.sample_step_s,
                    unit="s",
                    source="assumption",
                    reference="sync_buck.py constructor",
                    review_status="unreviewed",
                    note="printed waveform resolution, not solver accuracy",
                ),
            )
        return ParameterProvenance(records=records, status="declared")

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _parameters(self) -> SyncBuckNetlistParameters:
        return SyncBuckNetlistParameters(
            input_voltage_v=self.input_voltage_v,
            inductance_h=self.inductance_h,
            capacitance_f=self.capacitance_f,
            load_resistance_ohm=self.load_resistance_ohm,
            switching_frequency_hz=self.switching_frequency_hz,
            dead_time_s=self.dead_time_s,
            switch_on_resistance_ohm=self.switch_on_resistance_ohm,
            switch_off_resistance_ohm=self.switch_off_resistance_ohm,
            diode_saturation_current_a=self.diode_saturation_current_a,
            diode_emission_coefficient=self.diode_emission_coefficient,
            initial_inductor_current_a=self.inductor_current_a,
            initial_output_voltage_v=self.output_voltage_v,
        )


def export_sync_buck_topology_descriptor(plant: Any) -> Any:
    """Export the synchronous-Buck display descriptor for one plant."""

    from ..topology import ngspice_sync_buck_topology_descriptor

    return ngspice_sync_buck_topology_descriptor(plant)


register_solve_path(
    "ngspice-sync-buck",
    (
        "backends/sync_buck.py",
        "backends/ngspice_plants.py",
        "backends/process.py",
        "backends/settings.py",
        "backends/wrdata.py",
    ),
)


__all__ = [
    "SYNC_NETLIST_TEMPLATE_VERSION",
    "SyncBuckNetlistParameters",
    "NativeSyncBuckNetlistBuilder",
    "NgspiceSyncBuckPlant",
    "classify_conduction_regime",
    "export_sync_buck_topology_descriptor",
    "render_sync_buck_netlist",
]

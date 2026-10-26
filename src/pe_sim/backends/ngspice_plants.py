"""Window-batched ngspice Buck plant adapter.

The public Plant contract advances time in small segments.  Re-running an
external simulator for every segment would be unusable, so this adapter solves
one zero-order-hold solve segment per ngspice invocation and serves the
interior segment endpoints by interpolation.  The Runner ends a solve segment
at the next change point of the external inputs, so the duty ratio is constant
for the whole segment and the carrier phase is continuous across segments.

This is a switching-level reference adapter, not a device, thermal, or product
model.  It must be reviewed and accepted on its own evidence boundary.
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

from ..contracts import BackendProcessError, PlantObservation
from ..model_claim import (
    ModelClaim,
    NumericalLimit,
    OperatingRange,
    ParameterProvenance,
    ParameterRecord,
)
from ..numerical_health import NumericalHealth
from ..safety import BoundedActionPolicy
from ..identity import implementation_digest, implementation_sources, review_status
from .netlist import (
    NETLIST_TEMPLATE_VERSION,
    BuckNetlistParameters,
    default_sample_step_s,
)
from .netlist_builder import NativeBuckNetlistBuilder, NetlistBuilder
from .process import NgspiceExecutableAdapter, NgspiceProcess, NgspiceRunResult
from .settings import NgspiceSolverSettings
from .wrdata import TraceTable


_VOUT_SIGNALS = ("v(vout)", "vout")
_IL_SIGNALS = ("i(l1)", "l1#branch", "il")
_VSW_SIGNALS = ("v(sw)", "sw")


def _positive(name: str, value: float) -> float:
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"{name} must be finite and positive")
    return number


def _non_negative(name: str, value: float) -> float:
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"{name} must be finite and non-negative")
    return number


def _finite(name: str, value: float) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite")
    return number


def _first_signal(table: TraceTable, candidates: tuple[str, ...], description: str) -> str:
    for name in candidates:
        if name in table.signals:
            return name
    raise BackendProcessError(
        f"ngspice output lacks the required {description} signal; available: {sorted(table.signals)}"
    )


@dataclass(frozen=True)
class _WindowCache:
    """One solved zero-order-hold solve segment expressed in absolute time."""

    start_s: float
    end_s: float
    duty: float
    input_voltage_v: float
    load_resistance_ohm: float
    table: TraceTable
    netlist_sha256: str
    output_sha256: str | None

    def matches(self, duty: float, input_voltage_v: float, load_resistance_ohm: float, tolerance: float = 1e-12) -> bool:
        return (
            abs(self.duty - duty) <= tolerance
            and abs(self.input_voltage_v - input_voltage_v) <= tolerance
            and abs(self.load_resistance_ohm - load_resistance_ohm) <= tolerance
        )

    def covers(self, time_s: float, tolerance: float = 1e-12) -> bool:
        return self.start_s - tolerance <= time_s <= self.end_s + tolerance

    def value_at(self, table_signal: str, absolute_time_s: float) -> float:
        return self.table.value_at(table_signal, absolute_time_s - self.start_s)


class NgspiceBuckPlant:
    """Switching-level asynchronous Buck converter solved by ngspice.

    The adapter declares bounded duty action, external input updates, and
    snapshot/restore.  Restore is state-equivalent, not bit-identical: ngspice
    is not asked to persist its internal solver state, so a resumed run must be
    compared with an explicit numerical tolerance rather than an exact hash.
    """

    topology = "buck"
    model_kind = "ngspice_switching_reference"
    model_level = "L2"
    # Declared so the kernel can recompute the reviewed solve-path identity
    # instead of trusting this adapter's own manifest_identity() record.
    backend_id = "ngspice-buck"

    def __init__(
        self,
        *,
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
        netlist_builder: NetlistBuilder | None = None,
    ) -> None:
        self.input_voltage_v = _positive("input_voltage_v", input_voltage_v)
        self.inductance_h = _positive("inductance_h", inductance_h)
        self.capacitance_f = _positive("capacitance_f", capacitance_f)
        self.load_resistance_ohm = _positive("load_resistance_ohm", load_resistance_ohm)
        self.switching_frequency_hz = _positive("switching_frequency_hz", switching_frequency_hz)
        self.control_period_s = _positive("control_period_s", control_period_s)
        # The carrier phase is a parameter of each solve segment, so the control
        # period no longer has to be an integer multiple of the switching
        # period.  The Runner splits a window at every external-input change
        # point and passes the segment start phase to the netlist builder.
        self.switch_on_resistance_ohm = _positive("switch_on_resistance_ohm", switch_on_resistance_ohm)
        self.switch_off_resistance_ohm = _positive("switch_off_resistance_ohm", switch_off_resistance_ohm)
        self.diode_saturation_current_a = _positive("diode_saturation_current_a", diode_saturation_current_a)
        self.diode_emission_coefficient = _positive("diode_emission_coefficient", diode_emission_coefficient)
        self.solver = solver or NgspiceSolverSettings()
        if sample_step_s is not None:
            _positive("sample_step_s", sample_step_s)
        self.sample_step_s = sample_step_s if sample_step_s is None else float(sample_step_s)
        self.time_s = 0.0
        self.inductor_current_a = 0.0
        self.output_voltage_v = 0.0
        self.switch_node_voltage_v = 0.0
        self.last_duty = 0.0
        self._cache: _WindowCache | None = None
        self._first_result: NgspiceRunResult | None = None
        self._last_result: NgspiceRunResult | None = None
        self._solver_invocations = 0
        self._last_failure_evidence: dict[str, Any] | None = None
        self._numerical_health: NumericalHealth | None = None
        command = ngspice_executable if ngspice_executable is not None else "ngspice"
        self._provenance_adapter = (
            None if solver_callable is not None else NgspiceExecutableAdapter(command, solver=self.solver)
        )
        self._process = (
            None
            if solver_callable is not None
            else NgspiceProcess(
                command,
                solver=self.solver,
                timeout_s=timeout_s,
                keep_artifacts=keep_artifacts,
                work_root=work_root,
            )
        )
        self._solver_callable = solver_callable
        self._netlist_builder: NetlistBuilder = netlist_builder or NativeBuckNetlistBuilder()
        self._provenance_cache: dict[str, Any] | None = None
        self._identity_cache: dict[str, Any] | None = None

    # ------------------------------------------------------------------
    # PlantAdapter lifecycle
    # ------------------------------------------------------------------
    def capabilities(self) -> set[str]:
        return {"continuous_time", "external_input", "snapshot"}

    def action_policy(self) -> BoundedActionPolicy:
        return BoundedActionPolicy(0.0, 1.0, name="duty")

    def reset(self, initial_state: Mapping[str, Any]) -> Mapping[str, Any]:
        state = dict(initial_state or {})
        self.time_s = 0.0
        self.inductor_current_a = _finite(
            "inductor_current_a",
            state.get("inductor_current_a", state.get("current_a", 0.0)),
        )
        self.output_voltage_v = _finite(
            "output_voltage_v",
            state.get("output_voltage_v", state.get("vout", 0.0)),
        )
        self.switch_node_voltage_v = 0.0
        self.last_duty = float(state.get("last_duty", 0.0))
        if not math.isfinite(self.last_duty) or not 0.0 <= self.last_duty <= 1.0:
            raise ValueError("last_duty must be finite and in [0, 1]")
        self._cache = None
        self._first_result = None
        self._last_result = None
        self._solver_invocations = 0
        self._last_failure_evidence = None
        self._numerical_health = None
        self._validate_state()
        return self._state_dict()

    def observe(self) -> PlantObservation:
        self._validate_state()
        truth: dict[str, float] = {
            "vout": self.output_voltage_v,
            "inductor_current": self.inductor_current_a,
            "duty": self.last_duty,
            "input_voltage": self.input_voltage_v,
            "load_resistance": self.load_resistance_ohm,
            "switch_node_voltage": self.switch_node_voltage_v,
        }
        return PlantObservation(
            time_s=self.time_s,
            measurement={
                "vout": self.output_voltage_v,
                "inductor_current": self.inductor_current_a,
            },
            truth=truth,
            age_steps=0,
            measurement_units={"vout": "V", "inductor_current": "A"},
        )

    def advance(
        self,
        action: float,
        duration: float,
        external_input: Mapping[str, Any] | None = None,
    ) -> PlantObservation:
        duty = float(action)
        if not math.isfinite(duty) or not 0.0 <= duty <= 1.0:
            raise ValueError("action duty must be finite and in [0, 1]")
        duration_value = float(duration)
        if not math.isfinite(duration_value) or duration_value <= 0:
            raise ValueError("duration must be finite and positive")
        self._apply_external_input(external_input)

        target_time = self.time_s + duration_value
        tolerance = 1e-12
        cache = self._cache
        needs_solve = (
            cache is None
            or not cache.matches(duty, self.input_voltage_v, self.load_resistance_ohm)
            or not cache.covers(target_time, tolerance)
        )
        if needs_solve:
            cache = self._solve_window(duty, duration_value)
            self._cache = cache

        vout = cache.value_at(_first_signal(cache.table, _VOUT_SIGNALS, "output voltage"), target_time)
        inductor_current = cache.value_at(_first_signal(cache.table, _IL_SIGNALS, "inductor current"), target_time)
        switch_signal = next((name for name in _VSW_SIGNALS if name in cache.table.signals), None)
        switch_node = cache.value_at(switch_signal, target_time) if switch_signal else 0.0

        self.time_s = target_time
        self.output_voltage_v = vout
        self.inductor_current_a = inductor_current
        self.switch_node_voltage_v = switch_node
        self.last_duty = duty
        self._validate_state()
        return self.observe()

    def snapshot(self) -> Mapping[str, Any]:
        self._validate_state()
        return {
            **self._state_dict(),
            "topology": self.topology,
            "model_kind": self.model_kind,
            "last_duty": self.last_duty,
            "input_voltage_v": self.input_voltage_v,
            "load_resistance_ohm": self.load_resistance_ohm,
            "netlist_template_version": NETLIST_TEMPLATE_VERSION,
            "netlist_builder": self._netlist_builder.name,
        }

    def restore(self, snapshot: Mapping[str, Any]) -> Mapping[str, Any]:
        if str(snapshot.get("topology", self.topology)) != self.topology:
            raise ValueError("snapshot topology does not match plant")
        if str(snapshot.get("model_kind", self.model_kind)) != self.model_kind:
            raise ValueError("snapshot model kind does not match plant")
        template_version = snapshot.get("netlist_template_version", NETLIST_TEMPLATE_VERSION)
        if str(template_version) != NETLIST_TEMPLATE_VERSION:
            raise ValueError("snapshot netlist template version does not match plant")
        builder_name = snapshot.get("netlist_builder", self._netlist_builder.name)
        if str(builder_name) != self._netlist_builder.name:
            raise ValueError("snapshot netlist builder does not match plant")
        self.time_s = _non_negative("time_s", snapshot["time_s"])
        self.inductor_current_a = _finite("inductor_current_a", snapshot["inductor_current_a"])
        self.output_voltage_v = _finite("output_voltage_v", snapshot["output_voltage_v"])
        self.last_duty = float(snapshot.get("last_duty", 0.0))
        if not math.isfinite(self.last_duty) or not 0.0 <= self.last_duty <= 1.0:
            raise ValueError("last_duty must be finite and in [0, 1]")
        self.input_voltage_v = _positive("input_voltage_v", snapshot.get("input_voltage_v", self.input_voltage_v))
        self.load_resistance_ohm = _positive("load_resistance_ohm", snapshot.get("load_resistance_ohm", self.load_resistance_ohm))
        self.switch_node_voltage_v = 0.0
        # ngspice internal solver state is not persisted; the next advance
        # re-solves from the restored inductor current and capacitor voltage.
        self._cache = None
        self._last_result = None
        self._validate_state()
        return self._state_dict()

    # ------------------------------------------------------------------
    # Reference helpers and evidence
    # ------------------------------------------------------------------
    def analytical_steady_state(self, duty: float) -> dict[str, float]:
        """Return the ideal CCM equilibrium for comparison, not a model claim."""

        duty_value = float(duty)
        if not math.isfinite(duty_value) or not 0.0 <= duty_value <= 1.0:
            raise ValueError("duty must be finite and in [0, 1]")
        voltage = duty_value * self.input_voltage_v
        return {"output_voltage_v": voltage, "inductor_current_a": voltage / self.load_resistance_ohm}

    def validity_envelope(self) -> ModelClaim:
        """Declare the reviewed switching-reference model limits.

        The netlist represents the switch and diode explicitly, so CCM/DCM is
        a reported regime rather than a validity requirement.  The carrier
        phase is a parameter of each solve segment, so the control period may
        be any positive duration, not an integer multiple of the switching
        period.
        """

        parameters = self._parameters()
        switching_period_s = 1.0 / self.switching_frequency_hz
        effective_step_s = (
            self.sample_step_s
            if self.sample_step_s is not None
            else default_sample_step_s(parameters, self.control_period_s)
        )
        samples_per_switching_period = switching_period_s / effective_step_s
        switching_periods_per_control_period = self.control_period_s * self.switching_frequency_hz
        pulse_edge_s = 1e-9
        return ModelClaim(
            claim_level="L2-switching-reference",
            topology=self.topology,
            model_form="switching_netlist",
            assumptions=(
                "ideal voltage-controlled switch with explicit on/off resistance",
                "Shockley diode with the declared saturation current and emission coefficient",
                "linear inductor and capacitor (no saturation, ESR, or ESL)",
                "no thermal state or temperature-dependent device parameters",
                "constant duty ratio within each solved segment; external inputs "
                "may change only at declared change points",
            ),
            operating_ranges=(
                OperatingRange(
                    "duty",
                    unit="1",
                    minimum=0.0,
                    maximum=1.0,
                    basis="template_domain",
                ),
                OperatingRange(
                    "input_voltage_v",
                    unit="V",
                    minimum=0.0,
                    minimum_exclusive=True,
                    basis="equation_domain",
                ),
                OperatingRange(
                    "load_resistance_ohm",
                    unit="ohm",
                    minimum=0.0,
                    minimum_exclusive=True,
                    basis="equation_domain",
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
                    "load.min_off_resistance_ratio",
                    1e3,
                    "1",
                    strength="advisory",
                    basis="structural",
                    note="load resistance should stay well below the switch off resistance",
                ),
                NumericalLimit(
                    "duty.min_edge_ratio",
                    pulse_edge_s,
                    "s",
                    strength="advisory",
                    basis="resolution",
                    note="PULSE rise/fall edge in the netlist template",
                ),
            ),
            limitations=(
                "ideal voltage-controlled switch (Ron/Roff) and Shockley diode with the declared emission coefficient",
                "no thermal model and no temperature coefficients",
                "no ESR/ESL, magnetic saturation, or device self-heating",
                "the carrier is reconstructed from the segment-start phase; a segment "
                "that starts inside an on-pulse applies the remaining on-time as a "
                "finite PWL tail",
                "restore is state-equivalent, not bit-identical",
                "waveform values between printed wrdata points are linearly interpolated",
            ),
            uncheckable_rules=(
                {
                    "rule_id": "envelope.numerical.max_step",
                    "reason": "solver_option_not_applied_by_netlist_template",
                },
                {
                    "rule_id": "envelope.thermal.state",
                    "reason": "no_thermal_state_modelled",
                },
                {
                    "rule_id": "envelope.restore.bit_identical",
                    "reason": "ngspice_solver_state_not_persisted",
                },
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
                "diode_saturation_current_a": self.diode_saturation_current_a,
                "diode_emission_coefficient": self.diode_emission_coefficient,
                "switching_period_s": switching_period_s,
                "switching_periods_per_control_period": switching_periods_per_control_period,
                "samples_per_switching_period": samples_per_switching_period,
                "pulse_edge_s": pulse_edge_s,
                "load_off_resistance_ratio": self.switch_off_resistance_ohm / self.load_resistance_ohm,
            },
        )

    def parameter_provenance(self) -> ParameterProvenance:
        """Declare the reference netlist parameters and their review state."""

        values = (
            ("input_voltage_v", self.input_voltage_v, "V"),
            ("inductance_h", self.inductance_h, "H"),
            ("capacitance_f", self.capacitance_f, "F"),
            ("load_resistance_ohm", self.load_resistance_ohm, "ohm"),
            ("switching_frequency_hz", self.switching_frequency_hz, "Hz"),
            ("control_period_s", self.control_period_s, "s"),
            ("switch_on_resistance_ohm", self.switch_on_resistance_ohm, "ohm"),
            ("switch_off_resistance_ohm", self.switch_off_resistance_ohm, "ohm"),
            ("diode_saturation_current_a", self.diode_saturation_current_a, "A"),
            ("diode_emission_coefficient", self.diode_emission_coefficient, "1"),
        )
        records = tuple(
            ParameterRecord(
                name=name,
                value=value,
                unit=unit,
                source="assumption",
                reference="ngspice_plants.py constructor/default",
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
                    reference="ngspice_plants.py constructor",
                    review_status="unreviewed",
                    note="printed waveform resolution, not solver accuracy",
                ),
            )
        return ParameterProvenance(records=records, status="declared")

    def backend_provenance(self) -> Mapping[str, Any]:
        if self._provenance_cache is None:
            if self._provenance_adapter is None:
                self._provenance_cache = {
                    "status": "not_declared",
                    "name": "ngspice",
                    "version": None,
                    "solver_settings": self.solver.to_dict(),
                    "limitations": [
                        "no ngspice executable was configured; backend identity was not assessed"
                    ],
                }
            else:
                self._provenance_cache = dict(self._provenance_adapter.backend_provenance())
        return dict(self._provenance_cache)

    def manifest_identity(self) -> dict[str, Any]:
        if self._identity_cache is None:
            module = sys.modules[type(self).__module__]
            netlist_module = sys.modules["pe_sim.backends.netlist"]
            solve_path_sources = implementation_sources("ngspice-buck")
            solve_path_sha256 = implementation_digest("ngspice-buck")
            implementation = {
                "module": type(self).__module__,
                "class": type(self).__qualname__,
                "source_sha256": hashlib.sha256(inspect.getsource(module).encode("utf-8")).hexdigest(),
                "netlist_source_sha256": hashlib.sha256(inspect.getsource(netlist_module).encode("utf-8")).hexdigest(),
                "solve_path_modules": solve_path_sources,
                "sha256": solve_path_sha256,
                "review": review_status("ngspice-buck", solve_path_sha256),
            }
            backend = self.backend_provenance()
            identity: dict[str, Any] = {
                "level": self.model_level,
                "kind": self.model_kind,
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
                    "diode_saturation_current_a": self.diode_saturation_current_a,
                    "diode_emission_coefficient": self.diode_emission_coefficient,
                },
                "netlist_template": NETLIST_TEMPLATE_VERSION,
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
            self._identity_cache = identity
        return dict(self._identity_cache)

    def backend_evidence(self) -> dict[str, Any]:
        """Return bounded execution evidence for the run Manifest.

        Only digests, counts, and a bounded failure-log excerpt are recorded;
        complete waveforms and temporary paths are deliberately excluded.
        """

        evidence: dict[str, Any] = {"solver_invocations": self._solver_invocations}
        if self._first_result is not None:
            evidence["first_window"] = self._window_evidence(self._first_result)
        if self._last_result is not None:
            evidence["last_window"] = self._window_evidence(self._last_result)
        if self._numerical_health is not None:
            evidence["numerical_health"] = self._numerical_health.to_dict()
        if self._last_failure_evidence:
            evidence["last_failure"] = dict(self._last_failure_evidence)
        return evidence

    @staticmethod
    def _window_evidence(result: NgspiceRunResult) -> dict[str, Any]:
        return {
            "netlist_sha256": result.netlist_sha256,
            "output_sha256": result.output_sha256,
            "log_sha256": result.log_sha256,
            "returncode": result.returncode,
            "duration_s": result.duration_s,
        }

    def preview_netlist(self, *, duty: float = 0.5, duration_s: float | None = None) -> str:
        """Render the reviewed netlist without executing a simulator.

        This is the human-verification path for the circuit definition.  It
        uses the same builder, solver settings, and initial state as a real
        window, but performs no process call and writes no artifact.
        """

        self._validate_state()
        horizon = self.control_period_s if duration_s is None else float(duration_s)
        if not math.isfinite(horizon) or horizon <= 0:
            raise ValueError("duration_s must be finite and positive")
        switching_period_s = 1.0 / self.switching_frequency_hz
        return self._netlist_builder.render(
            self._parameters(),
            duty=float(duty),
            duration_s=horizon,
            solver=self.solver,
            sample_step_s=self.sample_step_s,
            carrier_phase_s=self.time_s % switching_period_s,
        )

    def topology_descriptor(self):
        from ..topology import export_topology_descriptor

        return export_topology_descriptor(self)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _parameters(self) -> BuckNetlistParameters:
        return BuckNetlistParameters(
            input_voltage_v=self.input_voltage_v,
            inductance_h=self.inductance_h,
            capacitance_f=self.capacitance_f,
            load_resistance_ohm=self.load_resistance_ohm,
            switching_frequency_hz=self.switching_frequency_hz,
            switch_on_resistance_ohm=self.switch_on_resistance_ohm,
            switch_off_resistance_ohm=self.switch_off_resistance_ohm,
            diode_saturation_current_a=self.diode_saturation_current_a,
            diode_emission_coefficient=self.diode_emission_coefficient,
            initial_inductor_current_a=self.inductor_current_a,
            initial_output_voltage_v=self.output_voltage_v,
        )

    def _solve_window(self, duty: float, duration_s: float) -> _WindowCache:
        window_start = self.time_s
        # Solve to the end of the current control window, or to the requested
        # duration when it is longer (a direct API caller may ask for more than
        # one window).  The Runner never requests past the window end, so a
        # Runner-driven solve stops exactly at the window end.  A change point
        # inside the window forces a new solve because the inputs no longer
        # match; a stale tail beyond the change point is never read.
        period = self.control_period_s
        cycles = math.floor(window_start / period + 1e-9)
        window_end = (cycles + 1) * period
        if window_end - window_start <= 1e-12:
            window_end += period
        horizon = max(float(duration_s), window_end - window_start)
        switching_period_s = 1.0 / self.switching_frequency_hz
        carrier_phase_s = window_start % switching_period_s
        parameters = self._parameters()
        netlist = self._netlist_builder.render(
            parameters,
            duty=duty,
            duration_s=horizon,
            solver=self.solver,
            sample_step_s=self.sample_step_s,
            carrier_phase_s=carrier_phase_s,
        )
        result = self._run_netlist(netlist)
        table = result.table
        if table.end_time_s + 1e-12 < horizon:
            raise BackendProcessError(
                f"ngspice window ended at {table.end_time_s!r}s before the requested {horizon!r}s"
            )
        self._last_result = result
        return _WindowCache(
            start_s=window_start,
            end_s=window_start + horizon,
            duty=duty,
            input_voltage_v=self.input_voltage_v,
            load_resistance_ohm=self.load_resistance_ohm,
            table=table,
            netlist_sha256=result.netlist_sha256,
            output_sha256=result.output_sha256,
        )

    def _run_netlist(self, netlist: str) -> NgspiceRunResult:
        self._solver_invocations += 1
        try:
            if self._solver_callable is not None:
                result = self._solver_callable(netlist)
            elif self._process is None:
                raise BackendProcessError("no ngspice execution path is configured")
            else:
                result = self._process.run(netlist, label=f"t{self.time_s:.9g}")
        except Exception as exc:
            evidence = getattr(exc, "evidence", None)
            if isinstance(evidence, Mapping):
                self._last_failure_evidence = dict(evidence)
            raise
        health = getattr(result, "numerical_health", None)
        if isinstance(health, NumericalHealth):
            self._numerical_health = (
                health
                if self._numerical_health is None
                else self._numerical_health.merged(health)
            )
        if self._first_result is None:
            self._first_result = result
        return result

    def _apply_external_input(self, external_input: Mapping[str, Any] | None) -> None:
        values = dict(external_input or {})
        if "vin_v" in values or "input_voltage_v" in values:
            value = values["vin_v"] if "vin_v" in values else values["input_voltage_v"]
            self.input_voltage_v = _positive("input_voltage_v", value)
        if "load_ohm" in values or "load_resistance_ohm" in values:
            value = values["load_ohm"] if "load_ohm" in values else values["load_resistance_ohm"]
            self.load_resistance_ohm = _positive("load_resistance_ohm", value)

    def _state_dict(self) -> dict[str, float]:
        return {
            "time_s": self.time_s,
            "inductor_current_a": self.inductor_current_a,
            "output_voltage_v": self.output_voltage_v,
        }

    def _validate_state(self) -> None:
        for name in ("time_s", "inductor_current_a", "output_voltage_v"):
            value = float(getattr(self, name))
            if not math.isfinite(value):
                raise ValueError(f"{name} must remain finite")
        if self.time_s < 0:
            raise ValueError("time_s must remain non-negative")


__all__ = ["NgspiceBuckPlant"]

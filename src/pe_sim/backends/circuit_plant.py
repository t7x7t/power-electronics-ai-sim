"""Run a compiled circuit topology as a stepped Plant adapter.

``pe_sim.circuit_compile`` answers whether a declared topology can become a
netlist; this module executes that netlist.  It is deliberately a *stepped*
adapter: the kernel owns the clock, one control window is one constant-duty
solve segment, and the next segment resumes from the element state the previous
one ended at.

Three facts are worth being explicit about:

- **The descriptor is the model identity.**  A run records the descriptor
  fingerprint and the full descriptor document, not a Buck parameter record.
  Changing an input voltage or load resistance for one window is a parameter
  *override*; it does not silently rewrite the identity the caller cites.
- **Resume is state-equivalent, not bit-identical.**  Only the element state
  (inductor currents, capacitor voltages) is carried between windows.  ngspice
  solver internals are not persisted, so a resumed run must be compared with a
  numerical tolerance.
- **This adapter is not reviewed.**  It registers its solve path at runtime and
  reports ``not_pinned``.  Nothing produced through it can claim ``comparable``
  evidence, regardless of how plausible the waveform looks.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import inspect
import json
import math
from pathlib import Path
import sys
from typing import Any, Callable, Mapping

from ..circuit_compile import (
    CIRCUIT_NETLIST_TEMPLATE_VERSION,
    CompiledCircuit,
    compile_topology,
)
from ..circuit_devices import canonical_device_kind
from ..contracts import BackendProcessError, PlantObservation
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
from ..numerical_health import NumericalHealth
from ..safety import BoundedActionPolicy
from ..topology import TopologyDescriptor
from .process import NgspiceExecutableAdapter, NgspiceProcess, NgspiceRunResult
from .settings import NgspiceSolverSettings
from .wrdata import TraceTable


BACKEND_ID = "circuit-topology"
MODEL_KIND = "compiled_topology_reference"
MODEL_LEVEL = "L2"

#: Unit suffix to declared unit.  A parameter without a recognised suffix is
#: recorded as dimensionless rather than guessed.
_UNITS: tuple[tuple[str, str], ...] = (
    ("_frequency_hz", "Hz"),
    ("_time_s", "s"),
    ("_resistance_ohm", "ohm"),
    ("_voltage_v", "V"),
    ("_current_a", "A"),
    ("_inductance_h", "H"),
    ("_capacitance_f", "F"),
)


def _positive(name: str, value: Any) -> float:
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"{name} must be finite and positive")
    return number


def _finite(name: str, value: Any) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite")
    return number


def _unit_of(name: str) -> str | None:
    for suffix, unit in _UNITS:
        if name.endswith(suffix):
            return unit
    return None


@dataclass(frozen=True)
class _WindowCache:
    """One solved zero-order-hold segment expressed in absolute time."""

    start_s: float
    end_s: float
    duty: float
    input_voltage_v: float | None
    load_resistance_ohm: float
    table: TraceTable
    netlist_sha256: str
    output_sha256: str | None
    compiled_digest: str

    def matches(
        self,
        duty: float,
        input_voltage_v: float | None,
        load_resistance_ohm: float,
        tolerance: float = 1e-12,
    ) -> bool:
        if abs(self.duty - duty) > tolerance:
            return False
        if abs(self.load_resistance_ohm - load_resistance_ohm) > tolerance:
            return False
        if self.input_voltage_v is None or input_voltage_v is None:
            return self.input_voltage_v is input_voltage_v
        return abs(self.input_voltage_v - input_voltage_v) <= tolerance

    def covers(self, time_s: float, tolerance: float = 1e-12) -> bool:
        return self.start_s - tolerance <= time_s <= self.end_s + tolerance

    def value_at(self, signal: str, absolute_time_s: float) -> float:
        return self.table.value_at(signal, absolute_time_s - self.start_s)


class CircuitPlant:
    """A stepped Plant adapter over one declared circuit topology.

    The adapter requires exactly one ``resistor_load`` because the kernel's
    measurement contract names one output voltage.  It accepts at most one
    ``voltage_source`` as the externally updateable input; additional fixed
    sources are rendered but cannot be changed by an input schedule.
    """

    model_kind = MODEL_KIND
    model_level = MODEL_LEVEL
    backend_id = BACKEND_ID

    def __init__(
        self,
        descriptor: TopologyDescriptor,
        *,
        control_period_s: float | None = None,
        integrator: str | None = None,
        solver: NgspiceSolverSettings | None = None,
        ngspice_executable: str | Path | None = None,
        timeout_s: float = 30.0,
        sample_step_s: float | None = None,
        keep_artifacts: bool = False,
        work_root: str | Path | None = None,
        solver_callable: Callable[[str], NgspiceRunResult] | None = None,
    ) -> None:
        if not isinstance(descriptor, TopologyDescriptor):
            raise TypeError("descriptor must be a TopologyDescriptor")
        self.descriptor = descriptor
        self.topology = descriptor.topology_id
        base_solver = solver or NgspiceSolverSettings()
        if integrator is not None and integrator != base_solver.integration_method:
            base_solver = replace(base_solver, integration_method=integrator)
        self.solver = base_solver
        self.integrator = self.solver.integration_method
        if sample_step_s is not None:
            _positive("sample_step_s", sample_step_s)
        self.sample_step_s = None if sample_step_s is None else float(sample_step_s)

        components = list(descriptor.components)
        kinds = {
            component.component_id: canonical_device_kind(component.kind)
            for component in components
        }
        loads = [
            component
            for component in components
            if kinds[component.component_id] == "resistor_load"
        ]
        if len(loads) != 1:
            raise ValueError(
                "a circuit plant needs exactly one resistor_load to define vout; "
                f"the descriptor declares {len(loads)}"
            )
        self._load_id = loads[0].component_id
        self.load_resistance_ohm = _positive(
            "resistance_ohm", loads[0].parameters.get("resistance_ohm")
        )
        sources = [
            component
            for component in components
            if kinds[component.component_id] == "voltage_source"
        ]
        if len(sources) == 1:
            self._source_id: str | None = sources[0].component_id
            self.input_voltage_v: float | None = _positive(
                "voltage_v", sources[0].parameters.get("voltage_v")
            )
        elif not sources:
            self._source_id = None
            self.input_voltage_v = None
        else:
            # Additional fixed sources are legal circuits; they just cannot be
            # addressed by one external ``vin_v`` update.
            self._source_id = None
            self.input_voltage_v = None

        self._inductor_ids = tuple(
            component.component_id
            for component in components
            if kinds[component.component_id] == "inductor"
        )
        self._capacitor_ids = tuple(
            component.component_id
            for component in components
            if kinds[component.component_id] == "capacitor"
        )
        self._component_by_id = {
            component.component_id: component for component in components
        }
        self._initial_state: dict[str, float] = {}
        for component_id in (*self._inductor_ids, *self._capacitor_ids):
            component = self._component_by_id[component_id]
            kind = kinds[component_id]
            raw = component.parameters.get(
                "initial_current_a" if kind == "inductor" else "initial_voltage_v",
                0.0,
            )
            if raw is None:
                raw = 0.0
            self._initial_state[component_id] = _finite(
                f"initial state of {component_id}", raw
            )

        switching_period = self._switching_period(components, kinds)
        self._carrier_frequency_hz = 1.0 / switching_period
        if control_period_s is None:
            control_period_s = switching_period
        self.control_period_s = _positive("control_period_s", control_period_s)

        # A compile probe validates the descriptor and yields the element names
        # and the output net the adapter needs.  It starts no solver and writes
        # no file, so construction stays cheap.
        self._probe: CompiledCircuit = compile_topology(
            descriptor,
            duty=0.5,
            duration_s=self.control_period_s,
            sample_step_s=self.sample_step_s,
            integrator=self.integrator,
            options=self._compile_options(),
            num_digits=self.solver.num_digits,
            temperature_c=self.solver.temperature_c,
        )
        self.element_names = dict(self._probe.element_names)
        self.output_net = self._probe.output_net
        assert self.output_net is not None
        load_ports = {bound.port_id: bound.net_id for bound in loads[0].ports}
        self._output_positive_net = load_ports["positive"]
        self._output_negative_net = load_ports["negative"]
        assert self.output_net == self._output_positive_net
        self._switch_node_net: str | None = None
        for component in components:
            if kinds[component.component_id] != "voltage_controlled_switch":
                continue
            output_net = next(
                bound.net_id
                for bound in component.ports
                if bound.port_id == "output"
            )
            if output_net != self._probe.reference_net:
                self._switch_node_net = output_net
                break

        self.time_s = 0.0
        self.last_duty = 0.0
        self.output_voltage_v = 0.0
        self.inductor_current_a = 0.0
        self.switch_node_voltage_v = 0.0
        self._element_state: dict[str, float] = dict(self._initial_state)
        self._cache: _WindowCache | None = None
        self._last_compiled: CompiledCircuit | None = None
        self._first_result: NgspiceRunResult | None = None
        self._last_result: NgspiceRunResult | None = None
        self._solver_invocations = 0
        self._last_failure_evidence: dict[str, Any] | None = None
        self._numerical_health: NumericalHealth | None = None
        command = ngspice_executable if ngspice_executable is not None else "ngspice"
        self._provenance_adapter = (
            None
            if solver_callable is not None
            else NgspiceExecutableAdapter(command, solver=self.solver)
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
        self._provenance_cache: dict[str, Any] | None = None
        self._identity_cache: dict[str, Any] | None = None

    # ------------------------------------------------------------------
    # Construction helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _switching_period(
        components: list[Any], kinds: Mapping[str, str | None]
    ) -> float:
        frequencies = []
        for component in components:
            if kinds[component.component_id] != "pulse_source":
                continue
            raw = component.parameters.get("switching_frequency_hz")
            frequencies.append(_positive("switching_frequency_hz", raw))
        if not frequencies:
            raise ValueError(
                "a circuit plant needs a pulse_source carrier; compile_topology "
                "would refuse this descriptor"
            )
        first = frequencies[0]
        if any(abs(value - first) > 1e-12 * max(1.0, first) for value in frequencies):
            raise ValueError("carriers declare different switching frequencies")
        return 1.0 / first

    def _compile_options(self) -> dict[str, float]:
        """Return the numerical options the compiled netlist must carry.

        The adapter's ``solver_settings`` record and the rendered ``.options``
        line are the same declaration; a manifest that reported a tolerance the
        netlist did not contain would be provenance theatre.
        """

        return {
            "abstol": self.solver.abstol,
            "chgtol": self.solver.chgtol,
            "gmin": self.solver.gmin,
            "reltol": self.solver.reltol,
            "vntol": self.solver.vntol,
        }

    def _parameter_overrides(self) -> dict[str, dict[str, float]]:
        overrides: dict[str, dict[str, float]] = {}
        if self._source_id is not None and self.input_voltage_v is not None:
            overrides[self._source_id] = {"voltage_v": self.input_voltage_v}
        overrides[self._load_id] = {"resistance_ohm": self.load_resistance_ohm}
        return overrides

    # ------------------------------------------------------------------
    # PlantAdapter lifecycle
    # ------------------------------------------------------------------
    def capabilities(self) -> set[str]:
        return {"continuous_time", "external_input", "snapshot"}

    def action_policy(self) -> BoundedActionPolicy:
        return BoundedActionPolicy(0.0, 1.0, name="duty")

    def reset(self, initial_state: Mapping[str, Any]) -> Mapping[str, Any]:
        state = dict(initial_state or {})
        applied: dict[str, float] = {}
        element_state = dict(self._initial_state)

        if "inductor_current_a" in state or "current_a" in state:
            key = "inductor_current_a" if "inductor_current_a" in state else "current_a"
            if not self._inductor_ids:
                raise ValueError("the circuit declares no inductor to accept a current")
            value = _finite(key, state[key])
            element_state[self._inductor_ids[0]] = value
            applied[key] = value
        if "output_voltage_v" in state or "vout" in state:
            key = "output_voltage_v" if "output_voltage_v" in state else "vout"
            if not self._capacitor_ids:
                raise ValueError("the circuit declares no capacitor to accept a voltage")
            value = _finite(key, state[key])
            element_state[self._capacitor_ids[0]] = value
            applied[key] = value
        for key, value in sorted(state.items()):
            if key in applied or key in {"inductor_current_a", "current_a", "output_voltage_v", "vout"}:
                continue
            if key not in element_state:
                raise ValueError(f"unsupported initial_state key: {key!r}")
            number = _finite(key, value)
            element_state[key] = number
            applied[key] = number

        self.time_s = 0.0
        self.last_duty = 0.0
        self.output_voltage_v = 0.0
        if self._capacitor_ids:
            capacitor = self._component_by_id[self._capacitor_ids[0]]
            ports = {bound.port_id: bound.net_id for bound in capacitor.ports}
            if (ports["positive"], ports["negative"]) == (
                self._output_positive_net,
                self._output_negative_net,
            ):
                self.output_voltage_v = element_state[capacitor.component_id]
        self.inductor_current_a = element_state.get(self._inductor_ids[0], 0.0) if self._inductor_ids else 0.0
        self.switch_node_voltage_v = 0.0
        self._element_state = element_state
        self._cache = None
        self._last_compiled = None
        self._first_result = None
        self._last_result = None
        self._solver_invocations = 0
        self._last_failure_evidence = None
        self._numerical_health = None
        self._validate_state()
        return applied

    def observe(self) -> PlantObservation:
        self._validate_state()
        truth: dict[str, float] = {
            "vout": self.output_voltage_v,
            "inductor_current": self.inductor_current_a,
            "duty": self.last_duty,
            "switch_node_voltage": self.switch_node_voltage_v,
        }
        if self.input_voltage_v is not None:
            truth["input_voltage"] = self.input_voltage_v
        truth["load_resistance"] = self.load_resistance_ohm
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

        window_start = self.time_s
        target_time = window_start + duration_value
        cycles = math.floor(window_start / self.control_period_s + 1e-9)
        window_end = (cycles + 1) * self.control_period_s
        if window_end - window_start <= 1e-12:
            window_end += self.control_period_s
        horizon = max(duration_value, window_end - window_start)

        tolerance = 1e-12
        cache = self._cache
        needs_solve = (
            cache is None
            or not cache.matches(
                duty, self.input_voltage_v, self.load_resistance_ohm
            )
            or not cache.covers(target_time, tolerance)
        )
        if needs_solve:
            cache = self._solve_window(duty, horizon)
            self._cache = cache

        local_time = target_time - cache.start_s
        vout = self._net_voltage(
            cache.table, self._output_positive_net, local_time
        ) - self._net_voltage(cache.table, self._output_negative_net, local_time)
        if self._inductor_ids:
            inductor_current = cache.table.value_at(
                f"i({self.element_names[self._inductor_ids[0]]})", local_time
            )
        else:
            inductor_current = 0.0
        if self._switch_node_net is not None:
            switch_node = cache.table.value_at(
                f"v({self._switch_node_net})", local_time
            )
        else:
            switch_node = 0.0

        self.time_s = target_time
        self.last_duty = duty
        self.output_voltage_v = vout
        self.inductor_current_a = inductor_current
        self.switch_node_voltage_v = switch_node
        self._element_state = self._element_state_at(cache.table, local_time)
        self._validate_state()
        return self.observe()

    def snapshot(self) -> Mapping[str, Any]:
        self._validate_state()
        return {
            "time_s": self.time_s,
            "element_state": dict(self._element_state),
            "last_duty": self.last_duty,
            "input_voltage_v": self.input_voltage_v,
            "load_resistance_ohm": self.load_resistance_ohm,
            "topology_id": self.topology,
            "descriptor_fingerprint": self.descriptor.fingerprint,
            "netlist_template_version": CIRCUIT_NETLIST_TEMPLATE_VERSION,
        }

    def restore(self, snapshot: Mapping[str, Any]) -> Mapping[str, Any]:
        if str(snapshot.get("topology_id", self.topology)) != self.topology:
            raise ValueError("snapshot topology does not match plant")
        fingerprint = snapshot.get("descriptor_fingerprint")
        if fingerprint is not None and str(fingerprint) != self.descriptor.fingerprint:
            raise ValueError("snapshot descriptor fingerprint does not match plant")
        template = snapshot.get("netlist_template_version", CIRCUIT_NETLIST_TEMPLATE_VERSION)
        if str(template) != CIRCUIT_NETLIST_TEMPLATE_VERSION:
            raise ValueError("snapshot netlist template version does not match plant")
        raw_state = snapshot.get("element_state")
        if not isinstance(raw_state, Mapping):
            raise ValueError("snapshot element_state must be a mapping")
        unknown = sorted(set(raw_state) - set(self._initial_state))
        if unknown:
            raise ValueError(
                f"snapshot names elements this circuit does not declare: {unknown}"
            )
        element_state = dict(self._initial_state)
        for component_id, value in sorted(raw_state.items()):
            element_state[str(component_id)] = _finite(
                f"snapshot state of {component_id}", value
            )
        time_s = _finite("time_s", snapshot["time_s"])
        if time_s < 0.0:
            raise ValueError("snapshot time_s must be non-negative")
        self.time_s = time_s
        self.last_duty = float(snapshot.get("last_duty", 0.0))
        if not math.isfinite(self.last_duty) or not 0.0 <= self.last_duty <= 1.0:
            raise ValueError("last_duty must be finite and in [0, 1]")
        if snapshot.get("input_voltage_v") is not None:
            self.input_voltage_v = _positive(
                "input_voltage_v", snapshot["input_voltage_v"]
            )
        self.load_resistance_ohm = _positive(
            "load_resistance_ohm",
            snapshot.get("load_resistance_ohm", self.load_resistance_ohm),
        )
        self._element_state = element_state
        self._cache = None
        self._last_result = None
        self._validate_state()
        return self.snapshot()

    # ------------------------------------------------------------------
    # Inspection helpers
    # ------------------------------------------------------------------
    def preview_netlist(
        self, *, duty: float = 0.5, duration_s: float | None = None
    ) -> str:
        horizon = self.control_period_s if duration_s is None else float(duration_s)
        if not math.isfinite(horizon) or horizon <= 0:
            raise ValueError("duration_s must be finite and positive")
        switching_period = 1.0 / self._switching_frequency_hz()
        return compile_topology(
            self.descriptor,
            duty=duty,
            duration_s=horizon,
            sample_step_s=self.sample_step_s,
            integrator=self.integrator,
            carrier_phase_s=(self.time_s % switching_period),
            initial_conditions=self._element_state,
            parameter_overrides=self._parameter_overrides(),
            options=self._compile_options(),
            num_digits=self.solver.num_digits,
            temperature_c=self.solver.temperature_c,
        ).netlist

    def last_window_table(self) -> TraceTable | None:
        """Return the most recently solved window table, for inspection only."""
        return None if self._cache is None else self._cache.table

    def last_compiled(self) -> CompiledCircuit | None:
        """Return the most recently rendered netlist record, for inspection only."""
        return self._last_compiled

    def _switching_frequency_hz(self) -> float:
        return self._carrier_frequency_hz

    # ------------------------------------------------------------------
    # Reference helpers and evidence
    # ------------------------------------------------------------------
    def validity_envelope(self) -> ModelClaim:
        switching_frequency = self._switching_frequency_hz()
        effective_step_s = (
            self.sample_step_s
            if self.sample_step_s is not None
            else self.control_period_s / 200.0
        )
        samples_per_switching_period = (1.0 / switching_frequency) / effective_step_s
        return ModelClaim(
            claim_level="L2-compiled-topology",
            topology=self.topology,
            model_form="compiled_netlist",
            assumptions=(
                "the simulated circuit is the descriptor's device list rendered by "
                f"{CIRCUIT_NETLIST_TEMPLATE_VERSION}",
                "ideal sources, switches, and Shockley diodes with the declared parameters",
                "linear inductors and capacitors; no saturation, ESR, or ESL",
                "no thermal state or temperature-dependent device parameters",
                "constant duty ratio within each solved segment; external inputs "
                "may change only at declared change points",
            ),
            operating_ranges=(
                OperatingRange("duty", unit="1", minimum=0.0, maximum=1.0, basis="template_domain"),
                OperatingRange("control_period_s", unit="s", minimum=0.0, minimum_exclusive=True, basis="equation_domain"),
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
            ),
            limitations=(
                "no review record exists for a caller-authored topology; results are "
                "capped at limited evidence",
                "the descriptor declares connectivity, not electrical correctness",
                "no thermal model, ESR/ESL, magnetic saturation, or self-heating",
                "restore is state-equivalent, not bit-identical",
                "waveform values between printed wrdata points are linearly interpolated",
            ),
            uncheckable_rules=(
                {
                    "rule_id": "envelope.implementation.reviewed",
                    "reason": "no_review_record_for_compiled_topology",
                },
                {
                    "rule_id": "envelope.thermal.state",
                    "reason": "no_thermal_state_modelled",
                },
                {
                    "rule_id": "envelope.restore.bit_identical",
                    "reason": "ngspice_solver_state_not_persisted",
                },
                {
                    "rule_id": "envelope.descriptor.electrical_correctness",
                    "reason": "descriptor_declares_connectivity_only",
                },
            ),
            actuator={"name": "duty", "unit": "normalized"},
            static_parameters={
                "descriptor_fingerprint": self.descriptor.fingerprint,
                "netlist_template": CIRCUIT_NETLIST_TEMPLATE_VERSION,
                "control_period_s": self.control_period_s,
                "switching_frequency_hz": switching_frequency,
                "samples_per_switching_period": samples_per_switching_period,
                "output_net": self.output_net,
                "input_voltage_v": self.input_voltage_v,
                "load_resistance_ohm": self.load_resistance_ohm,
            },
        )

    def parameter_provenance(self) -> ParameterProvenance:
        records: list[ParameterRecord] = []
        for component in self.descriptor.components:
            for name, value in component.parameters.items():
                try:
                    number = float(value)
                except (TypeError, ValueError):
                    continue
                if not math.isfinite(number):
                    continue
                records.append(
                    ParameterRecord(
                        name=f"{component.component_id}.{name}",
                        value=number,
                        unit=_unit_of(str(name)),
                        source="assumption",
                        reference=f"descriptor component {component.component_id}",
                        review_status="unreviewed",
                        note="caller-authored descriptor value; not a device or calibrated parameter",
                    )
                )
        return ParameterProvenance(records=tuple(records), status="declared")

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
                self._provenance_cache = dict(
                    self._provenance_adapter.backend_provenance()
                )
        return dict(self._provenance_cache)

    def manifest_identity(self) -> dict[str, Any]:
        if self._identity_cache is None:
            module = sys.modules[type(self).__module__]
            solve_path_sources = implementation_sources(BACKEND_ID)
            solve_path_sha256 = implementation_digest(BACKEND_ID)
            implementation = {
                "module": type(self).__module__,
                "class": type(self).__qualname__,
                "source_sha256": hashlib.sha256(
                    inspect.getsource(module).encode("utf-8")
                ).hexdigest(),
                "solve_path_modules": solve_path_sources,
                "sha256": solve_path_sha256,
                "review": review_status(BACKEND_ID, solve_path_sha256),
            }
            backend = self.backend_provenance()
            identity: dict[str, Any] = {
                "level": self.model_level,
                "kind": self.model_kind,
                "topology": self.topology,
                "parameters": {
                    "descriptor_fingerprint": self.descriptor.fingerprint,
                    "control_period_s": self.control_period_s,
                    "integrator": self.integrator,
                    "netlist_template": CIRCUIT_NETLIST_TEMPLATE_VERSION,
                    "input_voltage_v": self.input_voltage_v,
                    "load_resistance_ohm": self.load_resistance_ohm,
                },
                "descriptor": self.descriptor.to_dict(),
                "netlist_template": CIRCUIT_NETLIST_TEMPLATE_VERSION,
                "netlist_builder": {
                    "name": "circuit-compiler",
                    "template": CIRCUIT_NETLIST_TEMPLATE_VERSION,
                    "execution": "external-ngspice-process",
                },
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
                json.dumps(
                    identity,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=True,
                ).encode("utf-8")
            ).hexdigest()
            self._identity_cache = identity
        return dict(self._identity_cache)

    def backend_evidence(self) -> dict[str, Any]:
        evidence: dict[str, Any] = {"solver_invocations": self._solver_invocations}
        if self._first_result is not None:
            evidence["first_window"] = self._window_evidence(self._first_result)
        if self._last_result is not None:
            evidence["last_window"] = self._window_evidence(self._last_result)
        if self._numerical_health is not None:
            evidence["numerical_health"] = self._numerical_health.to_dict()
        if self._last_failure_evidence:
            evidence["last_failure"] = dict(self._last_failure_evidence)
        if self._last_compiled is not None:
            evidence["compiled"] = {
                "digest": self._last_compiled.digest,
                "template": self._last_compiled.template,
                "control_mode": self._last_compiled.control_mode,
                "carrier_phase_s": self._last_compiled.carrier_phase_s,
                "carrier_tails_s": dict(self._last_compiled.carrier_tails_s),
            }
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

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _solve_window(self, duty: float, horizon: float) -> _WindowCache:
        window_start = self.time_s
        switching_period = 1.0 / self._switching_frequency_hz()
        compiled = compile_topology(
            self.descriptor,
            duty=duty,
            duration_s=horizon,
            sample_step_s=self.sample_step_s,
            integrator=self.integrator,
            carrier_phase_s=(window_start % switching_period),
            initial_conditions=self._element_state,
            parameter_overrides=self._parameter_overrides(),
            options=self._compile_options(),
            num_digits=self.solver.num_digits,
            temperature_c=self.solver.temperature_c,
        )
        result = self._run_netlist(compiled.netlist, label=f"t{window_start:.9g}")
        if result.table.end_time_s + 1e-12 < horizon:
            raise BackendProcessError(
                f"ngspice window ended at {result.table.end_time_s!r}s before the "
                f"requested {horizon!r}s"
            )
        self._last_compiled = compiled
        self._last_result = result
        return _WindowCache(
            start_s=window_start,
            end_s=window_start + horizon,
            duty=duty,
            input_voltage_v=self.input_voltage_v,
            load_resistance_ohm=self.load_resistance_ohm,
            table=result.table,
            netlist_sha256=result.netlist_sha256,
            output_sha256=result.output_sha256,
            compiled_digest=compiled.digest,
        )

    def _run_netlist(self, netlist: str, *, label: str) -> NgspiceRunResult:
        self._solver_invocations += 1
        try:
            if self._solver_callable is not None:
                result = self._solver_callable(netlist)
            elif self._process is None:
                raise BackendProcessError("no ngspice execution path is configured")
            else:
                result = self._process.run(netlist, label=label)
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
            if self._source_id is None:
                raise ValueError(
                    "the circuit does not declare exactly one voltage_source, so an "
                    "external vin_v update is ambiguous"
                )
            value = values["vin_v"] if "vin_v" in values else values["input_voltage_v"]
            self.input_voltage_v = _positive("input_voltage_v", value)
        if "load_ohm" in values or "load_resistance_ohm" in values:
            value = (
                values["load_ohm"]
                if "load_ohm" in values
                else values["load_resistance_ohm"]
            )
            self.load_resistance_ohm = _positive("load_resistance_ohm", value)

    def _net_voltage(self, table: TraceTable, net_id: str, local_time_s: float) -> float:
        if net_id == self._probe.reference_net:
            return 0.0
        return table.value_at(f"v({net_id})", local_time_s)

    def _element_state_at(
        self, table: TraceTable, local_time_s: float
    ) -> dict[str, float]:
        state: dict[str, float] = {}
        for component_id in self._inductor_ids:
            state[component_id] = table.value_at(
                f"i({self.element_names[component_id]})", local_time_s
            )
        for component_id in self._capacitor_ids:
            component = self._component_by_id[component_id]
            ports = {bound.port_id: bound.net_id for bound in component.ports}
            state[component_id] = self._net_voltage(
                table, ports["positive"], local_time_s
            ) - self._net_voltage(table, ports["negative"], local_time_s)
        return state

    def _validate_state(self) -> None:
        for name in ("time_s", "output_voltage_v", "inductor_current_a"):
            value = float(getattr(self, name))
            if not math.isfinite(value):
                raise ValueError(f"{name} must remain finite")
        if self.time_s < 0:
            raise ValueError("time_s must remain non-negative")
        for component_id, value in self._element_state.items():
            if not math.isfinite(float(value)):
                raise ValueError(f"state of {component_id} must remain finite")


register_solve_path(
    BACKEND_ID,
    (
        "backends/circuit_plant.py",
        "backends/process.py",
        "backends/settings.py",
        "backends/wrdata.py",
        "circuit_compile.py",
        "circuit_devices.py",
        "topology.py",
    ),
)


__all__ = ["BACKEND_ID", "CircuitPlant", "MODEL_KIND", "MODEL_LEVEL"]

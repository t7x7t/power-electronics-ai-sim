"""Machine-readable declaration of the parameters each built-in plant accepts.

An agent has to know what it may put in ``plant_config`` *before* it writes an
experiment document.  Deriving that answer from constructor signatures would
require importing every adapter, which pulls in solvers and third-party
packages; duplicating it in prose would drift from the factories.  This module
is the declared single source: it is stdlib-only, so a client can query the
surface without loading the simulation kernel.

The table is a declaration, not a validator.  It records the same defaults the
composition root (``pe_sim.builtin_plants``) applies, and
``tests/test_plant_parameters.py`` compares the two, so a factory change cannot
silently leave the agent-facing declaration behind.

A wrong declaration is worse than no declaration: an agent told that ``vin``
is accepted writes a document that fails only after a run directory and a
backend process already exist.  Every entry therefore states the type, unit,
bound, and default the factory actually enforces, and a spec that contradicts
itself (``minimum > maximum``, an integer that claims to be nullable, a numeric
parameter with no stated dimension) is refused at import time instead of being
exported as plausible-looking JSON Schema.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping


#: Schema tag for ``describe_plant``; a client branches on this tag rather than
#: on the module layout.
PLANT_PARAMETERS_SCHEMA = "pe-sim.plant-parameters.v1"

#: The only value shapes a ``plant_config`` field may declare.
_PARAMETER_KINDS = frozenset({"number", "integer", "boolean"})


@dataclass(frozen=True)
class ParameterSpec:
    """One declared ``plant_config`` field.

    ``unit=None`` is reserved for dimensionless quantities, and the
    ``description`` must then state the dimension explicitly: a reader has to
    be able to tell volts from a duty cycle without inferring it from the name.
    """

    name: str
    kind: str
    unit: str | None
    minimum: float | None = None
    exclusive_minimum: bool = False
    maximum: float | None = None
    default: Any = None
    nullable: bool = False
    description: str = ""

    def __post_init__(self) -> None:
        if self.kind not in _PARAMETER_KINDS:
            raise ValueError(
                f"parameter {self.name!r} declares kind {self.kind!r}; "
                f"expected one of {sorted(_PARAMETER_KINDS)}"
            )
        if (
            self.kind in {"number", "integer"}
            and self.unit is None
            and not self.description.strip()
        ):
            raise ValueError(
                f"parameter {self.name!r} is numeric and declares no unit, so its "
                "description must state the dimension (for example 'dimensionless')"
            )
        if self.kind == "integer" and self.nullable:
            raise ValueError(
                f"parameter {self.name!r} cannot be both integer and nullable; "
                "a null is not an integer"
            )
        if self.exclusive_minimum and self.minimum is None:
            raise ValueError(
                f"parameter {self.name!r} declares exclusive_minimum without a minimum; "
                "JSON Schema would then carry a null bound"
            )
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise ValueError(
                f"parameter {self.name!r} declares minimum {self.minimum!r} "
                f"above maximum {self.maximum!r}"
            )

    def as_dict(self) -> dict[str, Any]:
        """Return the JSON-safe declaration with a stable field order."""

        return {
            "name": self.name,
            "kind": self.kind,
            "unit": self.unit,
            "minimum": self.minimum,
            "exclusive_minimum": self.exclusive_minimum,
            "maximum": self.maximum,
            "default": self.default,
            "nullable": self.nullable,
            "description": self.description,
        }


def _positive(
    name: str,
    unit: str,
    default: Any,
    description: str,
    *,
    nullable: bool = False,
) -> ParameterSpec:
    """Declare a number the factory validates with its ``_positive`` helper."""

    return ParameterSpec(
        name=name,
        kind="number",
        unit=unit,
        minimum=0.0,
        exclusive_minimum=True,
        default=default,
        nullable=nullable,
        description=description,
    )


def _input_voltage_v(default: float = 12.0) -> ParameterSpec:
    return _positive(
        "input_voltage_v", "V", default, "DC voltage applied to the converter input port."
    )


def _inductance_h(default: float = 100e-6) -> ParameterSpec:
    return _positive("inductance_h", "H", default, "Inductance of the output inductor.")


def _capacitance_f(default: float = 470e-6) -> ParameterSpec:
    return _positive("capacitance_f", "F", default, "Capacitance of the output capacitor.")


def _load_resistance_ohm(default: float) -> ParameterSpec:
    return _positive(
        "load_resistance_ohm", "ohm", default, "Resistive load across the output capacitor."
    )


def _switching_frequency_hz(default: float = 100e3) -> ParameterSpec:
    return _positive(
        "switching_frequency_hz", "Hz", default, "Switching frequency of the converter."
    )


def _integration_step_s(default: float) -> ParameterSpec:
    return _positive(
        "integration_step_s", "s", default, "Fixed integration step used by the model."
    )


def _control_period_s() -> ParameterSpec:
    return _positive(
        "control_period_s",
        "s",
        None,
        "Controller sampling period; null means the experiment timebase control period is used.",
        nullable=True,
    )


def _cadence_s() -> ParameterSpec:
    return _positive(
        "cadence_s",
        "s",
        None,
        "Trace cadence of the whole-run engine; null means the experiment control period is used.",
        nullable=True,
    )


def _default_vref_v() -> ParameterSpec:
    return ParameterSpec(
        name="default_vref_v",
        kind="number",
        unit="V",
        minimum=0.0,
        default=5.0,
        description="Reference voltage used until an input_schedule entry sets vref.",
    )


def _gain(name: str, default: float, description: str) -> ParameterSpec:
    return ParameterSpec(
        name=name,
        kind="number",
        unit=None,
        default=default,
        description=description,
    )


def _duty_limit(name: str, default: float, description: str) -> ParameterSpec:
    return ParameterSpec(
        name=name,
        kind="number",
        unit=None,
        minimum=0.0,
        maximum=1.0,
        default=default,
        description=description,
    )


def _anti_windup() -> ParameterSpec:
    return ParameterSpec(
        name="anti_windup",
        kind="boolean",
        unit=None,
        default=True,
        description="Clamp the controller integral while the duty command is saturated.",
    )


_FAKE_PARAMETERS = MappingProxyType(
    {
        spec.name: spec
        for spec in (
            _gain(
                "gain",
                1.0,
                "Dimensionless output gain applied to the requested action.",
            ),
            ParameterSpec(
                name="tau_s",
                kind="number",
                unit="s",
                default=0.01,
                description="First-order response time constant of the fixture plant.",
            ),
        )
    }
)

_REFERENCE_PARAMETERS = MappingProxyType(
    {
        spec.name: spec
        for spec in (
            _input_voltage_v(),
            _inductance_h(),
            _capacitance_f(),
            _load_resistance_ohm(10.0),
            _integration_step_s(1e-6),
        )
    }
)

_MULTIPHASE_BUCK_PARAMETERS = MappingProxyType(
    {
        **_REFERENCE_PARAMETERS,
        **{
            spec.name: spec
            for spec in (
                ParameterSpec(
                    name="phases",
                    kind="integer",
                    unit=None,
                    # ``multiphase_plants._phase_count`` rejects anything outside
                    # [2, 8]; declaring a wider range here would let an agent
                    # write a document that passes this check and fails only
                    # when the factory builds the plant.
                    minimum=2,
                    maximum=8,
                    default=4,
                    description="Number of interleaved phases (dimensionless count).",
                ),
                _switching_frequency_hz(),
            )
        },
    }
)

_NGSPICE_BUCK_PARAMETERS = MappingProxyType(
    {
        spec.name: spec
        for spec in (
            _input_voltage_v(),
            _inductance_h(),
            _capacitance_f(),
            _load_resistance_ohm(10.0),
            _switching_frequency_hz(),
            _control_period_s(),
            _positive(
                "switch_on_resistance_ohm",
                "ohm",
                1e-3,
                "On-state resistance of the high-side switch.",
            ),
            _positive(
                "switch_off_resistance_ohm",
                "ohm",
                1e9,
                "Off-state resistance of the high-side switch.",
            ),
            _positive(
                "diode_saturation_current_a",
                "A",
                1e-12,
                "Saturation current of the freewheeling diode in the Shockley model.",
            ),
            ParameterSpec(
                name="diode_emission_coefficient",
                kind="number",
                unit=None,
                minimum=0.0,
                exclusive_minimum=True,
                default=0.02,
                description="Dimensionless emission coefficient of the freewheeling diode.",
            ),
            _positive(
                "timeout_s", "s", 30.0, "Wall-clock limit for one solver process invocation."
            ),
            _positive(
                "sample_step_s",
                "s",
                None,
                "Fixed waveform sampling step; null keeps the solver's own output grid.",
                nullable=True,
            ),
            ParameterSpec(
                name="keep_artifacts",
                kind="boolean",
                unit=None,
                default=False,
                description="Keep the generated netlists and raw solver output for inspection.",
            ),
        )
    }
)

_NGSPICE_SYNC_BUCK_PARAMETERS = MappingProxyType(
    {
        **_NGSPICE_BUCK_PARAMETERS,
        "dead_time_s": _positive(
            "dead_time_s",
            "s",
            100e-9,
            "Dead time inserted between the high-side and low-side gate signals.",
        ),
    }
)

_WHOLE_RUN_BUCK_PARAMETERS = MappingProxyType(
    {
        spec.name: spec
        for spec in (
            _input_voltage_v(),
            _inductance_h(),
            _capacitance_f(),
            _load_resistance_ohm(1.0),
            _integration_step_s(1e-6),
            _cadence_s(),
            _default_vref_v(),
            _gain(
                "kp",
                0.1,
                "Dimensionless proportional gain of the engine-owned PI loop.",
            ),
            _gain(
                "ki",
                60.0,
                "Dimensionless integral gain of the engine-owned PI loop.",
            ),
            _duty_limit(
                "output_min",
                0.0,
                "Dimensionless lower duty-cycle bound of the engine-owned controller.",
            ),
            _duty_limit(
                "output_max",
                1.0,
                "Dimensionless upper duty-cycle bound of the engine-owned controller.",
            ),
            _anti_windup(),
        )
    }
)

_MODEL_CONTROL_BUCK_PARAMETERS = MappingProxyType(
    {
        spec.name: spec
        for spec in (
            _input_voltage_v(),
            _inductance_h(),
            _capacitance_f(),
            _load_resistance_ohm(5.0),
            _switching_frequency_hz(),
            _control_period_s(),
            _integration_step_s(0.5e-6),
            _gain(
                "kp",
                0.05,
                "Dimensionless proportional gain of the model-side PI loop.",
            ),
            _gain(
                "ki",
                20.0,
                "Dimensionless integral gain of the model-side PI loop.",
            ),
            _default_vref_v(),
            _duty_limit(
                "output_min",
                0.0,
                "Dimensionless lower duty-cycle bound of the model-side controller.",
            ),
            _duty_limit(
                "output_max",
                1.0,
                "Dimensionless upper duty-cycle bound of the model-side controller.",
            ),
            _anti_windup(),
            _cadence_s(),
        )
    }
)

_PEAK_CURRENT_BUCK_PARAMETERS = MappingProxyType(
    {
        spec.name: spec
        for spec in (
            _input_voltage_v(),
            _inductance_h(),
            _capacitance_f(),
            _load_resistance_ohm(5.0),
            _switching_frequency_hz(),
            _control_period_s(),
            _integration_step_s(0.5e-6),
            _gain(
                "kp",
                0.5,
                "Dimensionless proportional gain of the outer voltage loop.",
            ),
            _gain(
                "ki",
                200.0,
                "Dimensionless integral gain of the outer voltage loop.",
            ),
            _default_vref_v(),
            _duty_limit(
                "output_min",
                0.0,
                "Dimensionless lower duty-cycle bound of the peak-current controller.",
            ),
            _duty_limit(
                "output_max",
                1.0,
                "Dimensionless upper duty-cycle bound of the peak-current controller.",
            ),
            _anti_windup(),
            _positive(
                "current_reference_max_a",
                "A",
                5.0,
                "Upper current-reference limit produced by the outer voltage loop.",
            ),
            ParameterSpec(
                name="diode_emulation",
                kind="boolean",
                unit=None,
                default=False,
                description="Enable the model's diode-emulation branch at the low-side switch.",
            ),
        )
    }
)

#: The declared ``plant_config`` surface of every built-in plant.  Keys match the
#: ``*_DEFAULTS`` dictionaries in ``pe_sim.builtin_plants``; the fixture plants
#: declare ``_FAKE_CONFIG_KEYS`` instead of a defaults dictionary.
PLANT_PARAMETERS: Mapping[str, Mapping[str, ParameterSpec]] = MappingProxyType(
    {
        "fake": _FAKE_PARAMETERS,
        "fake-load": _FAKE_PARAMETERS,
        "buck": _REFERENCE_PARAMETERS,
        "boost": _REFERENCE_PARAMETERS,
        "multiphase-buck": _MULTIPHASE_BUCK_PARAMETERS,
        "ngspice-buck": _NGSPICE_BUCK_PARAMETERS,
        "ngspice-sync-buck": _NGSPICE_SYNC_BUCK_PARAMETERS,
        "whole-run-buck": _WHOLE_RUN_BUCK_PARAMETERS,
        "model-control-buck": _MODEL_CONTROL_BUCK_PARAMETERS,
        "peak-current-buck": _PEAK_CURRENT_BUCK_PARAMETERS,
        "whole-run-switching-buck": _MODEL_CONTROL_BUCK_PARAMETERS,
    }
)


#: The ``input_schedule`` keys each built-in plant accepts.  These are declared
#: here for the same reason as the parameter table: an agent that writes a
#: document from ``describe`` alone must not have to guess the schedule
#: vocabulary, and an undeclared key is rejected only after the document is
#: already written.  ``tests/test_plant_parameters.py`` pins both tables to the
#: registrations in ``pe_sim.builtin_plants``.
PLANT_SCHEDULE_KEYS: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "fake": frozenset({"time_s", "vref"}),
        "fake-load": frozenset({"time_s", "vref", "load"}),
        "buck": frozenset(
            {"time_s", "vref", "vin_v", "input_voltage_v", "load_ohm", "load_resistance_ohm"}
        ),
        "boost": frozenset(
            {"time_s", "vref", "vin_v", "input_voltage_v", "load_ohm", "load_resistance_ohm"}
        ),
        "multiphase-buck": frozenset(
            {"time_s", "vref", "vin_v", "input_voltage_v", "load_ohm", "load_resistance_ohm"}
        ),
        "ngspice-buck": frozenset(
            {"time_s", "vref", "vin_v", "input_voltage_v", "load_ohm", "load_resistance_ohm"}
        ),
        "ngspice-sync-buck": frozenset(
            {"time_s", "vref", "vin_v", "input_voltage_v", "load_ohm", "load_resistance_ohm"}
        ),
        "whole-run-buck": frozenset({"time_s", "vref"}),
        "model-control-buck": frozenset(
            {"time_s", "vref", "vin_v", "input_voltage_v", "load_ohm", "load_resistance_ohm"}
        ),
        "peak-current-buck": frozenset(
            {"time_s", "vref", "vin_v", "input_voltage_v", "load_ohm", "load_resistance_ohm"}
        ),
        "whole-run-switching-buck": frozenset(
            {"time_s", "vref", "vin_v", "input_voltage_v", "load_ohm", "load_resistance_ohm"}
        ),
    }
)


def _parameters_of(plant_id: str) -> Mapping[str, ParameterSpec]:
    """Return one plant's declaration, failing with the whole known list.

    Listing every known id is what lets an agent correct itself in one turn
    instead of guessing plant names.
    """

    try:
        return PLANT_PARAMETERS[plant_id]
    except KeyError:
        raise KeyError(
            f"unknown plant_id {plant_id!r}; known plant ids: {sorted(PLANT_PARAMETERS)}"
        ) from None


def _json_type(spec: ParameterSpec) -> str | list[str]:
    if spec.kind == "boolean":
        return "boolean"
    if spec.kind == "integer":
        return "integer"
    # ``null`` is part of the type list so a nullable field validates without a
    # contradictory "minimum" applied to null.
    return ["number", "null"] if spec.nullable else "number"


def parameter_schema(plant_id: str) -> dict[str, Any]:
    """Return the JSON Schema fragment for one plant's ``plant_config``.

    The object is closed (``additionalProperties: false``) because the
    factories reject unknown keys; leaving the schema open would let a document
    validate here and fail at build time.
    """

    parameters = _parameters_of(plant_id)
    properties: dict[str, Any] = {}
    for name in sorted(parameters):
        spec = parameters[name]
        entry: dict[str, Any] = {"type": _json_type(spec)}
        if spec.minimum is not None:
            if spec.exclusive_minimum:
                entry["exclusiveMinimum"] = spec.minimum
            else:
                entry["minimum"] = spec.minimum
        if spec.maximum is not None:
            entry["maximum"] = spec.maximum
        entry["default"] = spec.default
        entry["description"] = spec.description
        if spec.unit is not None:
            # ``x-unit`` is the documented extension: JSON Schema has no unit
            # vocabulary, and an agent must not have to infer volts from a name.
            entry["x-unit"] = spec.unit
        properties[name] = entry
    return {"type": "object", "additionalProperties": False, "properties": properties}


def describe_plant(plant_id: str) -> dict[str, Any]:
    """Return the agent-facing declaration of one plant, deterministically."""

    parameters = _parameters_of(plant_id)
    return {
        "schema": PLANT_PARAMETERS_SCHEMA,
        "plant_id": plant_id,
        "parameters": [parameters[name].as_dict() for name in sorted(parameters)],
        "parameter_schema": parameter_schema(plant_id),
        # Published with the parameters because a document needs both halves:
        # a correct ``plant_config`` next to a made-up schedule key still fails.
        "schedule_keys": sorted(PLANT_SCHEDULE_KEYS[plant_id]),
        "unknown_keys": "rejected",
    }


__all__ = [
    "PLANT_PARAMETERS",
    "PLANT_PARAMETERS_SCHEMA",
    "PLANT_SCHEDULE_KEYS",
    "ParameterSpec",
    "describe_plant",
    "parameter_schema",
]

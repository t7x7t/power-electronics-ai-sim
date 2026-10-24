"""Composition root for the plants that ship with ``pe_sim``.

This module is the only place that knows *which* plants the project ships and
*how* to build them.  The kernel reads the resulting registrations from
``pe_sim.plant_registry``; it never imports this module to decide what exists.

The factory signature is uniform so the kernel can call every plant the same
way::

    factory(config: Mapping[str, Any], context: PlantBuildContext) -> object

``config`` is the JSON-safe ``plant_config`` from the experiment document and
``context`` carries the execution facts a factory may need and the document
does not (the timebase, the resolved simulator executable, the netlist builder
choice, and whether a real executable is required).

The values here mirror ``pe_sim.cli`` exactly on purpose: this stage extracts
the knowledge into data, and a later change removes the duplicate.  Keeping
them in sync is checked by tests rather than by importing the CLI, so the CLI
stays free to change.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .backends import (
    BackendConfigError,
    BackendNotFoundError,
    NativeBuckNetlistBuilder,
    NgspiceBuckPlant,
    PyspiceBuckNetlistBuilder,
    PyspiceUnavailableError,
    resolve_ngspice_executable,
)
from .contracts import Timebase
from .plant_registry import PlantRegistration, register_plant, registered_plants
from .reference_plants import BuckPlant, BoostPlant
from .runtime import FakeLoadPlant, FakePlant


@dataclass(frozen=True)
class PlantBuildContext:
    """Execution facts a factory may need that are not in ``plant_config``."""

    timebase: Timebase | None = None
    ngspice_executable: str | None = None
    model_builder: str = "native"
    require_executable: bool = True


_BASE_SCHEDULE_KEYS = frozenset({"time_s", "vref"})
_EXTERNAL_INPUT_SCHEDULE_KEYS = frozenset(
    {"vin_v", "input_voltage_v", "load_ohm", "load_resistance_ohm"}
)

_FAKE_CONFIG_KEYS = frozenset({"gain", "tau_s"})

_REFERENCE_PLANT_DEFAULTS: dict[str, object] = {
    "input_voltage_v": 12.0,
    "inductance_h": 100e-6,
    "capacitance_f": 470e-6,
    "load_resistance_ohm": 10.0,
    "integration_step_s": 1e-6,
}
_REFERENCE_PLANT_CONFIG_KEYS = frozenset(_REFERENCE_PLANT_DEFAULTS)

_NGSPICE_BUCK_DEFAULTS: dict[str, object] = {
    "input_voltage_v": 12.0,
    "inductance_h": 100e-6,
    "capacitance_f": 470e-6,
    "load_resistance_ohm": 10.0,
    "switching_frequency_hz": 100e3,
    "control_period_s": None,
    "switch_on_resistance_ohm": 1e-3,
    "switch_off_resistance_ohm": 1e9,
    "diode_saturation_current_a": 1e-12,
    "diode_emission_coefficient": 0.02,
    "timeout_s": 30.0,
    "sample_step_s": None,
    "keep_artifacts": False,
}


def _reject_unknown_keys(plant_id: str, config: Mapping[str, Any], allowed: frozenset[str]) -> None:
    unknown = set(config) - set(allowed)
    if unknown:
        raise ValueError(f"unsupported {plant_id} plant_config fields: {sorted(unknown)}")


def _build_fake(config: Mapping[str, Any], context: PlantBuildContext) -> FakePlant:
    _reject_unknown_keys("fake", config, _FAKE_CONFIG_KEYS)
    return FakePlant(**dict(config))


def _build_fake_load(config: Mapping[str, Any], context: PlantBuildContext) -> FakeLoadPlant:
    _reject_unknown_keys("fake-load", config, _FAKE_CONFIG_KEYS)
    return FakeLoadPlant(**dict(config))


def _build_buck(config: Mapping[str, Any], context: PlantBuildContext) -> BuckPlant:
    _reject_unknown_keys("buck", config, _REFERENCE_PLANT_CONFIG_KEYS)
    values = dict(_REFERENCE_PLANT_DEFAULTS)
    values.update(config)
    return BuckPlant(**values)


def _build_boost(config: Mapping[str, Any], context: PlantBuildContext) -> BoostPlant:
    _reject_unknown_keys("boost", config, _REFERENCE_PLANT_CONFIG_KEYS)
    values = dict(_REFERENCE_PLANT_DEFAULTS)
    values.update(config)
    return BoostPlant(**values)


def _build_ngspice_buck(config: Mapping[str, Any], context: PlantBuildContext) -> NgspiceBuckPlant:
    """Build the switching-level Buck adapter with explicit execution evidence."""

    _reject_unknown_keys("ngspice-buck", config, frozenset(_NGSPICE_BUCK_DEFAULTS))
    values = dict(_NGSPICE_BUCK_DEFAULTS)
    values.update(config)
    control_period = values.pop("control_period_s")
    if control_period is None:
        if context.timebase is None:
            raise ValueError("ngspice-buck requires control_period_s in plant_config or timebase")
        control_period = context.timebase.control_period_s
    if context.require_executable:
        try:
            resolution = resolve_ngspice_executable(context.ngspice_executable)
        except (BackendNotFoundError, BackendConfigError) as exc:
            raise ValueError(str(exc)) from exc
        executable = resolution.executable
    else:
        # Preview and offline inspection do not execute the solver, so a
        # missing installation must not prevent reading the circuit template.
        executable = str(context.ngspice_executable or "ngspice")
    if context.model_builder == "pyspice":
        builder = PyspiceBuckNetlistBuilder()
        try:
            builder.check_available()
        except PyspiceUnavailableError as exc:
            raise ValueError(str(exc)) from exc
    else:
        builder = NativeBuckNetlistBuilder()
    return NgspiceBuckPlant(
        control_period_s=float(control_period),
        ngspice_executable=executable,
        netlist_builder=builder,
        **values,
    )


_BUILTIN_REGISTRATIONS: tuple[PlantRegistration, ...] = (
    PlantRegistration(
        plant_id="fake",
        backend_id="fake",
        factory=_build_fake,
        class_name="FakePlant",
        schedule_keys=_BASE_SCHEDULE_KEYS,
        config_keys=_FAKE_CONFIG_KEYS,
        trust_level="fixture",
        export_eligible=True,
    ),
    PlantRegistration(
        plant_id="fake-load",
        backend_id="fake-load",
        factory=_build_fake_load,
        class_name="FakeLoadPlant",
        schedule_keys=_BASE_SCHEDULE_KEYS | {"load"},
        config_keys=_FAKE_CONFIG_KEYS,
        trust_level="fixture",
        export_eligible=True,
    ),
    PlantRegistration(
        plant_id="buck",
        backend_id="buck",
        factory=_build_buck,
        class_name="BuckPlant",
        schedule_keys=_BASE_SCHEDULE_KEYS | _EXTERNAL_INPUT_SCHEDULE_KEYS,
        config_keys=_REFERENCE_PLANT_CONFIG_KEYS,
        trust_level="reference",
        export_eligible=True,
    ),
    PlantRegistration(
        plant_id="boost",
        backend_id="boost",
        factory=_build_boost,
        class_name="BoostPlant",
        schedule_keys=_BASE_SCHEDULE_KEYS | _EXTERNAL_INPUT_SCHEDULE_KEYS,
        config_keys=_REFERENCE_PLANT_CONFIG_KEYS,
        trust_level="reference",
        export_eligible=True,
    ),
    PlantRegistration(
        plant_id="ngspice-buck",
        backend_id="ngspice-buck",
        factory=_build_ngspice_buck,
        class_name="NgspiceBuckPlant",
        schedule_keys=_BASE_SCHEDULE_KEYS | _EXTERNAL_INPUT_SCHEDULE_KEYS,
        config_keys=None,
        trust_level="reference",
        export_eligible=True,
    ),
)


def builtin_registrations() -> tuple[PlantRegistration, ...]:
    """Return the project-owned registrations without touching the registry."""

    return _BUILTIN_REGISTRATIONS


def load_builtin_plants() -> None:
    """Register the shipped plants.  Calling it twice is a no-op.

    The whole set is checked before anything is registered, so a conflict
    cannot leave the registry holding half of the built-in plants.
    """

    existing = registered_plants()
    pending: list[PlantRegistration] = []
    for registration in _BUILTIN_REGISTRATIONS:
        current = existing.get(registration.plant_id)
        if current == registration:
            continue
        if current is not None:
            raise ValueError(
                f"plant_id {registration.plant_id!r} is already registered by backend "
                f"{current.backend_id!r}; unregister it before loading the built-in plants"
            )
        pending.append(registration)
    for registration in pending:
        register_plant(registration)


__all__ = [
    "PlantBuildContext",
    "builtin_registrations",
    "load_builtin_plants",
]

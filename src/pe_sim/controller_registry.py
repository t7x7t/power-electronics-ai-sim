"""Data-driven registry of controller factories.

The kernel does not know the names of the controllers it can execute.  What it
needs is the *shape* of a controller: an identifier, a callable that builds it,
the configuration keys it accepts, and the class name the factory must return.
Those facts are data, so adding a compensator is a registration instead of a
branch in the CLI.

The design mirrors :mod:`pe_sim.plant_registry`:

- the project-owned composition root (:mod:`pe_sim.builtin_controllers`)
  registers the shipped controllers explicitly;
- resolution validates the identifier and the declared configuration keys
  before a run directory exists;
- value validation stays in each factory, where the semantics live.

This module imports no adapter, no simulator, and no CLI: importing it cannot
start a run or pull in a heavy dependency.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Any, Callable, Mapping


# A controller id is used in run evidence and may become part of a file name,
# so it follows the same conservative grammar as ``plant_id``: letters, digits,
# '_', '.', '-'; no path separators and no embedded '..'.
_CONTROLLER_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")


@dataclass(frozen=True)
class ControllerBuildContext:
    """Execution facts a factory may need that are not in ``controller_config``."""

    control_period_s: float

    def __post_init__(self) -> None:
        try:
            value = float(self.control_period_s)
        except (TypeError, ValueError):
            raise ValueError(
                "control_period_s must be a finite positive number"
            ) from None
        if not math.isfinite(value) or value <= 0.0:
            raise ValueError("control_period_s must be finite and positive")
        object.__setattr__(self, "control_period_s", value)


@dataclass(frozen=True)
class ControllerRegistration:
    """One declarative description of a controller the kernel may build.

    ``config_keys`` is the complete set of keys the factory understands.  An
    unknown key fails at resolution, so a configuration copied from another
    controller cannot silently look effective.
    """

    controller_id: str
    factory: Callable[..., Any]
    config_keys: frozenset[str]
    class_name: str


_REGISTRATIONS: dict[str, ControllerRegistration] = {}


def _freeze_keys(value: Any) -> frozenset[str]:
    if isinstance(value, str):
        raise TypeError(
            "config_keys must be a collection of strings, not a single string"
        )
    try:
        items = frozenset(value)
    except TypeError:
        raise TypeError("config_keys must be a frozenset of strings") from None
    for item in items:
        if not isinstance(item, str) or not item:
            raise ValueError(
                f"config_keys must contain non-empty strings, got {item!r}"
            )
    return items


def _validated(registration: ControllerRegistration) -> ControllerRegistration:
    """Return a normalized copy, or raise without touching the registry."""

    if not isinstance(registration, ControllerRegistration):
        raise TypeError("registration must be a ControllerRegistration instance")
    controller_id = registration.controller_id
    if (
        not isinstance(controller_id, str)
        or _CONTROLLER_ID_PATTERN.fullmatch(controller_id) is None
        or ".." in controller_id
    ):
        raise ValueError(
            f"invalid controller_id {controller_id!r}: it must match "
            f"{_CONTROLLER_ID_PATTERN.pattern!r} (letters, digits, '_', '.', '-'; "
            "no path separators and no '..')"
        )
    if not callable(registration.factory):
        raise TypeError(f"controller_id {controller_id!r} factory must be callable")
    if not isinstance(registration.class_name, str) or not registration.class_name.strip():
        raise ValueError(
            f"controller_id {controller_id!r} must declare a non-empty class_name"
        )
    return replace(registration, config_keys=_freeze_keys(registration.config_keys))


def register_controller(
    registration: ControllerRegistration, *, replace: bool = False
) -> None:
    """Add one controller to the process-wide registry.

    A duplicate id is an error unless ``replace=True``, because a silent
    override would let one registration hide another's declared configuration
    keys.
    """

    validated = _validated(registration)
    existing = _REGISTRATIONS.get(validated.controller_id)
    if existing is not None and not replace:
        raise ValueError(
            f"controller_id {validated.controller_id!r} is already registered by "
            f"class {existing.class_name!r}; pass replace=True to override it"
        )
    _REGISTRATIONS[validated.controller_id] = validated


def resolve_controller(
    controller_id: str,
    config: Mapping[str, Any],
    context: ControllerBuildContext,
) -> Any:
    """Build the controller declared by ``controller_id``.

    Unknown ids and undeclared configuration keys fail closed before any run
    directory is created.  The factory owns value validation.
    """

    registration = _REGISTRATIONS.get(controller_id)
    if registration is None:
        raise ValueError(
            f"controller_id {controller_id!r} is not registered; "
            f"registered: {sorted(_REGISTRATIONS)}"
        )
    if not isinstance(config, Mapping):
        raise TypeError(
            f"controller_config for controller_id {controller_id!r} must be a mapping"
        )
    if not isinstance(context, ControllerBuildContext):
        raise TypeError("context must be a ControllerBuildContext")
    unknown = set(config) - registration.config_keys
    if unknown:
        raise ValueError(
            f"unsupported controller_config fields for controller_id "
            f"{controller_id!r}: {sorted(unknown)}; "
            f"allowed: {sorted(registration.config_keys)}"
        )
    controller = registration.factory(dict(config), context)
    actual = type(controller).__name__
    if actual != registration.class_name:
        raise ValueError(
            f"controller_id {controller_id!r} built {actual!r}, "
            f"expected {registration.class_name!r}"
        )
    return controller


def registered_controllers() -> Mapping[str, ControllerRegistration]:
    """Return a read-only snapshot of the registry."""

    return MappingProxyType(dict(_REGISTRATIONS))


def clear_controllers() -> None:
    """Reset the whole module state.  Used by tests only."""

    _REGISTRATIONS.clear()


__all__ = [
    "ControllerBuildContext",
    "ControllerRegistration",
    "clear_controllers",
    "register_controller",
    "registered_controllers",
    "resolve_controller",
]

"""Data-driven registry of Plant factories.

The kernel must not know the names of the engines and models it ships with.
What it actually needs is the *shape* of a plant: an identifier, the backend
that reviews it, a callable that builds it, the schedule keys it understands,
and the configuration keys it accepts.  Those facts are data, so adding a
plant becomes a registration instead of a kernel edit.

Two sources feed the registry:

- the project-owned composition root (``pe_sim.builtin_plants``) registers the
  shipped plants explicitly, and
- the ``pe_sim.plants`` entry-point group lets a third-party distribution ship
  its own plant without touching this repository.

Nothing here imports an adapter: importing this module cannot start a
simulator, read a configuration file, or pull in a heavy dependency.  Entry
points are only inspected when a caller explicitly asks for discovery or when
a lookup misses, so the built-in paths never import third-party code.

Trust is declared, never inferred.  The registry records ``trust_level`` and
``export_eligible`` as supplied by the registration; it never branches on the
value of a declaration, and it never upgrades a third-party plant to a
reviewed one.
"""

from __future__ import annotations

import importlib.metadata
import re
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Any, Callable, Mapping


# Declared provenance classes.  ``reference`` means the project reviewed the
# implementation; ``fixture`` means it exists for tests and examples;
# ``third-party`` is anything the project did not review.
TRUST_LEVELS: tuple[str, ...] = ("reference", "third-party", "fixture")

# Entry-point group a third-party distribution uses to ship a plant.  The
# group name is a contract with those distributions and is not a plant list.
ENTRY_POINT_GROUP = "pe_sim.plants"

# A plant id becomes a file-system-adjacent name in several places (run
# directories, export folders), so path separators, leading dots, and
# whitespace are rejected at registration time rather than at use time.
_PLANT_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")

_DEFAULT_SCHEDULE_KEYS = frozenset({"time_s", "vref"})


@dataclass(frozen=True)
class PlantRegistration:
    """One declarative description of a plant the kernel may build.

    ``config_keys`` of ``None`` means "the factory validates its own keys".
    ``schedule_keys`` defaults to the base schedule every plant understands.
    ``trust_level`` is a declaration for reporting; it is never used to decide
    how the kernel behaves.
    """

    plant_id: str
    backend_id: str
    factory: Callable[..., Any]
    class_name: str
    schedule_keys: frozenset[str] = _DEFAULT_SCHEDULE_KEYS
    config_keys: frozenset[str] | None = None
    trust_level: str = "third-party"
    export_eligible: bool = False


_REGISTRATIONS: dict[str, PlantRegistration] = {}

# Entry-point names are attempted at most once per process, so a repeated
# lookup cannot turn one broken plugin into a stream of duplicate errors.
_ATTEMPTED_ENTRY_POINTS: set[str] = set()
_DISCOVERY_ERRORS: list[str] = []
_DISCOVERY_ATTEMPTED = False


def _freeze_keys(name: str, value: Any) -> frozenset[str] | None:
    if value is None:
        return None
    if isinstance(value, str):
        raise TypeError(f"{name} must be a collection of strings, not a single string")
    try:
        items = frozenset(value)
    except TypeError:
        raise TypeError(f"{name} must be a frozenset of strings") from None
    for item in items:
        if not isinstance(item, str) or not item:
            raise ValueError(f"{name} must contain non-empty strings, got {item!r}")
    return items


def _validated(registration: PlantRegistration) -> PlantRegistration:
    """Return a normalized copy, or raise without touching the registry."""

    if not isinstance(registration, PlantRegistration):
        raise TypeError("registration must be a PlantRegistration instance")
    plant_id = registration.plant_id
    if not isinstance(plant_id, str) or _PLANT_ID_PATTERN.fullmatch(plant_id) is None:
        raise ValueError(
            f"invalid plant_id {plant_id!r}: it must match "
            f"{_PLANT_ID_PATTERN.pattern!r} (letters, digits, '_', '.', '-'; no path separators)"
        )
    if not isinstance(registration.backend_id, str) or not registration.backend_id.strip():
        raise ValueError(f"plant_id {plant_id!r} must declare a non-empty backend_id")
    if registration.trust_level not in TRUST_LEVELS:
        raise ValueError(
            f"plant_id {plant_id!r} declares trust_level {registration.trust_level!r}; "
            f"expected one of {list(TRUST_LEVELS)}"
        )
    if not callable(registration.factory):
        raise TypeError(f"plant_id {plant_id!r} factory must be callable")
    if not isinstance(registration.class_name, str) or not registration.class_name.strip():
        raise ValueError(f"plant_id {plant_id!r} must declare a non-empty class_name")
    if not isinstance(registration.export_eligible, bool):
        raise TypeError(f"plant_id {plant_id!r} export_eligible must be a bool")
    return replace(
        registration,
        schedule_keys=_freeze_keys("schedule_keys", registration.schedule_keys),
        config_keys=_freeze_keys("config_keys", registration.config_keys),
    )


def register_plant(registration: PlantRegistration, *, replace: bool = False) -> None:
    """Add one plant to the process-wide registry.

    A duplicate id is an error unless ``replace=True``, because a silent
    override would let one registration hide another's declared configuration
    keys.
    """

    validated = _validated(registration)
    existing = _REGISTRATIONS.get(validated.plant_id)
    if existing is not None and not replace:
        raise ValueError(
            f"plant_id {validated.plant_id!r} is already registered by backend "
            f"{existing.backend_id!r}; pass replace=True to override it"
        )
    _REGISTRATIONS[validated.plant_id] = validated


def get_plant(plant_id: str) -> PlantRegistration:
    """Return an already-registered plant; never triggers discovery."""

    registration = _REGISTRATIONS.get(plant_id)
    if registration is None:
        raise KeyError(
            f"plant_id {plant_id!r} is not registered; registered: {sorted(_REGISTRATIONS)}"
        )
    return registration


def plant_ids() -> tuple[str, ...]:
    """Return every registered plant id, sorted."""

    return tuple(sorted(_REGISTRATIONS))


def registered_plants() -> Mapping[str, PlantRegistration]:
    """Return a read-only snapshot of the registry."""

    return MappingProxyType(dict(_REGISTRATIONS))


def unregister_plant(plant_id: str) -> bool:
    """Remove one plant; returns whether it was present."""

    return _REGISTRATIONS.pop(plant_id, None) is not None


def clear_registrations() -> None:
    """Reset the whole module state.  Used by tests only."""

    global _DISCOVERY_ATTEMPTED
    _REGISTRATIONS.clear()
    _ATTEMPTED_ENTRY_POINTS.clear()
    _DISCOVERY_ERRORS.clear()
    _DISCOVERY_ATTEMPTED = False


def _load_candidate(entry_point: Any) -> PlantRegistration:
    """Load one entry point as a registration or as a zero-argument factory."""

    loaded = entry_point.load()
    if isinstance(loaded, PlantRegistration):
        return loaded
    if not callable(loaded):
        raise TypeError(
            "entry point must expose a PlantRegistration or a zero-argument "
            f"callable returning one, got {type(loaded).__name__}"
        )
    produced = loaded()
    if not isinstance(produced, PlantRegistration):
        raise TypeError(
            "entry point callable must return a PlantRegistration, "
            f"got {type(produced).__name__}"
        )
    return produced


def discover_entry_points() -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Load plants declared in the ``pe_sim.plants`` entry-point group.

    Returns ``(loaded_ids, errors)``.  Nothing is raised: a plugin that fails
    to import, that returns the wrong shape, or that collides with an existing
    id is reported as a string and the remaining plugins are still loaded.
    Repeated calls do not retry an entry point name that was already
    attempted, so the function is idempotent and cannot emit duplicate errors.
    """

    global _DISCOVERY_ATTEMPTED
    _DISCOVERY_ATTEMPTED = True
    loaded_ids: list[str] = []
    errors: list[str] = []
    try:
        entry_points = importlib.metadata.entry_points(group=ENTRY_POINT_GROUP)
    except Exception as exc:  # pragma: no cover - metadata backend failure
        message = f"cannot enumerate entry points in group {ENTRY_POINT_GROUP!r}: {type(exc).__name__}: {exc}"
        _DISCOVERY_ERRORS.append(message)
        return (), (message,)
    for entry_point in entry_points:
        name = str(getattr(entry_point, "name", entry_point))
        if name in _ATTEMPTED_ENTRY_POINTS:
            continue
        _ATTEMPTED_ENTRY_POINTS.add(name)
        try:
            registration = _load_candidate(entry_point)
            # A third-party distribution cannot grant itself a reviewed trust
            # level or export eligibility.  Both are project decisions, so they
            # are forced here rather than trusted from the plugin's declaration.
            registration = replace(registration, trust_level="third-party", export_eligible=False)
            register_plant(registration)
        except Exception as exc:
            errors.append(f"{name}: {type(exc).__name__}: {exc}")
            continue
        loaded_ids.append(registration.plant_id)
    _DISCOVERY_ERRORS.extend(errors)
    return tuple(loaded_ids), tuple(errors)


def resolve(plant_id: str) -> PlantRegistration:
    """Return a plant, discovering third-party entry points at most once.

    The built-in path never imports third-party code: discovery only happens
    when an id is not already registered.
    """

    registration = _REGISTRATIONS.get(plant_id)
    if registration is not None:
        return registration
    if not _DISCOVERY_ATTEMPTED:
        discover_entry_points()
        registration = _REGISTRATIONS.get(plant_id)
        if registration is not None:
            return registration
    raise KeyError(
        f"plant_id {plant_id!r} is not registered; registered: {sorted(_REGISTRATIONS)}; "
        f"entry-point discovery errors: {list(_DISCOVERY_ERRORS)}"
    )


__all__ = [
    "ENTRY_POINT_GROUP",
    "TRUST_LEVELS",
    "PlantRegistration",
    "clear_registrations",
    "discover_entry_points",
    "get_plant",
    "plant_ids",
    "register_plant",
    "registered_plants",
    "resolve",
    "unregister_plant",
]

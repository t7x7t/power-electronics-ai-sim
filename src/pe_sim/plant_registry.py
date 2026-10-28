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

The optional ``topology_exporter`` is the registration's own projection from
recorded manifest parameters to the reviewed display descriptor.  The registry
keeps the callable in-process and reports only whether one was declared, so a
consumer can decide *whether* a plant is displayable without this module
gaining knowledge of any topology.
"""

from __future__ import annotations

import importlib.metadata
import re
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Any, Callable, Mapping

from .time_model import (
    CONTROL_LOCATION_KERNEL,
    EVENT_GRANULARITY_WINDOW,
    TIME_MODEL_STEPPED,
    RouteCapabilities,
)


# Declared provenance classes.  ``reference`` means the project reviewed the
# implementation; ``fixture`` means it exists for tests and examples;
# ``third-party`` is anything the project did not review.
TRUST_LEVELS: tuple[str, ...] = ("reference", "third-party", "fixture")

# Entry-point group a third-party distribution uses to ship a plant.  The
# group name is a contract with those distributions and is not a plant list.
ENTRY_POINT_GROUP = "pe_sim.plants"

# Schema tag for the read-only discovery document returned by
# ``plant_catalog``.  A client branches on this tag, not on module layout.
PLANT_CATALOG_SCHEMA = "pe-sim.plant-catalog.v1"

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
    how the kernel behaves.  ``time_model``, ``control_location``, and
    ``event_granularity`` are likewise declarations: the registry validates
    their vocabulary and records them for the manifest, but it never branches
    on their values.  ``topology_exporter`` is an optional ``parameters ->
    descriptor`` callable owned by the registration; ``None`` means the plant
    publishes no display topology, and the catalog reports that as
    ``topology_export: false``.
    """

    plant_id: str
    backend_id: str
    factory: Callable[..., Any]
    class_name: str
    schedule_keys: frozenset[str] = _DEFAULT_SCHEDULE_KEYS
    config_keys: frozenset[str] | None = None
    trust_level: str = "third-party"
    export_eligible: bool = False
    #: ``stepped`` means the kernel owns the clock and the adapter must advance
    #: to the requested endpoint; ``whole_run`` means the engine owns its time
    #: axis.  Both are recorded as data; only the evidence cap differs.
    time_model: str = TIME_MODEL_STEPPED
    #: ``kernel`` means a kernel-owned ControllerAdapter closes the loop;
    #: ``model`` means the compensator lives inside the model or the netlist.
    control_location: str = CONTROL_LOCATION_KERNEL
    #: ``window`` means the controller observes and actuates at control-window
    #: boundaries (the solve still honours declared change points since ADR
    #: 0004 step 2); ``continuous`` declares the model reviewed for mid-window
    #: changes and reports no quantization limitation.
    event_granularity: str = EVENT_GRANULARITY_WINDOW
    #: Builds the reviewed display descriptor from the parameters a run records
    #: in its manifest.  The registry validates that it is callable and reports
    #: whether it exists; it never calls it and never imports a topology module.
    topology_exporter: Callable[[Mapping[str, Any]], Any] | None = None
    #: Declared JSON Schema for ``plant_config``; ``None`` means the plant
    #: publishes no machine-readable parameter surface.  The registry checks the
    #: shape only (a closed object whose properties match ``config_keys``); it
    #: never validates a document against this schema.  ``compare=False`` and
    #: ``hash=False`` are required because the value is a mapping: including it
    #: in equality would make ``load_builtin_plants`` depend on dict identity and
    #: ordering, and including it in the hash would make the frozen registration
    #: unhashable.
    parameter_schema: Mapping[str, Any] | None = field(
        default=None, repr=False, compare=False, hash=False
    )


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


def _freeze_parameter_schema(
    plant_id: str,
    value: Any,
    config_keys: frozenset[str] | None,
) -> Mapping[str, Any] | None:
    """Validate the declared schema's shape and freeze it for publishing.

    This module must not import ``pe_sim.plant_parameters``: it is the
    kernel-side registry and depends on no declaration table, adapter, or
    engine.  What it can still enforce is the promise the catalog makes.  The
    only reason to declare a schema here is to publish a *closed* object, so
    ``type: object`` and ``additionalProperties: false`` are mandatory; an
    open schema would accept a key the factory then rejects, which is exactly
    the contradiction this declaration exists to remove.

    When ``config_keys`` is also declared, the two key sets must be identical.
    The catalog and the validator are two views of one contract: if the
    catalog advertised one set of keys while validation accepted another, an
    agent would get two contradictory answers for the same plant.

    Both published levels are frozen, so a caller holding the original dict
    cannot change what was registered after the fact.  Nested property entries
    stay ordinary dicts; only the key set is part of this module's contract.
    """

    if value is None:
        return None
    try:
        declared = dict(value)
    except (TypeError, ValueError):
        raise TypeError(
            f"plant_id {plant_id!r} parameter_schema must be a mapping or None, "
            f"got {type(value).__name__}"
        ) from None
    if declared.get("type") != "object":
        raise ValueError(
            f"plant_id {plant_id!r} parameter_schema must declare type 'object', "
            f"got {declared.get('type')!r}"
        )
    if declared.get("additionalProperties") is not False:
        raise ValueError(
            f"plant_id {plant_id!r} parameter_schema must declare "
            "'additionalProperties': false; an open schema would accept keys "
            "the factory rejects, which is the only reason to declare one"
        )
    properties = declared.get("properties")
    if not isinstance(properties, Mapping):
        raise ValueError(
            f"plant_id {plant_id!r} parameter_schema must declare a 'properties' "
            f"mapping, got {type(properties).__name__}"
        )
    if config_keys is not None:
        declared_keys = set(properties)
        if declared_keys != set(config_keys):
            raise ValueError(
                f"plant_id {plant_id!r} declares config_keys "
                f"{sorted(set(config_keys), key=str)} but its parameter_schema "
                f"properties {sorted(declared_keys, key=str)}; the catalog and "
                "the validator must accept the same keys"
            )
    return MappingProxyType(
        {**declared, "properties": MappingProxyType(dict(properties))}
    )


def _json_safe(value: Any) -> Any:
    """Rebuild read-only containers as plain JSON-safe data.

    ``plant_catalog`` is consumed as JSON, and ``MappingProxyType`` is not
    serializable; the copy also keeps a client from mutating the registered
    declaration through the value it receives.  Only containers are rebuilt;
    scalars pass through unchanged.
    """

    if isinstance(value, Mapping):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


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
    # The route declarations share one validation definition with the runtime
    # record, so the vocabulary and the ``whole_run + kernel`` contradiction are
    # refused here, at registration, rather than during a run.
    try:
        RouteCapabilities(
            time_model=registration.time_model,
            control_location=registration.control_location,
            event_granularity=registration.event_granularity,
        )
    except ValueError as exc:
        raise ValueError(f"plant_id {plant_id!r}: {exc}") from None
    if not isinstance(registration.export_eligible, bool):
        raise TypeError(f"plant_id {plant_id!r} export_eligible must be a bool")
    if registration.topology_exporter is not None and not callable(registration.topology_exporter):
        raise TypeError(
            f"plant_id {plant_id!r} topology_exporter must be callable or None, "
            f"got {type(registration.topology_exporter).__name__}"
        )
    schedule_keys = _freeze_keys("schedule_keys", registration.schedule_keys)
    config_keys = _freeze_keys("config_keys", registration.config_keys)
    parameter_schema = _freeze_parameter_schema(
        plant_id, registration.parameter_schema, config_keys
    )
    return replace(
        registration,
        schedule_keys=schedule_keys,
        config_keys=config_keys,
        parameter_schema=parameter_schema,
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


def plant_catalog(*, discover: bool = False) -> dict[str, Any]:
    """Return the declared shape of every registered plant.

    This is the read-only answer to "what can I ask the kernel to run?".  It
    exposes declarations only: the factory stays in-process, because a client
    needs to know which inputs a plant accepts, not how the kernel builds it.
    ``discover=True`` also inspects the entry-point group; the default does
    not, so importing or introspecting the registry never imports third-party
    code by accident.
    """

    if discover:
        discover_entry_points()
    plants = [
        {
            "plant_id": registration.plant_id,
            "backend_id": registration.backend_id,
            "class_name": registration.class_name,
            "time_model": registration.time_model,
            "control_location": registration.control_location,
            "event_granularity": registration.event_granularity,
            "schedule_keys": sorted(registration.schedule_keys),
            "config_keys": None
            if registration.config_keys is None
            else sorted(registration.config_keys),
            # Published as plain JSON rather than as the frozen mapping the
            # registry keeps, so a client can serialize (and own) its copy.
            "parameter_schema": _json_safe(registration.parameter_schema),
            "trust_level": registration.trust_level,
            "export_eligible": registration.export_eligible,
            # A capability flag, never the callable: the catalog stays JSON-safe
            # and a client cannot acquire an in-process builder from it.
            "topology_export": registration.topology_exporter is not None,
        }
        for _, registration in sorted(_REGISTRATIONS.items())
    ]
    return {
        "schema": PLANT_CATALOG_SCHEMA,
        "trust_levels": list(TRUST_LEVELS),
        "plants": plants,
    }


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
    "PLANT_CATALOG_SCHEMA",
    "TRUST_LEVELS",
    "PlantRegistration",
    "clear_registrations",
    "discover_entry_points",
    "get_plant",
    "plant_catalog",
    "plant_ids",
    "register_plant",
    "registered_plants",
    "resolve",
    "unregister_plant",
]

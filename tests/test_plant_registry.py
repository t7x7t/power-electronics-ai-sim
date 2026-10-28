"""Plants are data: the registry, entry-point discovery, and the solve-path pin.

These tests hold the kernel to one rule: it knows the *shape* of a plant, not a
list of plant names.  A new plant is a registration (built-in or third-party),
and a new engine is a declared solve path, never a kernel edit.
"""

from __future__ import annotations

import importlib.metadata
import json
import sys

import pytest

from pe_sim import builtin_plants, identity, plant_registry, time_model
from pe_sim.cli import main as cli_main
from pe_sim.contracts import Timebase
from pe_sim.plant_registry import (
    ENTRY_POINT_GROUP,
    PLANT_CATALOG_SCHEMA,
    TRUST_LEVELS,
    PlantRegistration,
    clear_registrations,
    discover_entry_points,
    get_plant,
    plant_catalog,
    plant_ids,
    register_plant,
    registered_plants,
    resolve,
    unregister_plant,
)


_EXPECTED_SOLVE_PATHS: dict[str, tuple[str, ...]] = {
    "ngspice-buck": (
        "backends/ngspice_plants.py",
        "backends/netlist.py",
        "backends/netlist_builder.py",
        "backends/process.py",
        "backends/settings.py",
        "backends/wrdata.py",
    ),
    "buck": ("reference_plants.py",),
    "boost": ("reference_plants.py",),
}

_EXPECTED_TRUST = {
    "fake": "fixture",
    "fake-load": "fixture",
    "buck": "reference",
    "boost": "reference",
    "model-control-buck": "reference",
    "multiphase-buck": "reference",
    "ngspice-buck": "reference",
    "peak-current-buck": "reference",
    "ngspice-sync-buck": "reference",
    "whole-run-buck": "reference",
    "whole-run-switching-buck": "reference",
}


@pytest.fixture(autouse=True)
def _isolated_registry():
    """The registry is process-wide, so every test starts and ends empty."""

    clear_registrations()
    yield
    clear_registrations()


@pytest.fixture(autouse=True)
def _isolated_solve_paths(monkeypatch):
    """Runtime solve-path declarations are process-wide too."""

    monkeypatch.setattr(identity, "_MEMORY_SOLVE_PATHS", {})
    yield


def _registration(plant_id: str = "demo", **overrides) -> PlantRegistration:
    values = {
        "plant_id": plant_id,
        "backend_id": plant_id,
        "factory": lambda config, context: object(),
        "class_name": "DemoPlant",
        "trust_level": "third-party",
        "export_eligible": False,
    }
    values.update(overrides)
    return PlantRegistration(**values)


class _FakeEntryPoint:
    """Minimal stand-in for ``importlib.metadata.EntryPoint``."""

    def __init__(self, name: str, loader):
        self.name = name
        self._loader = loader

    def load(self):
        return self._loader()


def _raise_plugin_error():
    raise RuntimeError("plugin exploded while importing")


def _entry_points_returning(*entry_points):
    def _fake(**kwargs):
        assert kwargs.get("group") == ENTRY_POINT_GROUP
        return tuple(entry_points)

    return _fake


# ---------------------------------------------------------------------------
# Registration, lookup, and replacement
# ---------------------------------------------------------------------------


def test_register_and_query_round_trip():
    registration = _registration("demo", trust_level="reference", export_eligible=True)

    register_plant(registration)

    assert get_plant("demo") == registration
    assert plant_ids() == ("demo",)
    assert dict(registered_plants()) == {"demo": registration}


def test_duplicate_registration_is_rejected_and_replace_overrides():
    register_plant(_registration("demo"))
    replacement = _registration("demo", backend_id="other", class_name="OtherPlant")

    with pytest.raises(ValueError, match="replace=True"):
        register_plant(replacement)

    assert get_plant("demo").class_name == "DemoPlant"
    register_plant(replacement, replace=True)
    assert get_plant("demo").class_name == "OtherPlant"


def test_registered_plants_is_a_read_only_snapshot():
    register_plant(_registration("demo"))
    snapshot = registered_plants()

    with pytest.raises(TypeError):
        snapshot["other"] = _registration("other")  # type: ignore[index]

    register_plant(_registration("later"))
    assert "later" not in snapshot
    assert "later" in registered_plants()


def test_unregister_removes_only_the_named_plant():
    register_plant(_registration("keep"))
    register_plant(_registration("drop"))

    assert unregister_plant("drop") is True
    assert unregister_plant("drop") is False
    assert plant_ids() == ("keep",)


def test_schedule_keys_are_normalized_to_a_frozenset():
    register_plant(_registration("demo", schedule_keys={"time_s"}))

    assert get_plant("demo").schedule_keys == frozenset({"time_s"})


# ---------------------------------------------------------------------------
# Rejected registrations
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_id",
    ["", "../x", "a/b", "..", ".hidden", "with space", "trailing\n", "a:b"],
)
def test_invalid_plant_ids_are_rejected(bad_id):
    with pytest.raises(ValueError, match="invalid plant_id"):
        register_plant(_registration(bad_id))

    assert plant_ids() == ()


@pytest.mark.parametrize("bad_trust", ["trusted", "", "Reference", None])
def test_invalid_trust_levels_are_rejected(bad_trust):
    with pytest.raises(ValueError, match="trust_level"):
        register_plant(_registration("demo", trust_level=bad_trust))


def test_every_declared_trust_level_is_accepted():
    for index, trust_level in enumerate(TRUST_LEVELS):
        register_plant(_registration(f"demo-{index}", trust_level=trust_level))

    assert {item.trust_level for item in registered_plants().values()} == set(TRUST_LEVELS)


def test_non_callable_factory_is_rejected():
    with pytest.raises(TypeError, match="callable"):
        register_plant(_registration("demo", factory="not callable"))


def test_empty_backend_id_is_rejected():
    with pytest.raises(ValueError, match="backend_id"):
        register_plant(_registration("demo", backend_id="   "))


def test_missing_plant_lists_the_registered_ids():
    register_plant(_registration("alpha"))
    register_plant(_registration("beta"))

    with pytest.raises(KeyError) as excinfo:
        get_plant("missing")

    message = excinfo.value.args[0]
    assert "missing" in message
    assert "alpha" in message
    assert "beta" in message


# ---------------------------------------------------------------------------
# Lazy entry-point discovery
# ---------------------------------------------------------------------------


def test_unknown_plant_discovers_entry_points_only_once(monkeypatch):
    calls: list[object] = []

    def _counting_entry_points(**kwargs):
        calls.append(kwargs.get("group"))
        return ()

    monkeypatch.setattr(importlib.metadata, "entry_points", _counting_entry_points)

    with pytest.raises(KeyError):
        resolve("missing")
    with pytest.raises(KeyError):
        resolve("still-missing")

    assert calls == [ENTRY_POINT_GROUP]


def test_a_registered_plant_never_touches_entry_points(monkeypatch):
    calls: list[str] = []

    def _counting_entry_points(**kwargs):  # pragma: no cover - must not run
        calls.append(kwargs.get("group"))
        return ()

    monkeypatch.setattr(importlib.metadata, "entry_points", _counting_entry_points)
    register_plant(_registration("mine"))

    assert resolve("mine").plant_id == "mine"
    assert get_plant("mine").plant_id == "mine"
    assert calls == []


def test_a_broken_plugin_does_not_block_the_others(monkeypatch):
    good = _registration("good-plugin", class_name="GoodPlant")
    monkeypatch.setattr(
        importlib.metadata,
        "entry_points",
        _entry_points_returning(
            _FakeEntryPoint("broken", _raise_plugin_error),
            _FakeEntryPoint("good", lambda: good),
        ),
    )

    loaded_ids, errors = discover_entry_points()

    assert loaded_ids == ("good-plugin",)
    assert get_plant("good-plugin") == good
    assert len(errors) == 1
    assert "broken" in errors[0]
    assert "RuntimeError" in errors[0]
    assert "plugin exploded" in errors[0]


def test_entry_point_may_be_a_zero_argument_factory(monkeypatch):
    registration = _registration("lazy-plugin")
    monkeypatch.setattr(
        importlib.metadata,
        "entry_points",
        _entry_points_returning(_FakeEntryPoint("lazy", lambda: (lambda: registration))),
    )

    loaded_ids, errors = discover_entry_points()

    assert loaded_ids == ("lazy-plugin",)
    assert errors == ()
    assert resolve("lazy-plugin") == registration


def test_entry_point_with_the_wrong_shape_is_reported(monkeypatch):
    monkeypatch.setattr(
        importlib.metadata,
        "entry_points",
        _entry_points_returning(_FakeEntryPoint("odd", lambda: 42)),
    )

    loaded_ids, errors = discover_entry_points()

    assert loaded_ids == ()
    assert len(errors) == 1
    assert "TypeError" in errors[0]


def test_entry_point_conflict_is_an_error_not_a_silent_override(monkeypatch):
    register_plant(_registration("taken", class_name="First"))
    monkeypatch.setattr(
        importlib.metadata,
        "entry_points",
        _entry_points_returning(
            _FakeEntryPoint("taken", lambda: _registration("taken", class_name="Second"))
        ),
    )

    loaded_ids, errors = discover_entry_points()

    assert loaded_ids == ()
    assert get_plant("taken").class_name == "First"
    assert "taken" in errors[0]
    assert "replace=True" in errors[0]


def test_discovery_is_idempotent(monkeypatch):
    registration = _registration("once")
    monkeypatch.setattr(
        importlib.metadata,
        "entry_points",
        _entry_points_returning(_FakeEntryPoint("once", lambda: registration)),
    )

    first = discover_entry_points()
    second = discover_entry_points()

    assert first == (("once",), ())
    assert second == ((), ())


def test_resolve_reports_discovery_errors(monkeypatch):
    monkeypatch.setattr(
        importlib.metadata,
        "entry_points",
        _entry_points_returning(_FakeEntryPoint("broken", _raise_plugin_error)),
    )

    with pytest.raises(KeyError) as excinfo:
        resolve("missing")

    message = excinfo.value.args[0]
    assert "broken" in message
    assert "RuntimeError" in message


def test_direct_discovery_does_not_import_anything_when_nothing_is_declared(monkeypatch):
    monkeypatch.setattr(importlib.metadata, "entry_points", lambda **kwargs: ())

    assert discover_entry_points() == ((), ())
    assert plant_ids() == ()


# ---------------------------------------------------------------------------
# The kernel does not name engines: solve paths are declared data
# ---------------------------------------------------------------------------


def test_runtime_solve_path_registration_makes_a_digest_available():
    identity.register_solve_path("third-party-engine", ["reference_plants.py"])

    assert identity.declared_solve_paths()["third-party-engine"] == ("reference_plants.py",)
    assert identity.implementation_digest("third-party-engine") == identity.implementation_digest("buck")
    assert identity.implementation_sources("third-party-engine") == identity.implementation_sources("buck")


def test_runtime_declaration_wins_over_the_checked_in_pin():
    identity.register_solve_path("buck", ["runtime.py"])

    assert identity.declared_solve_paths()["buck"] == ("runtime.py",)
    assert set(identity.implementation_sources("buck")) == {"runtime.py"}


@pytest.mark.parametrize("modules", [["../escape.py"], ["/absolute.py"], ["backends/../../x.py"]])
def test_solve_paths_must_stay_inside_the_package(modules):
    with pytest.raises(ValueError):
        identity.register_solve_path("escaping", modules)


def test_an_undeclared_backend_must_be_registered_first():
    with pytest.raises(KeyError) as excinfo:
        identity.implementation_digest("not-declared")

    message = excinfo.value.args[0]
    assert "not-declared" in message
    assert "register_solve_path" in message


def test_the_review_pin_declares_a_solve_path_for_every_backend():
    backends = identity.load_reviewed_identities()["backends"]

    assert set(backends) == set(_EXPECTED_SOLVE_PATHS)
    for backend_id, expected in _EXPECTED_SOLVE_PATHS.items():
        assert tuple(backends[backend_id]["solve_path"]) == expected
        assert identity.declared_solve_paths()[backend_id] == expected


# ---------------------------------------------------------------------------
# Composition root
# ---------------------------------------------------------------------------


def test_load_builtin_plants_is_idempotent_and_complete():
    builtin_plants.load_builtin_plants()
    first = dict(registered_plants())

    builtin_plants.load_builtin_plants()

    assert dict(registered_plants()) == first
    assert plant_ids() == tuple(sorted(_EXPECTED_TRUST))
    for plant_id, trust_level in _EXPECTED_TRUST.items():
        registration = get_plant(plant_id)
        assert registration.trust_level == trust_level
        # The whole-run route now declares a reviewed topology exporter, so it
        # is export-eligible; the fixture plants are eligible but publish no
        # display topology.
        assert registration.export_eligible is True
        assert registration.topology_exporter is not None or plant_id in ("fake", "fake-load")
        assert registration.class_name.endswith("Plant")


def test_cli_resolves_plants_through_the_registry():
    """The CLI must not keep its own plant table: it resolves the registry."""

    from pe_sim.cli import _select_registration

    builtin_plants.load_builtin_plants()

    for plant_id in plant_ids():
        registration = _select_registration({"plant_id": plant_id}, None)
        assert registration is get_plant(plant_id)
        assert registration.backend_id == plant_id


def test_cli_rejects_a_mismatched_backend_for_a_registered_plant():
    from pe_sim.cli import _select_registration

    builtin_plants.load_builtin_plants()

    with pytest.raises(ValueError, match="requires --backend"):
        _select_registration({"plant_id": "buck"}, "boost")


def test_builtin_factories_build_the_declared_class():
    builtin_plants.load_builtin_plants()
    context = builtin_plants.PlantBuildContext()

    for plant_id in ("fake", "fake-load", "buck", "boost"):
        registration = get_plant(plant_id)
        assert type(registration.factory({}, context)).__name__ == registration.class_name


def test_builtin_factories_reject_unknown_configuration_keys():
    builtin_plants.load_builtin_plants()
    context = builtin_plants.PlantBuildContext()

    for plant_id in ("fake", "fake-load", "buck", "boost", "ngspice-buck"):
        with pytest.raises(ValueError, match="plant_config"):
            get_plant(plant_id).factory({"not_a_key": 1}, context)


def test_ngspice_factory_preview_builds_without_an_executable():
    builtin_plants.load_builtin_plants()
    context = builtin_plants.PlantBuildContext(
        timebase=Timebase(control_period_s=10e-6),
        require_executable=False,
    )

    plant = get_plant("ngspice-buck").factory({"keep_artifacts": False}, context)

    assert type(plant).__name__ == "NgspiceBuckPlant"
    assert plant.control_period_s == pytest.approx(10e-6)


def test_ngspice_factory_derives_the_control_period_from_the_timebase():
    builtin_plants.load_builtin_plants()

    with pytest.raises(ValueError, match="control_period_s"):
        get_plant("ngspice-buck").factory({}, builtin_plants.PlantBuildContext(require_executable=False))


def test_builtin_loading_refuses_to_shadow_a_third_party_registration():
    register_plant(_registration("buck", class_name="ShadowBuck"))

    with pytest.raises(ValueError, match="already registered"):
        builtin_plants.load_builtin_plants()

    assert plant_ids() == ("buck",)
    assert get_plant("buck").class_name == "ShadowBuck"


def test_registry_module_is_pure():
    """The kernel-side registry imports no adapter and no kernel module."""

    imports = _imports_of(plant_registry)

    # The registry may learn the declared time-model vocabulary, but only from
    # the kernel-side module that owns it.
    assert {name for name in imports if name.startswith(".")} == {".time_model"}
    assert not [name for name in imports if "backends" in name]

    # That module is pure too: stdlib only, no adapter, no kernel module.
    time_model_imports = _imports_of(time_model)
    assert {name for name in time_model_imports if name.startswith(".")} == set()
    assert not [name for name in time_model_imports if "backends" in name]
    top_level = {name.split(".", 1)[0] for name in time_model_imports}
    assert top_level <= set(sys.stdlib_module_names) | {"__future__"}


def test_plant_catalog_reports_declarations_not_implementation(capsys):
    assert cli_main(["list-plants"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["schema"] == PLANT_CATALOG_SCHEMA
    assert payload["trust_levels"] == list(TRUST_LEVELS)
    entries = {item["plant_id"]: item for item in payload["plants"]}
    assert set(_EXPECTED_TRUST) <= set(entries)
    assert entries["buck"]["backend_id"] == "buck"
    assert entries["buck"]["trust_level"] == "reference"
    assert entries["buck"]["export_eligible"] is True
    assert entries["buck"]["time_model"] == "stepped"
    assert entries["buck"]["control_location"] == "kernel"
    assert entries["buck"]["event_granularity"] == "window"
    assert {item["time_model"] for item in payload["plants"]} == {
        "stepped",
        "whole_run",
    }
    assert "factory" not in entries["buck"], "a client needs the shape, not the builder"
    assert entries["buck"]["schedule_keys"] == sorted(entries["buck"]["schedule_keys"])

    direct = plant_catalog()
    assert list(direct) == ["schema", "trust_levels", "plants"]


def _imports_of(module) -> set[str]:
    import ast
    from pathlib import Path

    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names.add("." * node.level + (node.module or ""))
        elif isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
    return names

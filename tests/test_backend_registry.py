"""Registry tests for supported and planned simulation backends."""

from __future__ import annotations

import json

import pytest

from pe_sim.backends.registry import (
    PLANNED,
    SUPPORTED,
    current_platform,
    get_backend,
    list_backends,
)


def test_registry_contains_a_supported_executable_backend():
    ngspice = get_backend("ngspice")

    assert ngspice.status == SUPPORTED
    assert ngspice.kind == "executable"
    assert ngspice.config_section == "ngspice"
    assert "ngspice_con.exe" in ngspice.executable_names
    assert ngspice.console_sibling == "ngspice_con.exe"


def test_planned_backends_are_explicitly_marked():
    planned = [item for item in list_backends() if item.status == PLANNED]

    assert {item.name for item in planned} >= {"xyce", "ltspice"}
    assert all(item.notes for item in planned)


def test_install_recipes_are_available_for_the_current_platform():
    ngspice = get_backend("ngspice")

    recipes = ngspice.recipes_for(current_platform())

    assert recipes
    assert all(recipe.commands for recipe in recipes)


def test_descriptor_serialises_to_json():
    payload = get_backend("ngspice").to_dict()

    json.dumps(payload, allow_nan=False)
    assert payload["name"] == "ngspice"
    assert payload["install_recipes"]


def test_unknown_backend_name_fails_closed():
    with pytest.raises(KeyError):
        get_backend("definitely-not-a-simulator")

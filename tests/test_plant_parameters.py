"""The agent-facing parameter table must match the factories it describes.

A declaration that drifts from ``builtin_plants`` is worse than no declaration:
an agent would write a document the factory rejects, or omit a field the
factory would have defaulted.  These tests import the private ``*_DEFAULTS``
dictionaries and the fixture constructor on purpose, so a factory change fails
here until the declaration is updated in the same commit.
"""

from __future__ import annotations

import inspect

import pytest

from pe_sim.builtin_plants import (
    _FAKE_CONFIG_KEYS,
    _MODEL_CONTROL_BUCK_DEFAULTS,
    _MULTIPHASE_BUCK_DEFAULTS,
    _NGSPICE_BUCK_DEFAULTS,
    _NGSPICE_SYNC_BUCK_DEFAULTS,
    _PEAK_CURRENT_BUCK_DEFAULTS,
    _REFERENCE_PLANT_DEFAULTS,
    _WHOLE_RUN_BUCK_DEFAULTS,
)
from pe_sim.multiphase_plants import _phase_count
from pe_sim.plant_parameters import (
    PLANT_PARAMETERS,
    PLANT_PARAMETERS_SCHEMA,
    ParameterSpec,
    describe_plant,
    parameter_schema,
)
from pe_sim.runtime import FakePlant

_EXPECTED_PLANTS = frozenset(
    {
        "fake",
        "fake-load",
        "buck",
        "boost",
        "multiphase-buck",
        "ngspice-buck",
        "ngspice-sync-buck",
        "whole-run-buck",
        "model-control-buck",
        "peak-current-buck",
        "whole-run-switching-buck",
    }
)

_DEFAULTS_BY_PLANT = {
    "buck": _REFERENCE_PLANT_DEFAULTS,
    "boost": _REFERENCE_PLANT_DEFAULTS,
    "multiphase-buck": _MULTIPHASE_BUCK_DEFAULTS,
    "ngspice-buck": _NGSPICE_BUCK_DEFAULTS,
    "ngspice-sync-buck": _NGSPICE_SYNC_BUCK_DEFAULTS,
    "whole-run-buck": _WHOLE_RUN_BUCK_DEFAULTS,
    "model-control-buck": _MODEL_CONTROL_BUCK_DEFAULTS,
    "peak-current-buck": _PEAK_CURRENT_BUCK_DEFAULTS,
    "whole-run-switching-buck": _MODEL_CONTROL_BUCK_DEFAULTS,
}


def test_every_builtin_plant_is_declared():
    assert set(PLANT_PARAMETERS) == set(_EXPECTED_PLANTS)


@pytest.mark.parametrize("plant_id", sorted(_DEFAULTS_BY_PLANT))
def test_declared_keys_match_the_factory_defaults(plant_id):
    assert set(PLANT_PARAMETERS[plant_id]) == set(_DEFAULTS_BY_PLANT[plant_id])


@pytest.mark.parametrize("plant_id", sorted(_DEFAULTS_BY_PLANT))
def test_declared_defaults_match_the_factory_defaults(plant_id):
    expected = _DEFAULTS_BY_PLANT[plant_id]
    for name, spec in PLANT_PARAMETERS[plant_id].items():
        assert spec.default == expected[name], name
        assert type(spec.default) is type(expected[name]), name


@pytest.mark.parametrize("plant_id", ["fake", "fake-load"])
def test_fixture_plants_match_the_fixture_keys_and_signature(plant_id):
    declared = PLANT_PARAMETERS[plant_id]
    assert set(declared) == set(_FAKE_CONFIG_KEYS)

    signature = inspect.signature(FakePlant.__init__).parameters
    factory_defaults = {
        name: signature[name].default for name in _FAKE_CONFIG_KEYS
    }
    assert {name: spec.default for name, spec in declared.items()} == factory_defaults


@pytest.mark.parametrize("plant_id", sorted(PLANT_PARAMETERS))
def test_parameter_schema_closes_the_object(plant_id):
    schema = parameter_schema(plant_id)
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert set(schema["properties"]) == set(PLANT_PARAMETERS[plant_id])


def test_parameter_schema_translates_types_bounds_and_units():
    properties = parameter_schema("ngspice-sync-buck")["properties"]

    assert properties["input_voltage_v"]["type"] == "number"
    assert properties["input_voltage_v"]["exclusiveMinimum"] == 0.0
    assert "minimum" not in properties["input_voltage_v"]
    assert properties["input_voltage_v"]["x-unit"] == "V"
    assert properties["control_period_s"]["type"] == ["number", "null"]
    assert properties["control_period_s"]["default"] is None
    assert properties["keep_artifacts"]["type"] == "boolean"
    assert "x-unit" not in properties["keep_artifacts"]
    assert properties["dead_time_s"]["x-unit"] == "s"


def test_parameter_schema_declares_integers_and_duty_bounds():
    multiphase = parameter_schema("multiphase-buck")["properties"]
    assert multiphase["phases"]["type"] == "integer"
    # The declaration must copy the model, not a guess: ``_phase_count``
    # rejects everything outside [2, 8].  A wider declared bound would let an
    # agent write a document that passes this check and fails only when the
    # factory builds the plant, which is exactly the late failure this
    # declaration exists to prevent.
    assert multiphase["phases"]["minimum"] == 2
    assert multiphase["phases"]["maximum"] == 8
    _phase_count(2)
    _phase_count(8)
    for rejected in (1, 9):
        with pytest.raises(ValueError):
            _phase_count(rejected)

    whole_run = parameter_schema("whole-run-buck")["properties"]
    assert whole_run["output_min"]["minimum"] == 0.0
    assert whole_run["output_max"]["maximum"] == 1.0
    assert "x-unit" not in whole_run["output_min"]


def test_describe_plant_is_deterministic_and_sorted():
    first = describe_plant("buck")
    second = describe_plant("buck")

    assert first == second
    names = [entry["name"] for entry in first["parameters"]]
    assert names == sorted(names)
    assert first["schema"] == PLANT_PARAMETERS_SCHEMA
    assert first["plant_id"] == "buck"
    assert first["unknown_keys"] == "rejected"
    assert first["parameter_schema"] == parameter_schema("buck")


def test_unknown_plant_names_every_known_plant():
    with pytest.raises(KeyError) as info:
        parameter_schema("no-such-plant")
    text = str(info.value)
    for plant_id in PLANT_PARAMETERS:
        assert plant_id in text

    with pytest.raises(KeyError):
        describe_plant("no-such-plant")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"name": "x", "kind": "string", "unit": None, "description": "text"},
        {"name": "x", "kind": "number", "unit": "V", "minimum": 2.0, "maximum": 1.0},
        {
            "name": "x",
            "kind": "integer",
            "unit": None,
            "nullable": True,
            "description": "dimensionless count",
        },
        {"name": "x", "kind": "number", "unit": None, "description": ""},
        {"name": "x", "kind": "number", "unit": "V", "exclusive_minimum": True},
    ],
)
def test_parameter_spec_rejects_impossible_declarations(kwargs):
    with pytest.raises(ValueError):
        ParameterSpec(**kwargs)


def test_parameter_spec_accepts_a_unitless_declaration_with_a_stated_dimension():
    spec = ParameterSpec(
        name="kp", kind="number", unit=None, description="Dimensionless proportional gain."
    )
    assert spec.unit is None
    assert spec.as_dict()["name"] == "kp"

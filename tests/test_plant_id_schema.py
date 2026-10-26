"""``plant_id`` is an identifier in the experiment schema, not a closed enum.

Legality of a plant id is owned by the runtime plant registry, so the kernel
schema must accept any well-formed identifier and leave the "is this plant
implemented?" decision to the runtime. Otherwise onboarding a new simulator
would require editing the kernel schema, which is the coupling this contract
removes.
"""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest
from jsonschema import Draft202012Validator

REPOSITORY = Path(__file__).resolve().parents[1]
SCHEMA_PATHS = {
    "repository": REPOSITORY / "schemas" / "experiment.schema.json",
    "packaged": REPOSITORY / "src" / "pe_sim" / "schemas" / "experiment.schema.json",
}

# The five identifiers the pre-extension enum allowed; they must keep working.
LEGACY_PLANT_IDS = ("fake", "fake-load", "buck", "boost", "ngspice-buck")
# Identifiers a backend plugin may introduce without touching the kernel.
EXTENSION_PLANT_IDS = ("my-engine.v2", "vendor_pack-1")
# Every sample below is rejected by the shared identifier pattern.
# NOTE: do not add samples that only differ by a trailing newline -- Python's
# ``$`` matches before a final newline, so "buck\n" is accepted by the pattern.
MALFORMED_PLANT_IDS = (
    "",
    ".",
    "..",
    "a..b",
    ".a..b",
    "a/b",
    "a\\b",
    "a b",
    "a:b",
    "buck!",
    "buck@1",
)
# ``type: string`` must reject non-string ids even though it is not an enum.
NON_STRING_PLANT_IDS = (123, None, True, ["buck"], {"id": "buck"})

# Probes used to pin ``plant_id`` to the same identifier contract as
# ``experiment_id`` rather than to a hand-written expectation.
IDENTIFIER_PROBES = (
    LEGACY_PLANT_IDS
    + EXTENSION_PLANT_IDS
    + MALFORMED_PLANT_IDS
    + ("-leading-dash", "_underscore", "UPPER.Case_1-2", ".hidden", "dot.")
)


@pytest.fixture(params=sorted(SCHEMA_PATHS))
def schema(request: pytest.FixtureRequest) -> dict:
    return json.loads(SCHEMA_PATHS[request.param].read_text(encoding="utf-8"))


def _definition(schema: dict, name: str) -> dict:
    return schema["properties"][name]


def _is_valid(definition: dict, value: object) -> bool:
    return Draft202012Validator(definition).is_valid(value)


def test_plant_id_definition_is_identical_in_both_schema_copies():
    definitions = [
        json.loads(path.read_text(encoding="utf-8"))["properties"]["plant_id"]
        for path in SCHEMA_PATHS.values()
    ]
    assert definitions[0] == definitions[1], definitions


def test_plant_id_is_no_longer_an_enum(schema):
    definition = _definition(schema, "plant_id")

    assert "enum" not in definition
    assert definition.get("type") == "string"
    assert definition.get("minLength") == 1
    assert "pattern" in definition


def test_plant_id_reuses_the_shared_identifier_contract(schema):
    plant_id = _definition(schema, "plant_id")

    assert plant_id == _definition(schema, "experiment_id")
    assert plant_id == _definition(schema, "run_id")


@pytest.mark.parametrize("plant_id", LEGACY_PLANT_IDS)
def test_legacy_enum_values_stay_valid(schema, plant_id):
    Draft202012Validator(_definition(schema, "plant_id")).validate(plant_id)


@pytest.mark.parametrize("plant_id", EXTENSION_PLANT_IDS + ("buck",))
def test_well_formed_extension_plant_ids_are_accepted(schema, plant_id):
    Draft202012Validator(_definition(schema, "plant_id")).validate(plant_id)


@pytest.mark.parametrize("plant_id", MALFORMED_PLANT_IDS)
def test_malformed_plant_ids_are_rejected(schema, plant_id):
    validator = Draft202012Validator(_definition(schema, "plant_id"))

    assert not validator.is_valid(plant_id)
    with pytest.raises(jsonschema.ValidationError):
        validator.validate(plant_id)


@pytest.mark.parametrize("plant_id", NON_STRING_PLANT_IDS)
def test_non_string_plant_ids_are_rejected(schema, plant_id):
    validator = Draft202012Validator(_definition(schema, "plant_id"))

    assert not validator.is_valid(plant_id)
    with pytest.raises(jsonschema.ValidationError):
        validator.validate(plant_id)


@pytest.mark.parametrize("probe", IDENTIFIER_PROBES)
def test_plant_id_agrees_with_the_experiment_id_identifier_pattern(schema, probe):
    assert _is_valid(_definition(schema, "plant_id"), probe) == _is_valid(
        _definition(schema, "experiment_id"), probe
    )


def test_controller_id_uses_the_same_identifier_contract_as_plant_id(schema):
    # Controller extensibility is registry-owned: the schema must accept the
    # same safe identifiers as ``plant_id`` and leave the "is this controller
    # implemented?" decision to resolution.
    controller_id = _definition(schema, "controller_id")
    plant_id = _definition(schema, "plant_id")

    assert controller_id == plant_id
    assert "enum" not in controller_id
    for probe in IDENTIFIER_PROBES:
        assert _is_valid(controller_id, probe) == _is_valid(plant_id, probe)

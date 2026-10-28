"""The agent-facing spec surface: ``describe``, ``validate``, and ``run``.

These tests go through the CLI because that is the interface an agent calls.
They hold three promises:

- the same declaration answers "what may I write?" (``describe``, the catalog)
  and "is what I wrote accepted?" (``validate``);
- validation is a compile-time check: it never builds a plant, imports an
  adapter, starts a solver, or creates a run directory;
- a rejected ``run`` carries the same structured issues, so an agent can repair
  a document instead of parsing one flattened string.
"""

from __future__ import annotations

import json
from importlib import resources
from pathlib import Path

import pytest

from pe_sim.builtin_plants import builtin_registrations, load_builtin_plants
from pe_sim.cli import main as cli_main
from pe_sim.plant_parameters import PLANT_PARAMETERS_SCHEMA
from pe_sim.plant_registry import (
    PlantRegistration,
    clear_registrations,
    plant_catalog,
    register_plant,
)
from pe_sim.spec_validation import SPEC_VALIDATION_SCHEMA


@pytest.fixture(autouse=True)
def _isolated_registry():
    """The registry is process-wide, so every test starts and ends empty."""

    clear_registrations()
    yield
    clear_registrations()


def _write_template(tmp_path, **overrides) -> Path:
    """Copy the shipped Buck template and point its output at ``tmp_path``."""

    template = resources.files("pe_sim.templates").joinpath("buck_pi_step.json")
    document = json.loads(template.read_text(encoding="utf-8"))
    document["output"]["directory"] = str(tmp_path / "runs")
    document.update(overrides)
    path = tmp_path / "config.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def _rewrite(config: Path, mutator) -> None:
    """Apply one in-place edit to an already written document."""

    document = json.loads(config.read_text(encoding="utf-8"))
    mutator(document)
    config.write_text(json.dumps(document), encoding="utf-8")


def _factory(config, context):
    """A shared, hashable stand-in builder; equality tests rely on identity."""

    return object()


def _registration(**overrides) -> PlantRegistration:
    values = {
        "plant_id": "schema-demo",
        "backend_id": "schema-demo",
        "factory": _factory,
        "class_name": "SchemaDemoPlant",
    }
    values.update(overrides)
    return PlantRegistration(**values)


# ---------------------------------------------------------------------------
# describe
# ---------------------------------------------------------------------------


def test_describe_prints_the_declared_parameter_schema(capsys):
    assert cli_main(["describe", "ngspice-buck"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["schema"] == PLANT_PARAMETERS_SCHEMA
    assert payload["plant_id"] == "ngspice-buck"
    assert payload["parameter_schema"]["additionalProperties"] is False
    # A document needs both halves: a correct ``plant_config`` next to an
    # invented schedule key still fails, so the vocabulary travels with it.
    assert payload["schedule_keys"] == sorted(payload["schedule_keys"])
    assert "vin_v" in payload["schedule_keys"]


def test_describe_rejects_an_unknown_plant(capsys):
    assert cli_main(["describe", "nope"]) == 2
    payload = json.loads(capsys.readouterr().out)

    assert payload["error_code"] == "unknown_plant"
    # Same envelope shape as a refused ``run``: one parser for one class of
    # error, and ``expected`` carries the list a caller can correct from.
    assert payload["validation"] == SPEC_VALIDATION_SCHEMA
    assert payload["issues"][0]["code"] == "unknown_plant"
    assert payload["issues"][0]["path"] == "/plant_id"
    assert "buck" in payload["issues"][0]["expected"]


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------


def test_validate_accepts_a_shipped_template(tmp_path, capsys):
    config = _write_template(tmp_path)

    assert cli_main(["validate", str(config)]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["schema"] == SPEC_VALIDATION_SCHEMA
    assert payload["ok"] is True


def test_validate_points_at_a_misspelled_parameter(tmp_path, capsys):
    config = _write_template(tmp_path)
    _rewrite(config, lambda document: document.__setitem__("plant_config", {"vin": 12.0}))

    assert cli_main(["validate", str(config)]) == 2
    payload = json.loads(capsys.readouterr().out)

    matching = [issue for issue in payload["issues"] if issue["path"] == "/plant_config/vin"]
    assert len(matching) == 1
    assert matching[0]["suggestion"] == "input_voltage_v"


def test_validate_creates_no_run_directory(tmp_path, capsys):
    config = _write_template(tmp_path)
    _rewrite(config, lambda document: document.__setitem__("plant_config", {"vin": 12.0}))

    assert cli_main(["validate", str(config)]) == 2
    capsys.readouterr()

    assert not (tmp_path / "runs").exists()


def test_validate_checks_an_ngspice_plant_without_an_executable(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("PE_SIM_NGSPICE", raising=False)
    config = _write_template(tmp_path, plant_id="ngspice-buck")
    _rewrite(config, lambda document: document["plant_config"].__setitem__("not_a_key", 1))

    assert cli_main(["validate", str(config)]) == 2
    payload = json.loads(capsys.readouterr().out)

    assert "/plant_config/not_a_key" in {issue["path"] for issue in payload["issues"]}


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------


def test_run_rejection_carries_the_same_structured_issues(tmp_path, capsys):
    config = _write_template(tmp_path, plant_id="ngspice-buck")
    _rewrite(config, lambda document: document["plant_config"].__setitem__("not_a_key", 1))

    assert cli_main(["run", str(config)]) == 2
    payload = json.loads(capsys.readouterr().out)

    assert payload["error_code"] == "invalid_request"
    assert payload["validation"] == SPEC_VALIDATION_SCHEMA
    assert "/plant_config/not_a_key" in {issue["path"] for issue in payload["issues"]}
    assert not (tmp_path / "runs").exists()


# ---------------------------------------------------------------------------
# catalog and registry declarations
# ---------------------------------------------------------------------------


def test_every_builtin_plant_publishes_a_closed_parameter_schema():
    load_builtin_plants()
    catalog = plant_catalog()

    assert list(catalog) == ["schema", "trust_levels", "plants"]
    builtin_ids = {registration.plant_id for registration in builtin_registrations()}
    assert len(builtin_ids) == 12
    entries = {entry["plant_id"]: entry for entry in catalog["plants"]}
    assert set(entries) == builtin_ids
    for plant_id in sorted(builtin_ids):
        schema = entries[plant_id]["parameter_schema"]
        assert schema["type"] == "object", plant_id
        assert schema["additionalProperties"] is False, plant_id
        assert isinstance(schema["properties"], dict), plant_id

    # The published declaration is plain data: the catalog must serialize.
    json.dumps(catalog, sort_keys=True)


def test_registration_refuses_a_schema_that_disagrees_with_config_keys():
    registration = _registration(
        config_keys=frozenset({"a"}),
        parameter_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {"b": {}},
        },
    )

    with pytest.raises(ValueError, match="same keys"):
        register_plant(registration)


def test_parameter_schema_is_excluded_from_equality_and_hashing():
    left = _registration(
        config_keys=frozenset({"a"}),
        parameter_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {"a": {}},
        },
    )
    right = _registration(
        config_keys=frozenset({"a"}),
        parameter_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {"a": {"type": "number"}},
        },
    )

    assert left == right
    assert hash(left) == hash(right)


def test_run_keeps_the_unknown_plant_rejection_code(tmp_path, capsys):
    """A specific, branchable rejection code must survive the new pre-flight.

    Moving the plant lookup into ``spec_validation`` made every structured
    failure arrive as one exception type.  A client already branching on
    ``unknown_plant`` must keep working, so the rejection code is taken from
    the issue that names one - and the issue list still travels alongside it.
    """

    config = _write_template(tmp_path, plant_id="no-such-plant")

    assert cli_main(["run", str(config)]) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "CONFIG_REJECTED"
    assert payload["error_code"] == "unknown_plant"
    assert payload["issues"][0]["code"] == "unknown_plant"
    assert payload["issues"][0]["path"] == "/plant_id"
    assert not (tmp_path / "runs").exists()

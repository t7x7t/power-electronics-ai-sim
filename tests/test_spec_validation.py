"""Structured, agent-consumable validation of an experiment document.

The kernel's historical failure mode was one flat ``ValueError`` string, which
an agent cannot map back to a field.  These tests hold the new contract to the
five checks, the non-blocking exploratory warning, and deterministic ordering.
"""

from __future__ import annotations

import json
from importlib import resources
from pathlib import Path

import pytest

from pe_sim.plant_registry import (
    PlantRegistration,
    clear_registrations,
    register_plant,
)
from pe_sim.spec_validation import (
    CHECKED_STAGES,
    SPEC_ISSUE_CODES,
    SPEC_VALIDATION_SCHEMA,
    SpecIssue,
    SpecValidationError,
    collect_issues,
    validate_document,
    validation_document,
)

pytest.importorskip("jsonschema")


def _template() -> dict:
    text = (
        resources.files("pe_sim.templates")
        .joinpath("buck_pi_step.json")
        .read_text(encoding="utf-8")
    )
    return json.loads(text)


def _document(**overrides) -> dict:
    document = _template()
    document.update(overrides)
    return document


def test_the_shipped_template_is_valid():
    issues = collect_issues(_template())

    assert [issue for issue in issues if issue.severity == "error"] == []

    envelope = validation_document(_template(), issues=issues)
    assert envelope["schema"] == SPEC_VALIDATION_SCHEMA
    assert envelope["plant_id"] == "buck"
    assert envelope["ok"] is True
    assert envelope["error_count"] == 0
    assert envelope["checked"] == list(CHECKED_STAGES)


def test_validate_document_returns_the_envelope_for_a_valid_document():
    envelope = validate_document(_template())

    assert envelope["ok"] is True
    assert envelope["issues"] == []


def test_unknown_parameter_points_at_the_key_and_suggests_the_real_one():
    issues = collect_issues(_document(plant_config={"vin": 12}))

    unknown = [issue for issue in issues if issue.code == "unknown_parameter"]
    assert len(unknown) == 1
    issue = unknown[0]
    assert issue.path == "/plant_config/vin"
    assert issue.suggestion == "input_voltage_v"
    assert "input_voltage_v" in issue.expected
    assert issue.severity == "error"


def test_unknown_plant_is_reported_with_a_suggestion():
    issues = collect_issues(_document(plant_id="buckk"))

    unknown = [issue for issue in issues if issue.code == "unknown_plant"]
    assert len(unknown) == 1
    assert unknown[0].path == "/plant_id"
    assert unknown[0].suggestion == "buck"


def test_unknown_schedule_key_is_reported():
    issues = collect_issues(
        _document(input_schedule=[{"time_s": 0.0, "vinn_v": 12.0}])
    )

    unknown = [issue for issue in issues if issue.code == "unknown_schedule_key"]
    assert len(unknown) == 1
    assert unknown[0].path == "/input_schedule/0/vinn_v"
    assert unknown[0].suggestion == "vin_v"

    # ``jsonschema`` also flags the same field as an unexpected property.  That
    # generic record has no suggestion, so keeping it would make an agent repair
    # one typo twice and hide the record it can act on.
    assert [issue for issue in issues if issue.path == "/input_schedule/0"] == []


def test_out_of_range_parameter_value_is_reported():
    issues = collect_issues(_document(plant_config={"inductance_h": -1.0}))

    invalid = [issue for issue in issues if issue.code == "invalid_parameter_value"]
    assert len(invalid) == 1
    assert invalid[0].path == "/plant_config/inductance_h"
    assert invalid[0].expected == "number > 0"


def test_a_long_unknown_key_gets_no_suggestion():
    """A junk key must not be "repaired" into a real parameter.

    ``not_a_key`` shares as many letters with ``input_voltage_v`` as the
    genuine abbreviation ``vin`` does, so a purely score-based fallback would
    suggest it.  An agent following that suggestion would silently turn a junk
    field into a live parameter - a worse outcome than no suggestion, because
    ``expected`` already lists every legal name.
    """

    issues = collect_issues(_document(plant_config={"not_a_key": 1.0}))

    unknown = [issue for issue in issues if issue.code == "unknown_parameter"]
    assert len(unknown) == 1
    assert unknown[0].suggestion is None
    assert "input_voltage_v" in (unknown[0].expected or "")


def test_an_integer_is_not_a_boolean():
    issues = collect_issues(
        _document(plant_id="whole-run-buck", plant_config={"anti_windup": 1})
    )

    invalid = [issue for issue in issues if issue.code == "invalid_parameter_value"]
    assert len(invalid) == 1
    assert invalid[0].path == "/plant_config/anti_windup"
    assert invalid[0].expected == "boolean"


def _zero_hash_contracts() -> dict:
    zero = "0" * 64
    return {
        "safety": {"id": "exploratory-safety", "hash": zero},
        "qualification": {"id": "exploratory-qualification", "hash": zero},
    }


def test_zero_contract_hashes_are_a_warning_not_an_error():
    document = _document(contracts=_zero_hash_contracts())

    issues = collect_issues(document)
    warnings = [issue for issue in issues if issue.code == "exploratory_contract_default"]
    assert len(warnings) == 1
    assert warnings[0].severity == "warning"
    assert warnings[0].path == "/contracts"
    assert not [issue for issue in issues if issue.severity == "error"]

    envelope = validation_document(document, issues=issues)
    assert envelope["ok"] is True
    assert envelope["error_count"] == 0
    assert envelope["warning_count"] == 1


def test_validate_document_raises_with_structured_issues():
    with pytest.raises(SpecValidationError) as info:
        validate_document(_document(plant_config={"vin": 12}))

    error = info.value
    assert error.issues
    assert error.issues[0].path == "/plant_config/vin"
    assert "/plant_config/vin" in str(error)
    assert "1 spec issue" in str(error)
    assert error.to_dict()["issues"][0]["severity"] == "error"


def test_validate_document_returns_the_envelope_when_only_warnings_remain():
    envelope = validate_document(_document(contracts=_zero_hash_contracts()))

    assert envelope["ok"] is True
    assert envelope["warning_count"] == 1


def test_collection_is_deterministic():
    document = _document(plant_config={"vin": 12, "load": 3})

    assert collect_issues(document) == collect_issues(document)


def test_issues_are_sorted_errors_before_warnings():
    document = _document(
        plant_config={"vin": 12},
        contracts=_zero_hash_contracts(),
    )

    issues = collect_issues(document)
    assert issues[0].severity == "error"
    assert issues[-1].severity == "warning"


def test_schema_violations_are_not_truncated():
    import jsonschema

    document = _template()
    document["timebase"] = {"unit": "ms", "control_period_s": 0.0, "duration_s": -1.0}
    document["input_schedule"] = [
        {"time_s": -1.0, "vin_v": 0.0},
        {"time_s": -1.0, "load": "high"},
    ]
    document["seed"] = -1
    document["output"] = {"directory": "", "retention": "forever"}

    schema = json.loads(
        (
            resources.files("pe_sim.schemas")
            .joinpath("experiment.schema.json")
            .read_text(encoding="utf-8")
        )
    )
    raw = list(jsonschema.Draft202012Validator(schema).iter_errors(document))
    violations = [issue for issue in collect_issues(document) if issue.code == "schema_violation"]

    # The historical loader reported at most five errors; the new collector
    # must return the same set the validator produced, however large.
    assert len(raw) > 5
    assert len(violations) == len(raw)


def test_invalid_document_is_reported_without_raising():
    issues = collect_issues(["not", "an", "object"])  # type: ignore[arg-type]

    assert len(issues) == 1
    assert issues[0].code == "invalid_document"
    assert issues[0].path == ""


def test_issue_vocabulary_and_severity_are_closed():
    with pytest.raises(ValueError):
        SpecIssue(code="not_a_code", path="/x", message="message")
    with pytest.raises(ValueError):
        SpecIssue(code="unknown_plant", path="/plant_id", message="message", severity="fatal")

    assert {
        "invalid_document",
        "schema_violation",
        "unknown_plant",
        "unknown_parameter",
        "invalid_parameter_value",
        "unknown_schedule_key",
        "exploratory_contract_default",
    } <= SPEC_ISSUE_CODES


def test_schema_path_override_is_used(tmp_path: Path):
    schema = json.loads(
        (
            resources.files("pe_sim.schemas")
            .joinpath("experiment.schema.json")
            .read_text(encoding="utf-8")
        )
    )
    # A copy that drops the top-level "required" list: the document below is
    # still a JSON object, so only the overridden schema decides what is legal.
    schema.pop("required", None)
    override = tmp_path / "experiment.schema.json"
    override.write_text(json.dumps(schema), encoding="utf-8")

    issues = collect_issues({"plant_id": "no-such-plant"}, schema_path=override)

    assert not [issue for issue in issues if issue.code == "schema_violation"]
    assert [issue for issue in issues if issue.code == "unknown_plant"]


def _plugin_registration() -> PlantRegistration:
    """A plant that exists only in the registry, as a distribution would ship."""

    return PlantRegistration(
        plant_id="vendor-llc",
        backend_id="vendor-llc",
        factory=lambda config, context: None,
        class_name="VendorLlcPlant",
        config_keys=frozenset({"turns_ratio"}),
        parameter_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {"turns_ratio": {"type": "number", "exclusiveMinimum": 0}},
        },
    )


def test_a_registered_plugin_plant_is_checked_not_reported_unknown():
    """A plant that exists only in the registry must not be called unknown.

    Third-party distributions ship plants through the ``pe_sim.plants`` entry
    point, so the kernel cannot hold their names.  Treating "not in the
    built-in table" as "does not exist" would tell an agent to rename a plant
    that is installed and working - the opposite of the guidance this surface
    exists to give.
    """

    register_plant(_plugin_registration())
    try:
        misspelled = collect_issues(
            _document(plant_id="vendor-llc", plant_config={"turns_rati0": 4.0})
        )
        assert not [issue for issue in misspelled if issue.code == "unknown_plant"]
        unknown = [issue for issue in misspelled if issue.code == "unknown_parameter"]
        assert [issue.path for issue in unknown] == ["/plant_config/turns_rati0"]
        # The suggestion comes from the schema the plugin published, not from a
        # built-in table this checker is not allowed to know about.
        assert unknown[0].suggestion == "turns_ratio"

        out_of_range = collect_issues(
            _document(plant_id="vendor-llc", plant_config={"turns_ratio": -1.0})
        )
        invalid = [
            issue for issue in out_of_range if issue.code == "invalid_parameter_value"
        ]
        assert [issue.path for issue in invalid] == ["/plant_config/turns_ratio"]
    finally:
        clear_registrations()


def test_an_unresolvable_plant_lists_registered_names():
    """A near-miss on a plugin id should point at the installed plant."""

    register_plant(_plugin_registration())
    try:
        issues = collect_issues(_document(plant_id="vendor-llc-typo", plant_config={}))
        unknown = [issue for issue in issues if issue.code == "unknown_plant"]
        assert len(unknown) == 1
        assert "vendor-llc" in (unknown[0].expected or "")
        assert unknown[0].suggestion == "vendor-llc"
    finally:
        clear_registrations()


def test_checked_reports_only_the_stages_that_ran():
    """``ok`` must not look the same when a stage was skipped.

    With an unresolvable plant id there is nothing to look ``plant_config`` up
    against, so reporting that stage as checked would present "not examined"
    as "clean".  The envelope's ``checked`` list is the consumer's only way to
    tell the two apart.
    """

    unresolved = validation_document(
        _document(plant_id="no-such-plant"),
        issues=collect_issues(_document(plant_id="no-such-plant")),
    )
    assert unresolved["checked"] == ["schema", "contracts"]
    assert unresolved["ok"] is False

    resolved = validation_document(_template(), issues=collect_issues(_template()))
    assert resolved["checked"] == list(CHECKED_STAGES)

    # A schedule exists but its vocabulary does not: the stage is skipped, not
    # silently reported as clean.
    with_schedule = _document(
        plant_id="no-such-plant",
        input_schedule=[{"time_s": 0.0, "vref": 5.0}],
    )
    assert "input_schedule" not in validation_document(
        with_schedule, issues=collect_issues(with_schedule)
    )["checked"]

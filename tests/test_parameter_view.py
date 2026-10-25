"""Focused checks for the read-only parameter view of a published package."""

from __future__ import annotations

import json
from pathlib import Path
import shutil

import pytest

from pe_sim.artifacts import artifact_index, manifest_digest, package_digest
from pe_sim.inspection import inspect_run
from pe_sim.parameter_view import (
    BASE_GROUP_ORDER,
    PARAMETER_VIEW_RULE_VERSION,
    PARAMETER_VIEW_SCHEMA,
    ParameterViewError,
    build_parameter_view,
    parameter_rows,
    parameter_set_digest,
)


FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "visualization_service"
    / "buck-contract-fixture"
)

_PLANT_PARAMETERS = {
    "input_voltage_v": 12.0,
    "inductance_h": 1e-4,
    "capacitance_f": 4.7e-4,
    "load_resistance_ohm": 10.0,
    "integration_step_s": 1e-6,
}


def _publish(
    tmp_path,
    *,
    name="buck-parameters",
    plant_parameters=None,
    annotation_unit="V",
    review_status="unreviewed",
    policy_maximum=1.0,
):
    """Publish a patched copy of the checked-in Buck fixture.

    The patch adds the manifest sections a real run records and recomputes the
    derived hashes in dependency order: the artifact index is taken before the
    manifest is rewritten, and the manifest must not index itself.
    """

    run_dir = tmp_path / name
    shutil.copytree(FIXTURE, run_dir)
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    manifest["plant"]["identity"]["parameters"] = dict(
        _PLANT_PARAMETERS if plant_parameters is None else plant_parameters
    )
    manifest["controller"] = {
        "id": "pi",
        "identity": {
            "kind": "pi_controller",
            "module": "pe_sim.picontroller",
            "class": "PIController",
            "parameters": {"kp": 0.2, "ki": 0.0},
        },
        "run_configuration": {"kp": 0.25, "measurement_key": "vout"},
    }
    manifest["timebase"] = {
        "unit": "s",
        "control_period_s": 1e-5,
        "duration_s": 1e-4,
        "sample_offset_s": 0.0,
        "event_tolerance_s": 1e-12,
        "plant_step_s": 1e-6,
    }
    manifest["action_policy"] = {
        "hash": "c" * 64,
        "identity": {
            "module": "pe_sim.safety",
            "class": "BoundedActionPolicy",
            "name": "duty",
            "minimum": 0.0,
            "maximum": policy_maximum,
        },
    }
    manifest["environment"] = {
        "status": "known",
        "backends": [
            {
                "name": "pe_sim.ideal_averaged_python",
                "version": "reference-v1",
                "status": "known",
                "solver_settings": {"integrator": "rk4", "fixed_step": True},
            },
            {
                "name": "aaa_backend",
                "version": "0.1",
                "status": "partial",
                "solver_settings": {"alpha": 1.5},
            },
            {
                "name": "zzz_backend",
                "version": "0.2",
                "status": "unknown",
                "solver_settings": {"beta": 2},
            },
        ],
    }
    manifest["qualification"] = {
        "level": "partial",
        "passed": True,
        "checks": {
            "findings": [
                {
                    "rule_id": "qualification.steady_state",
                    "category": "steady_state_unassessed",
                    "message": "steady state was not assessed",
                },
                {
                    "rule_id": "qualification.duty_varying",
                    "category": "duty_varying",
                    "message": "the duty ratio varies",
                },
                {
                    "rule_id": "qualification.duty_varying_again",
                    "category": "duty_varying",
                    "message": "the duty ratio varies",
                },
            ]
        },
    }
    manifest["applicability"] = {
        "mode": "record",
        "parameter_provenance_status": "declared",
        "parameter_provenance_sha256": "b" * 64,
        "checks": {
            "findings": [
                {
                    "rule_id": "envelope.vin.domain",
                    "category": "envelope",
                    "message": "input voltage is inside the declared envelope",
                    "evidence": {
                        "parameter": "input_voltage_v",
                        "unit": annotation_unit,
                        "source": "assumption",
                        "review_status": review_status,
                    },
                },
                {
                    # A later finding must never overwrite the first record.
                    "rule_id": "envelope.vin.domain.duplicate",
                    "category": "envelope",
                    "message": "duplicate annotation for the same parameter",
                    "evidence": {
                        "parameter": "input_voltage_v",
                        "unit": "kV",
                        "source": "measured",
                        "review_status": "reviewed",
                    },
                },
                {
                    "rule_id": "envelope.load.domain",
                    "category": "envelope",
                    "message": "load resistance is inside the declared envelope",
                    "evidence": {
                        "parameter": "load_resistance_ohm",
                        "unit": "ohm",
                        "source": "datasheet",
                        "review_status": "reviewed",
                    },
                },
                {
                    # Annotating a name that is not a recorded row must not
                    # create a parameter.
                    "rule_id": "envelope.duty.domain",
                    "category": "envelope",
                    "message": "duty is inside the declared envelope",
                    "evidence": {
                        "parameter": "duty",
                        "unit": "1",
                        "source": "policy",
                        "review_status": "reviewed",
                    },
                },
            ]
        },
    }
    manifest["artifacts"] = artifact_index(run_dir)
    manifest["manifest_sha256"] = manifest_digest(manifest)
    manifest["package_sha256"] = package_digest(run_dir, manifest)
    manifest["hashes"] = {
        "manifest_sha256": manifest["manifest_sha256"],
        "package_sha256": manifest["package_sha256"],
    }
    (run_dir / "manifest.json").write_text(
        json.dumps(manifest, sort_keys=True), encoding="utf-8"
    )
    assert inspect_run(run_dir).status == "ok"
    return run_dir


def _group(view, group_id):
    for group in view["parameter_set"]["groups"]:
        if group["id"] == group_id:
            return group
    raise AssertionError(f"group {group_id!r} is missing")


def _rows_by_name(group):
    return {row["name"]: row for row in group["parameters"]}


def test_view_declares_schema_rule_version_and_documented_sections(tmp_path):
    run_dir = _publish(tmp_path)

    view = build_parameter_view(run_dir)

    assert view["schema_version"] == PARAMETER_VIEW_SCHEMA
    assert view["rule_version"] == PARAMETER_VIEW_RULE_VERSION
    assert set(view) == {
        "schema_version",
        "rule_version",
        "run",
        "package",
        "parameter_set",
        "qualification",
        "claim",
        "limitations",
    }
    assert view["limitations"]
    assert all(isinstance(item, str) and item for item in view["limitations"])
    assert set(view["run"]) == {
        "run_id",
        "run_mode",
        "status",
        "evidence_level",
        "qualification_level",
    }
    assert set(view["package"]) == {
        "manifest_sha256",
        "package_sha256",
        "source_commit",
        "working_tree_status",
        "compatibility",
    }
    assert set(view["package"]["compatibility"]) == {
        "status",
        "reader_schema",
        "package_schema",
    }
    assert set(view["parameter_set"]) == {"digest", "groups", "bounds", "counts"}
    for group in view["parameter_set"]["groups"]:
        assert set(group) == {"id", "component", "provenance", "parameters"}
        for row in group["parameters"]:
            assert set(row) == {
                "name",
                "value",
                "unit",
                "source",
                "review_status",
                "provenance_source",
                "range",
            }

    inspection = inspect_run(run_dir)
    assert view["run"] == {
        "run_id": "buck-contract-fixture",
        "run_mode": "exploratory",
        "status": "QUALIFIED",
        "evidence_level": "test_fixture",
        "qualification_level": "partial",
    }
    assert view["package"]["manifest_sha256"] == inspection.summary["hashes"]["manifest_sha256"]
    assert view["package"]["package_sha256"] == inspection.summary["hashes"]["package_sha256"]
    assert view["package"]["source_commit"] == "0" * 40
    assert view["package"]["working_tree_status"] == "not_applicable"
    assert view["package"]["compatibility"] == {
        "status": "current",
        "reader_schema": "0.2",
        "package_schema": "0.2",
    }
    # Deterministic: the same package yields byte-identical JSON.
    assert json.dumps(build_parameter_view(run_dir), sort_keys=True) == json.dumps(
        view, sort_keys=True
    )


def test_group_order_is_base_groups_then_sorted_solver_groups(tmp_path):
    view = build_parameter_view(_publish(tmp_path))

    ids = [group["id"] for group in view["parameter_set"]["groups"]]

    assert ids == [
        *BASE_GROUP_ORDER,
        "solver:aaa_backend",
        "solver:pe_sim.ideal_averaged_python",
        "solver:zzz_backend",
    ]


def test_parameters_are_sorted_by_name_in_every_group(tmp_path):
    view = build_parameter_view(_publish(tmp_path))

    for group in view["parameter_set"]["groups"]:
        names = [row["name"] for row in group["parameters"]]
        assert names == sorted(names), group["id"]


def test_annotated_parameters_carry_annotation_and_provenance(tmp_path):
    view = build_parameter_view(_publish(tmp_path))

    plant = _group(view, "plant")
    assert plant["component"] == {
        "id": "buck",
        "kind": "ideal_averaged_reference",
        "level": None,
        "identity_sha256": None,
    }
    assert plant["provenance"] == {"status": "declared", "sha256": "b" * 64}
    rows = _rows_by_name(plant)
    assert rows["input_voltage_v"] == {
        "name": "input_voltage_v",
        "value": 12.0,
        # The duplicate finding must not overwrite the first record.
        "unit": "V",
        "source": "assumption",
        "review_status": "unreviewed",
        "provenance_source": "applicability_finding",
        "range": None,
    }
    assert rows["load_resistance_ohm"]["unit"] == "ohm"
    assert rows["load_resistance_ohm"]["source"] == "datasheet"
    assert rows["load_resistance_ohm"]["review_status"] == "reviewed"
    assert rows["load_resistance_ohm"]["provenance_source"] == "applicability_finding"
    for name in ("capacitance_f", "inductance_h", "integration_step_s"):
        assert rows[name]["unit"] is None
        assert rows[name]["source"] is None
        assert rows[name]["review_status"] is None
        assert rows[name]["provenance_source"] == "not_recorded"

    controller = _group(view, "controller")
    assert controller["component"] == {
        "id": "pi",
        "kind": "pi_controller",
        "class": "PIController",
    }
    controller_rows = _rows_by_name(controller)
    # The run configuration wins over the identity default on a collision.
    assert controller_rows["kp"]["value"] == 0.25
    assert controller_rows["ki"]["value"] == 0.0
    assert controller_rows["measurement_key"]["value"] == "vout"
    assert all(row["unit"] is None for row in controller["parameters"])


def test_timebase_rows_inherit_the_declared_unit(tmp_path):
    view = build_parameter_view(_publish(tmp_path))

    timebase = _group(view, "timebase")

    assert timebase["component"] is None
    assert timebase["provenance"] is None
    names = [row["name"] for row in timebase["parameters"]]
    assert names == [
        "control_period_s",
        "duration_s",
        "event_tolerance_s",
        "plant_step_s",
        "sample_offset_s",
    ]
    assert "unit" not in names
    assert all(row["unit"] == "s" for row in timebase["parameters"])
    assert all(row["source"] is None for row in timebase["parameters"])
    assert all(
        row["provenance_source"] == "not_recorded"
        for row in timebase["parameters"]
    )


def test_range_is_null_and_bounds_follow_the_action_policy(tmp_path):
    view = build_parameter_view(_publish(tmp_path, policy_maximum=0.95))

    for group in view["parameter_set"]["groups"]:
        for row in group["parameters"]:
            assert row["range"] is None

    assert view["parameter_set"]["bounds"] == [
        {"name": "duty", "minimum": 0.0, "maximum": 0.95, "source": "action_policy"}
    ]
    policy = _group(view, "action_policy")
    assert policy["component"] == {"name": "duty", "module": "pe_sim.safety"}
    assert [row["name"] for row in policy["parameters"]] == ["maximum", "minimum"]
    assert _rows_by_name(policy)["maximum"]["value"] == 0.95


def test_counts_match_the_emitted_rows(tmp_path):
    view = build_parameter_view(_publish(tmp_path))

    groups = view["parameter_set"]["groups"]
    rows = [row for group in groups for row in group["parameters"]]

    assert view["parameter_set"]["counts"] == {
        "groups": len(groups),
        "parameters": len(rows),
        "without_unit": sum(1 for row in rows if row["unit"] is None),
        "without_source": sum(1 for row in rows if row["source"] is None),
    }
    assert view["parameter_set"]["counts"] == {
        "groups": 7,
        "parameters": 19,
        "without_unit": 12,
        "without_source": 17,
    }


def test_qualification_section_reports_recorded_findings(tmp_path):
    view = build_parameter_view(_publish(tmp_path))

    assert view["qualification"] == {
        "level": "partial",
        "finding_count": 3,
        "categories": ["duty_varying", "steady_state_unassessed"],
    }


def test_digest_is_stable_and_matches_parameter_set_digest(tmp_path):
    run_dir = _publish(tmp_path)

    first = build_parameter_view(run_dir)
    second = build_parameter_view(run_dir)

    assert first == second
    digest = first["parameter_set"]["digest"]
    assert isinstance(digest, str) and len(digest) == 64
    assert digest == parameter_set_digest(first)
    assert digest == parameter_set_digest(first["parameter_set"])


def test_digest_is_independent_of_mapping_insertion_order(tmp_path):
    forward = _publish(
        tmp_path, name="forward", plant_parameters=dict(_PLANT_PARAMETERS)
    )
    reversed_parameters = dict(reversed(list(_PLANT_PARAMETERS.items())))
    backward = _publish(
        tmp_path, name="backward", plant_parameters=reversed_parameters
    )

    forward_view = build_parameter_view(forward)
    backward_view = build_parameter_view(backward)

    assert forward_view["parameter_set"]["digest"] == backward_view["parameter_set"]["digest"]
    assert [row["name"] for row in _group(forward_view, "plant")["parameters"]] == sorted(
        _PLANT_PARAMETERS
    )


def test_digest_changes_with_value_unit_or_bound(tmp_path):
    base = build_parameter_view(_publish(tmp_path, name="base"))

    changed_value = dict(_PLANT_PARAMETERS)
    changed_value["input_voltage_v"] = 12.5
    value_view = build_parameter_view(
        _publish(tmp_path, name="value", plant_parameters=changed_value)
    )
    unit_view = build_parameter_view(
        _publish(tmp_path, name="unit", annotation_unit="mV")
    )
    bound_view = build_parameter_view(
        _publish(tmp_path, name="bound", policy_maximum=0.95)
    )

    digests = {
        base["parameter_set"]["digest"],
        value_view["parameter_set"]["digest"],
        unit_view["parameter_set"]["digest"],
        bound_view["parameter_set"]["digest"],
    }
    assert len(digests) == 4


def test_digest_ignores_review_status_only_changes(tmp_path):
    base = build_parameter_view(_publish(tmp_path, name="base"))
    reviewed = build_parameter_view(
        _publish(tmp_path, name="reviewed", review_status="reviewed")
    )

    assert _rows_by_name(_group(reviewed, "plant"))["input_voltage_v"][
        "review_status"
    ] == "reviewed"
    assert (
        reviewed["parameter_set"]["digest"] == base["parameter_set"]["digest"]
    )


def test_claim_is_embedded_verbatim(tmp_path):
    run_dir = _publish(tmp_path)

    view = build_parameter_view(run_dir)

    assert view["claim"] == inspect_run(run_dir).summary["claim"]


def test_unpatched_fixture_yields_only_the_plant_group(tmp_path):
    run_dir = tmp_path / "plain-fixture"
    shutil.copytree(FIXTURE, run_dir)

    view = build_parameter_view(run_dir)

    groups = view["parameter_set"]["groups"]
    assert [group["id"] for group in groups] == ["plant"]
    assert view["parameter_set"]["bounds"] == []
    assert view["parameter_set"]["counts"] == {
        "groups": 1,
        "parameters": 5,
        "without_unit": 5,
        "without_source": 5,
    }
    plant = groups[0]
    assert plant["provenance"] is None
    assert [row["name"] for row in plant["parameters"]] == sorted(
        _PLANT_PARAMETERS
    )
    assert all(row["unit"] is None for row in plant["parameters"])
    assert all(row["range"] is None for row in plant["parameters"])
    assert view["qualification"] == {
        "level": None,
        "finding_count": 0,
        "categories": [],
    }


def test_tampered_package_raises_parameter_view_error(tmp_path):
    run_dir = _publish(tmp_path)
    samples_path = run_dir / "samples.json"
    samples = json.loads(samples_path.read_text(encoding="utf-8"))
    samples[0]["vout"] = 0.125
    samples_path.write_text(json.dumps(samples), encoding="utf-8")

    with pytest.raises(ParameterViewError, match="invalid"):
        build_parameter_view(run_dir)


def test_non_scalar_parameter_value_raises_parameter_view_error(tmp_path):
    parameters = dict(_PLANT_PARAMETERS)
    parameters["input_voltage_v"] = {"nominal": 12.0}
    run_dir = _publish(tmp_path, plant_parameters=parameters)

    with pytest.raises(ParameterViewError, match="input_voltage_v"):
        build_parameter_view(run_dir)


def test_parameter_rows_follow_group_and_name_order(tmp_path):
    view = build_parameter_view(_publish(tmp_path))

    rows = parameter_rows(view)

    assert all(set(row) == {"group", "name", "value", "unit"} for row in rows)
    assert len(rows) == view["parameter_set"]["counts"]["parameters"]
    assert [(row["group"], row["name"]) for row in rows] == [
        (group["id"], row["name"])
        for group in view["parameter_set"]["groups"]
        for row in group["parameters"]
    ]
    assert rows[0] == {
        "group": "plant",
        "name": "capacitance_f",
        "value": 4.7e-4,
        "unit": None,
    }
    assert parameter_rows(view["parameter_set"]) == rows

"""Focused checks for the text-first run review surface."""

from __future__ import annotations

import json
from pathlib import Path
import shutil

import pytest

from pe_sim.artifacts import artifact_index, manifest_digest, package_digest
from pe_sim.inspection import inspect_run
from pe_sim.parameter_view import build_parameter_view
import pe_sim.review as review_module
from pe_sim.review import (
    REVIEW_RULE_VERSION,
    REVIEW_SCHEMA,
    SECTION_ORDER,
    ReviewError,
    build_run_review,
    render_markdown,
    render_text,
)


FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "visualization_service"
    / "buck-contract-fixture"
)

_DEFAULT_QUALIFICATION_FINDINGS = [
    {
        "rule_id": "qualification.steady_state",
        "category": "steady_state",
        "message": "steady state was assessed",
        "passed": True,
        "severity": "info",
    },
    {
        "rule_id": "qualification.duty_varying",
        "category": "duty_varying",
        "message": "the duty ratio varies",
        "passed": True,
        "severity": "info",
    },
]


def _publish(
    tmp_path: Path,
    *,
    name: str = "buck-review",
    qualification_findings: list[dict] | None = None,
) -> Path:
    """Publish a patched copy of the checked-in Buck contract fixture.

    The patch adds the manifest sections a real run records.  Derived hashes
    are recomputed in dependency order: the artifact index is taken first,
    then the manifest digest, then the package digest, then the recorded
    ``hashes`` copy, and only then is ``manifest.json`` rewritten.
    """

    run_dir = tmp_path / name
    shutil.copytree(FIXTURE, run_dir)
    (run_dir / ".run-state.json").write_text(
        json.dumps({"run_id": name, "status": "PUBLISHED"}, sort_keys=True),
        encoding="utf-8",
    )
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    manifest["run_id"] = name
    manifest["status"] = "QUALIFIED"
    manifest["run_mode"] = "exploratory"
    manifest["evidence_level"] = "functional"
    identity = manifest["plant"]["identity"]
    identity["kind"] = "ideal_averaged_reference"
    identity["level"] = "L1"
    manifest["controller"] = {
        "id": "pi",
        "identity": {
            "kind": "controller",
            "module": "pe_sim.reference_controllers",
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
            "maximum": 1.0,
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
            }
        ],
    }
    manifest["qualification"] = {
        "level": "partial",
        "passed": True,
        "checks": {
            "findings": list(
                _DEFAULT_QUALIFICATION_FINDINGS
                if qualification_findings is None
                else qualification_findings
            )
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
                    "passed": True,
                    "severity": "info",
                    "evidence": {
                        "parameter": "input_voltage_v",
                        "unit": "V",
                        "source": "assumption",
                        "review_status": "unreviewed",
                    },
                },
                {
                    "rule_id": "envelope.vin.domain.duplicate",
                    "category": "envelope",
                    "message": "duplicate annotation for the same parameter",
                    "passed": True,
                    "severity": "info",
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
                    "passed": True,
                    "severity": "info",
                    "evidence": {
                        "parameter": "load_resistance_ohm",
                        "unit": "ohm",
                        "source": "assumption",
                        "review_status": "unreviewed",
                    },
                },
                {
                    "rule_id": "thermal.dissipation",
                    "category": "thermal",
                    "message": "loss estimate remains below the advisory limit",
                    "passed": True,
                    "severity": "info",
                },
            ]
        },
    }
    manifest["safety"] = {
        "passed": True,
        "applicability": {
            "checks": {
                "findings": [
                    {
                        "rule_id": "safety.duty.bound",
                        "category": "action_bounds",
                        "message": "the recorded action stays inside the policy bounds",
                        "passed": False,
                        "severity": "warning",
                    }
                ]
            }
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


def _section(review: dict, section_id: str) -> dict:
    for section in review["sections"]:
        if section["id"] == section_id:
            return section
    raise AssertionError(f"section {section_id!r} is missing")


def _child_rows(section: dict, prefix: str) -> list[dict]:
    rows: list[dict] = []
    for child in section["children"]:
        if child["id"].startswith(prefix):
            rows.extend(child["rows"])
    return rows


def test_envelope_declares_schema_versions_and_all_eight_sections(tmp_path):
    run_dir = _publish(tmp_path)

    review = build_run_review(run_dir)

    assert review["schema_version"] == REVIEW_SCHEMA
    assert review["rule_version"] == REVIEW_RULE_VERSION
    assert set(review) == {
        "schema_version",
        "rule_version",
        "run_id",
        "summary",
        "sections",
        "limitations",
    }
    assert set(review["summary"]) == {
        "integrity",
        "claim",
        "qualification",
        "evidence",
    }
    assert [section["id"] for section in review["sections"]] == list(SECTION_ORDER)
    assert all(section["level"] == 1 for section in review["sections"])
    assert all(
        set(section) == {"id", "title", "level", "status", "rows", "notes", "children"}
        for section in review["sections"]
    )
    for section in review["sections"]:
        for child in section["children"]:
            assert child["level"] == 2
            assert set(child) == {
                "id",
                "title",
                "level",
                "status",
                "rows",
                "notes",
                "children",
            }
        for row in section["rows"]:
            assert set(row) == {"label", "value", "status"}
            assert isinstance(row["value"], str)
            assert row["status"] in {None, "ok", "attention", "fail", "not_recorded"}


def test_render_text_is_ascii_and_contains_every_section_title(tmp_path):
    run_dir = _publish(tmp_path)

    review = build_run_review(run_dir)
    text = render_text(review)

    assert text.startswith(f"pe-sim run review: {review['run_id']}")
    for section in review["sections"]:
        assert section["title"] in text
    assert text.isascii()
    assert text.endswith("\n") and not text.endswith("\n\n")


def test_render_markdown_numbers_every_section(tmp_path):
    run_dir = _publish(tmp_path)

    markdown = render_markdown(build_run_review(run_dir))

    for number in range(1, 9):
        assert f"## {number}." in markdown
    assert markdown.isascii()


def test_two_builds_are_identical(tmp_path):
    run_dir = _publish(tmp_path)

    first = build_run_review(run_dir)
    second = build_run_review(run_dir)

    assert first == second
    assert render_text(first) == render_text(second)
    assert render_markdown(first) == render_markdown(second)


def test_parameter_children_follow_the_view_and_missing_units_still_render(tmp_path):
    run_dir = _publish(tmp_path)
    view = build_parameter_view(run_dir)

    section = _section(build_run_review(run_dir), "parameters")

    expected_groups = [group["id"] for group in view["parameter_set"]["groups"]]
    assert [child["title"] for child in section["children"]] == expected_groups
    assert section["rows"][0]["value"] == str(
        view["parameter_set"]["counts"]["parameters"]
    )
    plant = next(child for child in section["children"] if child["title"] == "plant")
    capacitance = next(
        row for row in plant["rows"] if row["label"] == "capacitance_f"
    )
    assert capacitance["value"] == "0.00047"
    assert "  (source:" not in capacitance["value"]
    annotated = next(
        row for row in plant["rows"] if row["label"] == "input_voltage_v"
    )
    assert annotated["value"] == "12.0 V  (source: assumption, review: unreviewed)"


def test_failing_qualification_finding_fails_the_checks_section(tmp_path):
    run_dir = _publish(
        tmp_path,
        name="buck-checks-fail",
        qualification_findings=[
            {
                "rule_id": "qualification.gate",
                "category": "gate",
                "message": "the qualification gate failed",
                "passed": False,
                "severity": "error",
            }
        ],
    )

    section = _section(build_run_review(run_dir), "checks")

    assert section["status"] == "fail"
    rows = _child_rows(section, "qualification:")
    failing = [row for row in rows if row["label"] == "qualification.gate"]
    assert len(failing) == 1
    assert failing[0]["status"] == "fail"


def test_finding_groups_are_truncated_with_a_note(tmp_path):
    run_dir = _publish(tmp_path, name="buck-truncate")

    default_section = _section(build_run_review(run_dir), "checks")
    default_envelope = next(
        child
        for child in default_section["children"]
        if child["id"] == "applicability:envelope"
    )
    assert len(default_envelope["rows"]) == 3
    # The finding message is a group note, not a repeated per-row suffix, and
    # three distinct messages collapse into two notes plus a count.
    assert "more distinct message(s)" in default_envelope["notes"][-1]

    truncated = _section(
        build_run_review(run_dir, max_findings_per_group=2), "checks"
    )
    envelope = next(
        child
        for child in truncated["children"]
        if child["id"] == "applicability:envelope"
    )
    assert len(envelope["rows"]) == 2
    assert "1 more finding(s) not shown" in envelope["notes"]


def test_tampered_package_is_refused(tmp_path):
    run_dir = _publish(tmp_path, name="buck-tampered")
    samples_path = run_dir / "samples.json"
    samples = json.loads(samples_path.read_text(encoding="utf-8"))
    samples[0]["vout"] = 9.9
    samples_path.write_text(json.dumps(samples, sort_keys=True), encoding="utf-8")

    with pytest.raises(ReviewError):
        build_run_review(run_dir)


def test_limitations_are_recorded_once_in_order(tmp_path):
    run_dir = _publish(tmp_path, name="buck-limitations")

    review = build_run_review(run_dir)
    section = _section(review, "limitations")

    values = [row["value"] for row in section["rows"]]
    assert values
    assert len(values) == len(set(values))
    assert review["limitations"]
    assert len(review["limitations"]) == len(set(review["limitations"]))


def test_unknown_artifact_index_shape_is_unavailable_not_an_exception(
    tmp_path, monkeypatch
):
    run_dir = _publish(tmp_path, name="buck-artifact-shape")
    inspection = inspect_run(run_dir)
    view = build_parameter_view(run_dir)
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    manifest["artifacts"] = "not-an-index"
    (run_dir / "manifest.json").write_text(
        json.dumps(manifest, sort_keys=True), encoding="utf-8"
    )

    # The integrity gate cannot accept a malformed artifact index, so this
    # test isolates the section builder from the gate: the verified inspection
    # and parameter view were captured before the manifest copy was tampered.
    monkeypatch.setattr(review_module, "inspect_run", lambda _run_dir: inspection)
    monkeypatch.setattr(
        review_module, "build_parameter_view", lambda _run_dir: view
    )

    section = _section(build_run_review(run_dir), "artifacts")

    assert section["status"] == "unavailable"
    assert any("unreadable shape" in note for note in section["notes"])

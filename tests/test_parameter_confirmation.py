"""Parameter confirmations bind one reviewer's statement to one parameter set."""

from __future__ import annotations

import json
from pathlib import Path
import shutil

import pytest

from pe_sim.artifacts import artifact_index, manifest_digest, package_digest
from pe_sim.parameter_confirmation import (
    CONFIRMATION_RULE_VERSION,
    CONFIRMATION_SCHEMA,
    VERIFICATION_SCHEMA,
    ParameterConfirmationError,
    build_confirmation,
    confirmation_digest,
    render_verification_text,
    verify_confirmation,
    write_confirmation,
)
from pe_sim.parameter_view import (
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

PLANT_PARAMETERS = {
    "input_voltage_v": 12.0,
    "inductance_h": 100e-6,
    "capacitance_f": 470e-6,
    "load_resistance_ohm": 10.0,
    "integration_step_s": 1e-6,
}


@pytest.fixture(autouse=True)
def _local_environment(monkeypatch):
    """A confirmation is refused when CI is set; tests run as a local review."""

    monkeypatch.delenv("CI", raising=False)


def _package(
    root: Path,
    run_id: str = "buck-confirmation",
    *,
    parameters: dict | None = None,
    policy_maximum: float = 1.0,
) -> Path:
    """Copy the checked-in contract fixture and make it a complete package."""

    run_dir = root / run_id
    shutil.copytree(FIXTURE, run_dir)
    (run_dir / ".run-state.json").write_text(
        json.dumps({"run_id": run_id, "status": "PUBLISHED"}, sort_keys=True),
        encoding="utf-8",
    )
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    manifest["run_id"] = run_id
    manifest["status"] = "QUALIFIED"
    manifest["run_mode"] = "exploratory"
    manifest["evidence_level"] = "test_fixture"
    identity = manifest["plant"]["identity"]
    identity["kind"] = "ideal_averaged_reference"
    identity["level"] = "L1"
    identity["parameters"] = dict(
        PLANT_PARAMETERS if parameters is None else parameters
    )
    manifest["controller"] = {
        "id": "pi",
        "hash": "0" * 64,
        "identity": {
            "kind": "controller",
            "module": "pe_sim.reference_controllers",
            "class": "PIController",
            "parameters": {"kp": 4.0, "ki": 2000.0},
        },
        "run_configuration": {"kp": 4.0, "ki": 2000.0},
    }
    manifest["timebase"] = {
        "unit": "s",
        "control_period_s": 1e-4,
        "duration_s": 1e-2,
        "sample_offset_s": 0.0,
        "event_tolerance_s": 1e-12,
        "plant_step_s": 1e-5,
    }
    manifest["action_policy"] = {
        "hash": "1" * 64,
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
                "solver_settings": {"integrator": "rk4", "step_s": 1e-6},
            }
        ],
    }
    manifest["applicability"] = {
        "status": "limited",
        "parameter_provenance_status": "declared",
        "parameter_provenance_sha256": "2" * 64,
        "checks": {
            "findings": [
                {
                    "rule_id": "envelope.vin.domain",
                    "category": "duty_varying",
                    "message": "the input voltage is inside the declared range",
                    "severity": "info",
                    "passed": True,
                    "evidence": {
                        "parameter": "input_voltage_v",
                        "unit": "V",
                        "source": "assumption",
                        "review_status": "unreviewed",
                    },
                },
                {
                    "rule_id": "envelope.load.domain",
                    "category": "steady_state_unassessed",
                    "message": "the load is inside the declared range",
                    "severity": "info",
                    "passed": True,
                    "evidence": {
                        "parameter": "load_resistance_ohm",
                        "unit": "ohm",
                        "source": "assumption",
                        "review_status": "unreviewed",
                    },
                },
            ]
        },
    }
    _write_manifest(run_dir, manifest)
    return run_dir


def _write_manifest(run_dir: Path, manifest: dict) -> None:
    """Recompute the derived hashes in the documented order, then write."""

    manifest["artifacts"] = artifact_index(run_dir)
    manifest["manifest_sha256"] = manifest_digest(manifest)
    manifest["package_sha256"] = package_digest(run_dir, manifest)
    manifest["hashes"] = {
        "manifest_sha256": manifest["manifest_sha256"],
        "package_sha256": manifest["package_sha256"],
    }
    (run_dir / "manifest.json").write_bytes(
        (json.dumps(manifest, ensure_ascii=False, sort_keys=True) + "\n").encode(
            "utf-8"
        )
    )


def _recording_from(confirmation: dict, source_run_dir: Path) -> dict:
    """Return a digest-intact confirmation that records another package's set.

    ``build_parameter_view`` refuses a package whose manifest was edited
    without re-deriving its recorded hashes, so a ``stale`` fixture cannot
    tamper with the package.  It instead changes what the confirmation
    records: the package identity the reviewer named stays the same, while the
    recorded rows, digest, and bounds describe a different parameter set.
    That is the real-world case the verdict protects: the confirmed numbers no
    longer match what the same run records.
    """

    source_view = build_parameter_view(source_run_dir)
    updated = json.loads(json.dumps(confirmation))
    updated["parameter_set"]["rows"] = parameter_rows(source_view)
    updated["parameter_set"]["digest"] = source_view["parameter_set"]["digest"]
    updated["parameter_set"]["bounds"] = json.loads(
        json.dumps(source_view["parameter_set"]["bounds"])
    )
    updated["confirmation_digest"] = confirmation_digest(updated)
    return updated


def _stale_confirmation(
    tmp_path: Path,
    *,
    current_parameters: dict,
    recorded_parameters: dict,
    current_maximum: float = 1.0,
    recorded_maximum: float = 1.0,
) -> tuple[Path, dict]:
    """Build one current package and a confirmation that records another set."""

    current = _package(
        tmp_path / "current",
        parameters=current_parameters,
        policy_maximum=current_maximum,
    )
    recorded = _package(
        tmp_path / "recorded",
        parameters=recorded_parameters,
        policy_maximum=recorded_maximum,
    )
    return current, _recording_from(_confirmation(current), recorded)


def _confirmation(run_dir: Path, **overrides) -> dict:
    options = {
        "reviewer": "Ada Lovelace",
        "statement": "I reviewed every recorded buck parameter.",
        "confirmed_at_utc": "2026-10-25T00:00:00Z",
    }
    options.update(overrides)
    return build_confirmation(run_dir, **options)


def _package_bytes(run_dir: Path) -> dict[str, bytes]:
    return {
        path.name: path.read_bytes() for path in run_dir.iterdir() if path.is_file()
    }


def test_build_confirmation_records_the_view(tmp_path):
    run_dir = _package(tmp_path)

    confirmation = _confirmation(run_dir)

    assert set(confirmation) == {
        "schema_version",
        "rule_version",
        "run",
        "parameter_set",
        "review",
        "claim_conclusion",
        "limitations",
        "confirmation_digest",
    }
    assert confirmation["schema_version"] == CONFIRMATION_SCHEMA
    assert confirmation["rule_version"] == CONFIRMATION_RULE_VERSION
    assert set(confirmation["run"]) == {
        "run_id",
        "manifest_sha256",
        "package_sha256",
    }
    assert set(confirmation["parameter_set"]) == {"digest", "rows", "bounds"}
    assert set(confirmation["review"]) == {
        "reviewer",
        "confirmed_at_utc",
        "statement",
        "scope",
        "acknowledged_limitations",
    }
    assert confirmation["review"]["reviewer"] == "Ada Lovelace"
    assert confirmation["review"]["confirmed_at_utc"] == "2026-10-25T00:00:00Z"

    view = build_parameter_view(run_dir)
    group_ids = [group["id"] for group in view["parameter_set"]["groups"]]
    assert confirmation["review"]["scope"] == group_ids
    assert "plant" in confirmation["review"]["scope"]
    assert confirmation["parameter_set"]["digest"] == parameter_set_digest(view)
    assert confirmation["parameter_set"]["bounds"] == (
        view["parameter_set"]["bounds"]
    )
    assert confirmation["claim_conclusion"] == view["claim"]["conclusion"]
    assert confirmation["review"]["acknowledged_limitations"] == list(
        view["limitations"]
    )
    assert confirmation["run"]["run_id"] == view["run"]["run_id"]
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert confirmation["run"]["manifest_sha256"] == manifest["manifest_sha256"]
    assert confirmation["run"]["package_sha256"] == manifest["package_sha256"]

    assert confirmation["confirmation_digest"] == confirmation_digest(confirmation)
    body_without_digest = {
        key: value
        for key, value in confirmation.items()
        if key != "confirmation_digest"
    }
    assert confirmation_digest(body_without_digest) == confirmation["confirmation_digest"]
    assert any("not an approval" in item for item in confirmation["limitations"])
    assert any("signed" in item for item in confirmation["limitations"])


def test_build_confirmation_reports_every_parameter_row(tmp_path):
    run_dir = _package(tmp_path)
    view = build_parameter_view(run_dir)

    confirmation = _confirmation(run_dir)

    expected = {
        (group["id"], row["name"]): row["value"]
        for group in view["parameter_set"]["groups"]
        for row in group["parameters"]
    }
    recorded = {
        (row["group"], row["name"]): row["value"]
        for row in confirmation["parameter_set"]["rows"]
    }
    assert recorded == expected
    assert ("plant", "input_voltage_v") in recorded


def test_build_confirmation_refuses_automation_and_thin_reviews(tmp_path, monkeypatch):
    run_dir = _package(tmp_path)

    monkeypatch.setenv("CI", "true")
    with pytest.raises(ParameterConfirmationError, match="human local action"):
        _confirmation(run_dir)
    monkeypatch.setenv("CI", "False")
    assert _confirmation(run_dir)["schema_version"] == CONFIRMATION_SCHEMA
    monkeypatch.setenv("CI", "1")
    with pytest.raises(ParameterConfirmationError, match="human local action"):
        _confirmation(run_dir)
    monkeypatch.setenv("CI", "0")
    assert _confirmation(run_dir)["review"]["reviewer"] == "Ada Lovelace"
    monkeypatch.delenv("CI")

    with pytest.raises(ParameterConfirmationError, match="reviewer"):
        _confirmation(run_dir, reviewer="   ")
    with pytest.raises(ParameterConfirmationError, match="statement"):
        _confirmation(run_dir, statement="too short")
    with pytest.raises(ParameterConfirmationError, match="acknowledged_limitations"):
        _confirmation(run_dir, acknowledged_limitations="a single string")
    with pytest.raises(ParameterConfirmationError, match="acknowledged_limitations"):
        _confirmation(run_dir, acknowledged_limitations=[""])
    with pytest.raises(ParameterConfirmationError, match="acknowledged_limitations"):
        _confirmation(run_dir, acknowledged_limitations=["ok", 3])


def test_build_confirmation_validates_scope(tmp_path):
    run_dir = _package(tmp_path)

    with pytest.raises(ParameterConfirmationError, match="not a parameter group"):
        _confirmation(run_dir, scope=["plant", "telemetry"])
    with pytest.raises(ParameterConfirmationError, match="plant"):
        _confirmation(run_dir, scope=["controller"])

    confirmation = _confirmation(run_dir, scope=["plant"])
    assert confirmation["review"]["scope"] == ["plant"]

    result = verify_confirmation(run_dir, confirmation)
    assert result["status"] == "invalid"
    scope_check = next(
        check for check in result["checks"] if check["id"] == "scope-coverage"
    )
    assert scope_check["status"] == "fail"


def test_timestamps_are_normalised_to_utc(tmp_path):
    run_dir = _package(tmp_path)

    offset = _confirmation(run_dir, confirmed_at_utc="2026-10-25T08:00:00+08:00")
    assert offset["review"]["confirmed_at_utc"] == "2026-10-25T00:00:00Z"

    defaulted = _confirmation(run_dir, confirmed_at_utc=None)
    assert defaulted["review"]["confirmed_at_utc"].endswith("Z")

    with pytest.raises(ParameterConfirmationError, match="ISO 8601"):
        _confirmation(run_dir, confirmed_at_utc="yesterday")
    with pytest.raises(ParameterConfirmationError, match="ISO 8601"):
        _confirmation(run_dir, confirmed_at_utc="")


def test_write_confirmation_refuses_paths_inside_the_package(tmp_path):
    run_dir = _package(tmp_path)
    confirmation = _confirmation(run_dir)

    with pytest.raises(ParameterConfirmationError) as refusal:
        write_confirmation(
            run_dir / "nested" / "confirmation.json",
            confirmation,
            run_dir=run_dir,
        )
    message = str(refusal.value)
    assert "publication is immutable" in message
    assert str(run_dir.resolve()) in message
    assert not (run_dir / "nested").exists()

    target = tmp_path / "records" / "confirmation.json"
    written = write_confirmation(target, confirmation, run_dir=run_dir)
    assert written == target
    raw = target.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")
    text = raw.decode("utf-8")
    assert text.endswith("\n")
    assert text == json.dumps(
        confirmation, ensure_ascii=False, sort_keys=True, indent=2
    ) + "\n"
    assert json.loads(text) == confirmation


def test_untouched_package_is_current(tmp_path):
    run_dir = _package(tmp_path)
    confirmation = _confirmation(run_dir)
    before = _package_bytes(run_dir)

    result = verify_confirmation(
        run_dir, confirmation, confirmation_path=tmp_path / "confirmation.json"
    )

    assert result["schema_version"] == VERIFICATION_SCHEMA
    assert result["status"] == "current"
    assert result["run_id"] == "buck-confirmation"
    assert result["confirmation_path"] == str(tmp_path / "confirmation.json")
    assert [check["id"] for check in result["checks"]] == [
        "schema",
        "confirmation-digest",
        "run-identity",
        "package-identity",
        "parameter-set-digest",
        "scope-coverage",
        "claim-recorded",
    ]
    assert [check["status"] for check in result["checks"]] == ["pass"] * 7
    assert result["differences"] == []
    assert result["parameter_set_digest"]["confirmed"] == (
        result["parameter_set_digest"]["current"]
    )
    assert result["limitations"]
    assert _package_bytes(run_dir) == before


def test_changed_parameter_is_stale_with_the_exact_difference(tmp_path):
    run_dir, confirmation = _stale_confirmation(
        tmp_path,
        current_parameters={**PLANT_PARAMETERS, "input_voltage_v": 24.0},
        recorded_parameters=dict(PLANT_PARAMETERS),
    )

    result = verify_confirmation(run_dir, confirmation)

    assert result["status"] == "stale"
    assert result["differences"] == [
        {
            "group": "plant",
            "name": "input_voltage_v",
            "confirmed": 12.0,
            "current": 24.0,
        }
    ]
    assert result["parameter_set_digest"]["confirmed"] != (
        result["parameter_set_digest"]["current"]
    )
    statuses = {check["id"]: check["status"] for check in result["checks"]}
    assert statuses["run-identity"] == "pass"
    assert statuses["parameter-set-digest"] == "fail"
    assert statuses["confirmation-digest"] == "pass"


def test_added_and_removed_parameters_are_reported(tmp_path):
    current_parameters = {
        name: value
        for name, value in PLANT_PARAMETERS.items()
        if name != "capacitance_f"
    }
    current_parameters["switching_frequency_hz"] = 100000.0
    run_dir, confirmation = _stale_confirmation(
        tmp_path,
        current_parameters=current_parameters,
        recorded_parameters=dict(PLANT_PARAMETERS),
    )

    result = verify_confirmation(run_dir, confirmation)

    assert result["status"] == "stale"
    assert result["differences"] == [
        {
            "group": "plant",
            "name": "capacitance_f",
            "confirmed": 470e-6,
            "current": None,
        },
        {
            "group": "plant",
            "name": "switching_frequency_hz",
            "confirmed": None,
            "current": 100000.0,
        },
    ]


def test_different_package_is_a_mismatched_run(tmp_path):
    package_a = _package(tmp_path / "a", run_id="run-a")
    package_b = _package(tmp_path / "b", run_id="run-b")
    confirmation = _confirmation(package_a)

    result = verify_confirmation(package_b, confirmation)

    assert result["status"] == "mismatched_run"
    assert result["run_id"] == "run-b"
    assert result["parameter_set_digest"]["confirmed"] == (
        result["parameter_set_digest"]["current"]
    )
    assert result["differences"] == []
    statuses = {check["id"]: check["status"] for check in result["checks"]}
    assert statuses["run-identity"] == "fail"
    assert statuses["parameter-set-digest"] == "pass"


def test_edited_digest_and_wrong_schema_are_invalid(tmp_path):
    run_dir = _package(tmp_path)
    confirmation = _confirmation(run_dir)

    edited_digest = json.loads(json.dumps(confirmation))
    edited_digest["confirmation_digest"] = "0" * 64
    result = verify_confirmation(run_dir, edited_digest)
    assert result["status"] == "invalid"
    statuses = {check["id"]: check["status"] for check in result["checks"]}
    assert statuses["confirmation-digest"] == "fail"

    wrong_schema = json.loads(json.dumps(confirmation))
    wrong_schema["schema_version"] = "pe-sim.parameter-confirmation.v0"
    result = verify_confirmation(run_dir, wrong_schema)
    assert result["status"] == "invalid"
    statuses = {check["id"]: check["status"] for check in result["checks"]}
    assert statuses["schema"] == "fail"

    missing_claim = json.loads(json.dumps(confirmation))
    missing_claim["claim_conclusion"] = ""
    missing_claim["confirmation_digest"] = confirmation_digest(missing_claim)
    result = verify_confirmation(run_dir, missing_claim)
    assert result["status"] == "invalid"
    statuses = {check["id"]: check["status"] for check in result["checks"]}
    assert statuses["claim-recorded"] == "fail"
    assert statuses["confirmation-digest"] == "pass"


def test_missing_run_directory_is_unknown(tmp_path):
    run_dir = _package(tmp_path)
    confirmation = _confirmation(run_dir)

    result = verify_confirmation(tmp_path / "does-not-exist", confirmation)

    assert result["status"] == "unknown"
    assert [check["id"] for check in result["checks"]] == ["package"]
    assert result["checks"][0]["status"] == "fail"
    assert "could not be inspected" in result["checks"][0]["detail"]
    assert result["differences"] == []
    assert result["parameter_set_digest"] == {
        "confirmed": confirmation["parameter_set"]["digest"],
        "current": None,
    }
    assert result["run_id"] == "buck-confirmation"
    assert render_verification_text(result).splitlines()[0].endswith("unknown")


def test_malformed_confirmation_argument_is_rejected(tmp_path):
    run_dir = _package(tmp_path)

    with pytest.raises(ParameterConfirmationError, match="JSON object"):
        verify_confirmation(run_dir, ["not", "a", "mapping"])


def test_render_verification_text_names_status_and_differences(tmp_path):
    run_dir = _package(tmp_path)
    confirmation = _confirmation(run_dir)

    current_text = render_verification_text(verify_confirmation(run_dir, confirmation))
    assert "parameter confirmation: current" in current_text
    assert "buck-confirmation" in current_text
    assert confirmation["parameter_set"]["digest"] in current_text
    assert "differences: none" in current_text

    changed, changed_confirmation = _stale_confirmation(
        tmp_path / "stale",
        current_parameters={**PLANT_PARAMETERS, "inductance_h": 220e-6},
        recorded_parameters=dict(PLANT_PARAMETERS),
    )
    stale = verify_confirmation(changed, changed_confirmation)
    stale_text = render_verification_text(stale)
    assert "parameter confirmation: stale" in stale_text
    assert "inductance_h" in stale_text
    assert "plant" in stale_text
    assert "\x1b[" not in stale_text


def test_bound_change_is_reported_as_a_bounds_difference(tmp_path):
    run_dir, confirmation = _stale_confirmation(
        tmp_path,
        current_parameters=dict(PLANT_PARAMETERS),
        recorded_parameters=dict(PLANT_PARAMETERS),
        current_maximum=0.9,
        recorded_maximum=1.0,
    )
    confirmed_bounds = confirmation["parameter_set"]["bounds"]
    assert len(confirmed_bounds) == 1
    bound_name = confirmed_bounds[0]["name"]

    result = verify_confirmation(run_dir, confirmation)

    assert result["status"] == "stale"
    assert result["differences"] == [
        {
            "group": "action_policy",
            "name": "maximum",
            "confirmed": 1.0,
            "current": 0.9,
        },
        {
            "group": "bounds",
            "name": bound_name,
            "confirmed": {"minimum": 0.0, "maximum": 1.0},
            "current": {"minimum": 0.0, "maximum": 0.9},
        },
    ]
    assert any(item["group"] == "bounds" for item in result["differences"])
    assert bound_name in render_verification_text(result)

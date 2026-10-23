"""Read-only credibility factors and precedence."""

from __future__ import annotations

import hashlib
import json

from pe_sim.credibility import CredibilityPolicy, assess_credibility, assess_manifest
from pe_sim.inspection import inspect_run

from test_workbench_export import _published_buck_run


def _credible_manifest(**overrides):
    manifest = {
        "schema_version": "0.1",
        "run_id": "credible-run",
        "experiment_id": "credible-experiment",
        "status": "QUALIFIED",
        "run_mode": "formal_comparison",
        "working_tree_status": "clean",
        "source_commit": "a" * 40,
        "provenance": {"status": "known"},
        "environment": {
            "status": "known",
            "backends": [
                {"status": "known", "name": "ngspice", "version": "41", "solver_settings": {"method": "gear"}}
            ],
        },
        "plant": {
            "id": "ngspice-buck",
            "hash": "b" * 64,
            "identity": {
                "sha256": "c" * 64,
                "kind": "ngspice_switching_reference",
                "parameters": {"input_voltage_v": 12.0},
                "implementation": {
                    "sha256": "d" * 64,
                    "review": {"status": "reviewed"},
                },
            },
            "evidence": {"numerical_health": {"severity": "clean"}},
        },
        "qualification": {"passed": True, "level": "full", "checks": {"findings": []}},
        "applicability": {
            "status": "within",
            "claim": {"status": "declared", "claim_level": "L2-switching-reference"},
            "checks": {"findings": []},
        },
        "safety": {"passed": True},
        "evidence_level": "comparable",
        "manifest_sha256": "e" * 64,
        "package_sha256": "f" * 64,
    }
    manifest.update(overrides)
    return manifest


def _factor(assessment, factor_id):
    return next(item for item in assessment.factors if item.factor_id == factor_id)


def test_clean_formal_reviewed_run_is_credible():
    assessment = assess_manifest(
        _credible_manifest(),
        current_review={"status": "reviewed"},
    )

    assert assessment.status == "credible"
    assert assessment.blocking == ()
    assert all(item.status == "pass" for item in assessment.factors)


def test_dirty_exploratory_run_is_limited():
    assessment = assess_manifest(
        _credible_manifest(
            run_mode="exploratory",
            working_tree_status="dirty",
            provenance={"status": "exploratory"},
            evidence_level="functional",
        ),
        current_review={"status": "reviewed"},
    )

    assert assessment.status == "limited"
    assert _factor(assessment, "source_state").status == "limited"
    assert _factor(assessment, "run_mode").status == "limited"


def test_failed_run_is_not_credible():
    assessment = assess_manifest(
        _credible_manifest(status="RUN_FAILED", safety={"passed": False}),
        current_review={"status": "reviewed"},
    )

    assert assessment.status == "not_credible"
    assert "safety" in assessment.blocking


def test_warning_only_qualification_is_limited():
    assessment = assess_manifest(
        _credible_manifest(qualification={"passed": True, "level": "limited", "checks": {"findings": []}}),
        current_review={"status": "reviewed"},
    )

    assert assessment.status == "limited"
    assert _factor(assessment, "qualification").status == "limited"


def test_not_credible_takes_precedence_over_limited():
    assessment = assess_manifest(
        _credible_manifest(
            run_mode="exploratory",
            safety={"passed": False},
        ),
        current_review={"status": "reviewed"},
    )

    assert assessment.status == "not_credible"


def test_unreviewed_implementation_is_limited_by_default():
    manifest = _credible_manifest()
    manifest["plant"]["identity"]["implementation"]["review"] = {"status": "unreviewed"}
    assessment = assess_manifest(manifest)

    assert assessment.status == "limited"
    assert _factor(assessment, "implementation_review").status == "limited"


def test_require_declared_envelope_policy_downgrades_an_absent_envelope():
    manifest = _credible_manifest()
    manifest.pop("applicability")
    relaxed = assess_manifest(manifest, current_review={"status": "reviewed"})
    strict = assess_manifest(
        manifest,
        current_review={"status": "reviewed"},
        policy=CredibilityPolicy(require_declared_envelope=True),
    )

    assert _factor(relaxed, "applicability").status in {"pass", "unknown"}
    assert _factor(strict, "applicability").status == "limited"


def test_authenticity_is_optional_and_l1_anchored_is_credible():
    default = assess_manifest(_credible_manifest(), current_review={"status": "reviewed"})
    assert "authenticity" not in {item.factor_id for item in default.factors}

    anchored = assess_manifest(
        _credible_manifest(),
        current_review={"status": "reviewed"},
        authenticity={
            "status": "ok",
            "trust_level": "L1_anchored",
            "signature_status": "not_signed",
            "anchor": {"status": "committed"},
        },
        policy=CredibilityPolicy(include_authenticity=True),
    )
    assert anchored.status == "credible"
    assert _factor(anchored, "authenticity").status == "pass"


def test_pending_authenticity_is_limited():
    assessment = assess_manifest(
        _credible_manifest(),
        current_review={"status": "reviewed"},
        authenticity={
            "status": "ok",
            "trust_level": "L1_pending",
            "signature_status": "not_signed",
            "anchor": {"status": "uncommitted"},
        },
        policy=CredibilityPolicy(include_authenticity=True),
    )

    assert assessment.status == "limited"
    assert _factor(assessment, "authenticity").status == "limited"


def test_tampered_package_is_invalid_and_not_credible(tmp_path):
    run_dir, _, _ = _published_buck_run(tmp_path)
    samples_path = run_dir / "samples.json"
    samples = json.loads(samples_path.read_text(encoding="utf-8"))
    samples[0]["vout"] = 99.0
    samples_path.write_text(json.dumps(samples), encoding="utf-8")

    result = inspect_run(run_dir)

    assert result.status == "invalid"
    assert result.summary["credibility"]["status"] == "not_credible"
    assert "integrity" in result.summary["credibility"]["blocking"]


def test_credibility_assessment_is_read_only(tmp_path):
    run_dir, _, _ = _published_buck_run(tmp_path)
    before = {
        path.relative_to(run_dir).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in run_dir.rglob("*")
        if path.is_file()
    }

    assess_credibility(run_dir)

    after = {
        path.relative_to(run_dir).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in run_dir.rglob("*")
        if path.is_file()
    }
    assert after == before


def test_old_package_without_level_derives_a_level():
    manifest = _credible_manifest()
    manifest["qualification"] = {
        "passed": True,
        "checks": {
            "findings": [
                {"severity": "warning", "category": "legacy_warning", "passed": True}
            ]
        },
    }
    assessment = assess_manifest(manifest, current_review={"status": "reviewed"})

    assert _factor(assessment, "qualification").status == "limited"

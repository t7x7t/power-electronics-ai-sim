"""One token, one meaning: the evidence and qualification vocabularies.

Before schema 0.2 the project had two ladders that shared three words:

* evidence:       ``diagnostic_only`` / ``functional`` / ``limited`` / ``comparable``
* qualification: ``full`` / ``limited`` / ``failed``

``limited`` meant both "the claim is bounded but usable" and "some checks could
not be evaluated".  This test file holds the separation in place:

1. the two ladders are disjoint;
2. the evidence ladder has exactly one definition, imported everywhere;
3. Stage 12's old ``mechanism`` level is a scope marker, never a level;
4. readers map the legacy qualification words, writers never emit them.
"""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

from pe_sim import evidence as stage12
from pe_sim.checks import (
    CHECK_LEVELS,
    LEGACY_CHECK_LEVELS,
    CheckFinding,
    CheckReport,
    normalize_check_level,
)
from pe_sim.qualification import LEVELS, QualificationOutcome, qualification_document
from pe_sim.time_model import RUNTIME_EVIDENCE_LEVELS

_REPOSITORY = Path(__file__).resolve().parents[1]
_OLDER_PACKAGE = _REPOSITORY / "tests" / "fixtures" / "older_packages" / "v0.5.9-buck"


@pytest.fixture(scope="module")
def stage10_summary():
    """A real Stage 10 summary built from the checked-in published package."""

    from pe_sim.postprocess import summarize_run

    return summarize_run(_OLDER_PACKAGE)


def test_the_two_ladders_are_disjoint():
    assert set(RUNTIME_EVIDENCE_LEVELS).isdisjoint(set(CHECK_LEVELS))
    assert RUNTIME_EVIDENCE_LEVELS == (
        "diagnostic_only",
        "functional",
        "limited",
        "comparable",
    )
    assert CHECK_LEVELS == ("assessed", "partial", "failed")
    assert LEVELS == CHECK_LEVELS


def test_the_evidence_ladder_has_one_definition():
    assert stage12._LEVELS == frozenset(RUNTIME_EVIDENCE_LEVELS)


def test_stage12_reports_mechanism_as_a_scope_not_a_level(stage10_summary):
    scoped = stage12.classify_summary(stage10_summary, requested_level="mechanism")

    assert scoped["evidence_level"] == "functional"
    assert scoped["scope"] == "mechanism"
    assert scoped["evidence_level"] in RUNTIME_EVIDENCE_LEVELS
    assert scoped["schema"] == "stage12-evidence-classification-v2"


def test_stage12_rejects_a_level_it_cannot_justify(stage10_summary):
    with pytest.raises(stage12.EvidenceClassificationError):
        stage12.classify_summary(stage10_summary, requested_level="limited")


def test_the_stage12_schema_accepts_both_scopes(stage10_summary):
    schema = json.loads(
        (_REPOSITORY / "schemas" / "stage12-evidence-classification.schema.json").read_text(
            encoding="utf-8"
        )
    )
    validator = jsonschema.Draft202012Validator(schema)

    validator.validate(stage12.classify_summary(stage10_summary, requested_level="mechanism"))
    validator.validate(stage12.classify_stage10(stage10_summary))


@pytest.mark.parametrize(
    ("legacy", "current"),
    [("full", "assessed"), ("limited", "partial"), ("failed", "failed")],
)
def test_legacy_qualification_levels_are_mapped(legacy, current):
    assert normalize_check_level(legacy) == current
    assert LEGACY_CHECK_LEVELS.get(legacy, legacy) == current


@pytest.mark.parametrize("value", ["", "FULL", "ok", None, 3])
def test_unknown_qualification_levels_are_rejected(value):
    with pytest.raises(ValueError):
        normalize_check_level(value)


def test_a_legacy_outcome_is_accepted_and_normalized():
    outcome = QualificationOutcome(level="limited", passed=True)

    assert outcome.level == "partial"
    assert outcome.to_document()["schema_version"] == "qualification-v3"


def test_a_warning_only_report_is_partial():
    report = CheckReport(
        True,
        (
            CheckFinding(
                "vocabulary.warning",
                "warning_only",
                "warning only",
                severity="warning",
                passed=True,
            ),
        ),
    )

    assert report.effective_level == "partial"
    assert report.to_dict()["level"] == "partial"


def test_qualification_document_translates_an_explicit_legacy_level():
    document = qualification_document(CheckReport.ok(), level="limited")

    assert document["level"] == "partial"
    assert document["passed"] is True

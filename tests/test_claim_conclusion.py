"""The single read-only claim conclusion derived from recorded evidence."""

from __future__ import annotations

import itertools
import json
from pathlib import Path

import pytest

import pe_sim
from pe_sim.claim import (
    CLAIM_CONCLUSIONS,
    CLAIM_RULE_VERSION,
    CLAIM_SCHEMA,
    ClaimConclusion,
    derive_claim,
)
from pe_sim.inspection import inspect_run

from test_workbench_export import _published_buck_run


FIXTURES = Path(__file__).with_name("fixtures")
OLD_PACKAGE = FIXTURES / "older_packages" / "v0.5.9-buck"


def _derive(**overrides):
    values = {
        "integrity": "ok",
        "run_status": "RUN_OK",
        "evidence_level": "comparable",
        "qualification_level": "assessed",
        "credibility": "credible",
        "compatibility": "current",
    }
    values.update(overrides)
    return derive_claim(**values)


def test_vocabulary_and_schema_tag_are_closed():
    assert CLAIM_SCHEMA == "pe-sim.claim.v1"
    assert CLAIM_RULE_VERSION == "claim-v1"
    assert CLAIM_CONCLUSIONS == (
        "no_claim",
        "exploratory",
        "bounded",
        "comparison_eligible",
    )


def test_all_inputs_strongest_reach_comparison_eligibility():
    conclusion = _derive()

    assert conclusion.conclusion == "comparison_eligible"
    assert conclusion.narrowed_by == ()
    assert conclusion.may_claim
    document = conclusion.to_dict()
    assert document["schema"] == CLAIM_SCHEMA
    assert document["rule_version"] == CLAIM_RULE_VERSION
    assert document["inputs"]["credibility"] == "credible"
    assert json.loads(json.dumps(document)) == document


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        # Hard stops.
        ({"integrity": "invalid"}, "no_claim"),
        ({"run_status": "RUN_FAILED"}, "no_claim"),
        ({"run_status": None}, "no_claim"),
        ({"evidence_level": "diagnostic_only"}, "no_claim"),
        ({"evidence_level": None}, "no_claim"),
        ({"qualification_level": "failed"}, "no_claim"),
        ({"credibility": "not_credible"}, "no_claim"),
        ({"compatibility": "incompatible"}, "no_claim"),
        # Evidence-level baseline.
        ({"evidence_level": "limited"}, "bounded"),
        ({"evidence_level": "functional"}, "exploratory"),
        # Qualification narrows but never raises.
        ({"qualification_level": "partial"}, "bounded"),
        ({"qualification_level": None}, "exploratory"),
        ({"qualification_level": "not-a-level"}, "exploratory"),
        # Credibility narrows.
        ({"credibility": "limited"}, "bounded"),
        ({"credibility": "unknown"}, "exploratory"),
        ({"credibility": None}, "exploratory"),
        # Compatibility narrows; a read-only package cannot be comparison eligible.
        ({"compatibility": "read_only"}, "bounded"),
        ({"compatibility": None}, "bounded"),
        # A legacy qualification token normalises before it is judged.
        ({"qualification_level": "full"}, "comparison_eligible"),
        ({"qualification_level": "limited"}, "bounded"),
        # Combined narrowing picks the weakest applicable ceiling.
        (
            {"qualification_level": "partial", "credibility": "unknown"},
            "exploratory",
        ),
        (
            {"evidence_level": "functional", "credibility": "credible"},
            "exploratory",
        ),
    ],
)
def test_matrix(overrides, expected):
    assert _derive(**overrides).conclusion == expected


def test_narrowed_by_names_the_cap_that_applied():
    conclusion = _derive(qualification_level="partial")

    assert conclusion.conclusion == "bounded"
    assert any("qualification is partial" in item for item in conclusion.narrowed_by)

    read_only = _derive(compatibility="read_only")
    assert any("not current" in item for item in read_only.narrowed_by)
    assert any("not new evidence" in item for item in read_only.limitations)


def test_a_hard_stop_short_circuits_even_with_maximal_inputs():
    conclusion = _derive(integrity="invalid")

    assert conclusion.conclusion == "no_claim"
    assert conclusion.may_claim == ()
    assert conclusion.narrowed_by


def test_claim_never_upgrades_any_input():
    """Property check: the conclusion rank never exceeds the evidence baseline."""

    evidence_rank = {
        "comparable": 3,
        "limited": 2,
        "functional": 1,
        "diagnostic_only": 0,
    }
    ranks = {name: rank for rank, name in enumerate(CLAIM_CONCLUSIONS)}
    for integrity, status, evidence, qualification, credibility, compatibility in itertools.product(
        ("ok", "invalid"),
        ("RUN_OK", "RUN_FAILED"),
        ("comparable", "limited", "functional", "diagnostic_only"),
        ("assessed", "partial", "failed"),
        ("credible", "limited", "unknown", "not_credible"),
        ("current", "read_only", "incompatible"),
    ):
        conclusion = derive_claim(
            integrity=integrity,
            run_status=status,
            evidence_level=evidence,
            qualification_level=qualification,
            credibility=credibility,
            compatibility=compatibility,
        )
        assert conclusion.conclusion in CLAIM_CONCLUSIONS
        assert ranks[conclusion.conclusion] <= evidence_rank[evidence], (
            integrity,
            status,
            evidence,
            qualification,
            credibility,
            compatibility,
            conclusion.to_dict(),
        )


def test_malformed_inputs_do_not_raise_and_stay_conservative():
    conclusion = derive_claim(
        integrity=None,  # type: ignore[arg-type]
        run_status={"status": "RUN_OK"},  # type: ignore[arg-type]
        evidence_level=3.14,  # type: ignore[arg-type]
        qualification_level=object(),  # type: ignore[arg-type]
        credibility=["credible"],  # type: ignore[arg-type]
        compatibility=True,  # type: ignore[arg-type]
    )

    assert conclusion.conclusion == "no_claim"
    assert conclusion.inputs["integrity"] is None
    assert conclusion.inputs["evidence_level"] is None


def test_unknown_conclusion_is_rejected():
    with pytest.raises(ValueError, match="unsupported claim conclusion"):
        ClaimConclusion("probably_fine", {})


def test_every_conclusion_has_a_statement_and_explicit_limits():
    for name in CLAIM_CONCLUSIONS:
        conclusion = ClaimConclusion(name, {})
        assert conclusion.statement
        assert conclusion.may_not_claim, name
        assert conclusion.limitations == () or isinstance(conclusion.limitations, tuple)


def test_inspect_run_emits_a_claim_consistent_with_the_summary(tmp_path):
    run_dir, _, _ = _published_buck_run(tmp_path)

    result = inspect_run(run_dir)

    claim = result.summary["claim"]
    assert claim["schema"] == CLAIM_SCHEMA
    assert claim["conclusion"] in CLAIM_CONCLUSIONS
    assert claim["inputs"]["integrity"] == result.status
    assert claim["inputs"]["run_status"] == result.summary["run_status"]
    assert claim["inputs"]["evidence_level"] == result.summary["evidence_level"]
    assert claim["inputs"]["qualification_level"] == result.summary["qualification_level"]
    assert claim["inputs"]["credibility"] == result.summary["credibility"]["status"]
    assert claim["inputs"]["compatibility"] == result.summary["compatibility"]["status"]
    assert claim["conclusion"] == "exploratory"


def test_older_package_claim_is_capped_by_its_evidence():
    result = inspect_run(OLD_PACKAGE)

    claim = result.summary["claim"]
    assert result.status == "ok", result.problems
    assert result.summary["compatibility"]["status"] == "read_only"
    assert result.summary["evidence_level"] == "functional"
    assert claim["conclusion"] == "exploratory"
    assert any("not current" in item for item in claim["narrowed_by"])


def test_claim_is_reachable_from_the_top_level_experimental_surface():
    assert "CLAIM_SCHEMA" in pe_sim.__all__
    assert "derive_claim" in pe_sim.__all__
    assert pe_sim.CLAIM_CONCLUSIONS == CLAIM_CONCLUSIONS

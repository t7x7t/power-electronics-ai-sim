"""Compatibility verdicts for published packages (D4 read-with-policy)."""

from __future__ import annotations

import dataclasses
import json

import pytest

from pe_sim.compatibility import (
    COMPATIBILITY_SCHEMA,
    COMPATIBILITY_STATUSES,
    CompatibilityVerdict,
    assess_package_compatibility,
)
from pe_sim.contracts import CURRENT_SCHEMA_VERSION


def _manifest(schema_version):
    return {"schema_version": schema_version, "run_id": "compatibility-run"}


def test_current_package_is_current_and_never_rewritten():
    verdict = assess_package_compatibility(_manifest(CURRENT_SCHEMA_VERSION))

    assert verdict.status == "current"
    assert verdict.cross_version_comparison == "current"
    assert verdict.package_schema == CURRENT_SCHEMA_VERSION
    assert verdict.reader_schema == CURRENT_SCHEMA_VERSION
    assert verdict.reasons == ()
    assert verdict.limitations == ()
    assert verdict.schema == COMPATIBILITY_SCHEMA
    assert verdict.may_rewrite is False


def test_older_package_is_read_only_and_names_both_versions():
    verdict = assess_package_compatibility(_manifest("0.1"))
    reason = " ".join(verdict.reasons)
    limitations = " ".join(verdict.limitations).lower()

    assert verdict.status == "read_only"
    assert verdict.cross_version_comparison == "not_assessed"
    assert "0.1" in reason
    assert CURRENT_SCHEMA_VERSION in reason
    assert verdict.limitations
    assert "absent" in limitations
    assert "not assessed" in limitations
    assert verdict.may_rewrite is False
    # The registry says which hop makes the package readable, and that the
    # mapping applies to a reader-side copy only.
    assert verdict.migration == {
        "from": "0.1",
        "to": CURRENT_SCHEMA_VERSION,
        "path": ["0.1", CURRENT_SCHEMA_VERSION],
    }
    assert any("reader-side copy" in item for item in verdict.limitations)


def test_an_older_schema_with_no_registered_path_is_incompatible():
    verdict = assess_package_compatibility(_manifest("0.0"))

    assert verdict.status == "incompatible"
    assert verdict.cross_version_comparison == "unsupported"
    assert any("no registered migration" in reason for reason in verdict.reasons)
    assert verdict.migration is None


@pytest.mark.parametrize("schema_version", ["0.3", "1.0"])
def test_newer_packages_are_incompatible(schema_version):
    verdict = assess_package_compatibility(_manifest(schema_version))

    assert verdict.status == "incompatible"
    assert verdict.cross_version_comparison == "unsupported"
    assert verdict.package_schema == schema_version
    assert verdict.reasons
    assert verdict.may_rewrite is False


@pytest.mark.parametrize(
    "manifest",
    [
        {},
        {"schema_version": ""},
        {"schema_version": "   "},
        {"schema_version": None},
        {"schema_version": 1},
        {"schema_version": ["0.1"]},
        {"schema_version": "one"},
        {"schema_version": "0"},
        {"schema_version": "0.1.x"},
        {"schema_version": ".1"},
    ],
)
def test_malformed_manifests_are_incompatible_without_raising(manifest):
    verdict = assess_package_compatibility(manifest)

    assert verdict.status == "incompatible"
    assert verdict.cross_version_comparison == "unsupported"
    assert verdict.package_schema is None
    assert verdict.reasons
    assert verdict.may_rewrite is False


def test_non_mapping_manifest_is_incompatible_without_raising():
    verdict = assess_package_compatibility(None)  # type: ignore[arg-type]

    assert verdict.status == "incompatible"
    assert verdict.cross_version_comparison == "unsupported"
    assert verdict.reasons


def test_patch_suffix_is_ignored_for_ordering():
    current = assess_package_compatibility(_manifest(f"{CURRENT_SCHEMA_VERSION}.4"))
    older = assess_package_compatibility(_manifest("0.1.9"))

    assert current.status == "current"
    assert current.cross_version_comparison == "current"
    assert current.package_schema == f"{CURRENT_SCHEMA_VERSION}.4"
    assert current.reasons == ()

    assert older.status == "read_only"
    assert older.cross_version_comparison == "not_assessed"
    assert older.migration["from"] == "0.1"


def test_to_dict_carries_the_schema_tag_and_every_field():
    assert COMPATIBILITY_STATUSES == ("current", "read_only", "incompatible")

    verdict = assess_package_compatibility(_manifest("0.1"))
    payload = verdict.to_dict()
    field_names = {item.name for item in dataclasses.fields(CompatibilityVerdict)}

    assert payload["schema"] == COMPATIBILITY_SCHEMA
    assert set(payload) == field_names
    assert payload["status"] == "read_only"
    assert payload["package_schema"] == "0.1"
    assert payload["reader_schema"] == CURRENT_SCHEMA_VERSION
    assert payload["cross_version_comparison"] == "not_assessed"
    assert payload["reasons"] == list(verdict.reasons)
    assert payload["limitations"] == list(verdict.limitations)
    assert payload["may_rewrite"] is False
    assert payload["migration"]["to"] == CURRENT_SCHEMA_VERSION
    assert json.dumps(payload)


def test_may_rewrite_true_is_rejected():
    with pytest.raises(ValueError, match="may_rewrite"):
        CompatibilityVerdict(
            status="current",
            package_schema=CURRENT_SCHEMA_VERSION,
            reader_schema=CURRENT_SCHEMA_VERSION,
            cross_version_comparison="current",
            may_rewrite=True,
        )


def test_verdict_validates_status_reader_and_comparison():
    with pytest.raises(ValueError, match="status"):
        CompatibilityVerdict(
            status="migrated",
            package_schema=CURRENT_SCHEMA_VERSION,
            reader_schema=CURRENT_SCHEMA_VERSION,
            cross_version_comparison="current",
        )
    with pytest.raises(ValueError, match="reader_schema"):
        CompatibilityVerdict(
            status="current",
            package_schema=CURRENT_SCHEMA_VERSION,
            reader_schema="",
            cross_version_comparison="current",
        )
    with pytest.raises(ValueError, match="cross_version_comparison"):
        CompatibilityVerdict(
            status="current",
            package_schema=CURRENT_SCHEMA_VERSION,
            reader_schema=CURRENT_SCHEMA_VERSION,
            cross_version_comparison="compared",
        )

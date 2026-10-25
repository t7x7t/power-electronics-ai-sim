"""Schema 0.2 migration registry: read old, write new, never rewrite (D4).

The registry exists so the first schema bump is a declared hop instead of an
ad-hoc special case, and so the compatibility verdict can distinguish:

* an older package with a registered path -> ``read_only`` with a named mapping;
* an older package with no registered path -> ``incompatible``.
"""

from __future__ import annotations

import copy

import pytest

from pe_sim.compatibility import assess_package_compatibility
from pe_sim.contracts import CURRENT_SCHEMA_VERSION
from pe_sim.schema_registry import (
    MIGRATIONS,
    SCHEMA_REGISTRY_VERSION,
    SchemaMigrationError,
    migration_path,
    normalize_manifest,
    registered_versions,
    registry_document,
    schema_minor,
)


def test_the_registry_knows_the_current_version_and_the_first_hop():
    assert CURRENT_SCHEMA_VERSION == "0.2"
    assert ("0.1", "0.2") in MIGRATIONS
    assert set(registered_versions()) == {"0.1", "0.2"}


def test_migration_path_resolves_one_hop_and_the_identity():
    assert migration_path("0.2") == ("0.2",)
    assert migration_path("0.1") == ("0.1", "0.2")


@pytest.mark.parametrize("source", ["0.0", "0.3", "1.0", "", None])
def test_an_unregistered_source_is_refused(source):
    with pytest.raises(SchemaMigrationError):
        migration_path(source)  # type: ignore[arg-type]


def test_patch_suffix_is_ignored_for_the_graph():
    assert schema_minor("0.1.9") == "0.1"
    assert schema_minor("0.2") == "0.2"
    assert schema_minor("0") is None
    assert schema_minor(None) is None
    assert migration_path(schema_minor("0.1.9")) == ("0.1", "0.2")


def test_normalize_manifest_maps_every_qualification_level_it_owns():
    manifest = {
        "schema_version": "0.1",
        "qualification": {"level": "limited", "checks": {"level": "full"}},
        "checks": {
            "safety": {"level": "full"},
            "validity": {"level": "limited"},
            "qualification": {"level": "failed"},
        },
        "safety": {"checks": {"level": "limited"}},
    }

    document, notes = normalize_manifest(manifest)

    assert document["schema_version"] == CURRENT_SCHEMA_VERSION
    assert document["qualification"]["level"] == "partial"
    assert document["qualification"]["checks"]["level"] == "assessed"
    assert document["checks"]["safety"]["level"] == "assessed"
    assert document["checks"]["validity"]["level"] == "partial"
    assert document["checks"]["qualification"]["level"] == "failed"
    assert document["safety"]["checks"]["level"] == "partial"
    assert any(note.endswith("qualification.level") for note in notes)
    assert not any(note.endswith("checks.qualification.level") for note in notes)


def test_normalize_manifest_never_touches_the_evidence_ladder():
    manifest = {"schema_version": "0.1", "evidence_level": "limited"}

    document, _ = normalize_manifest(manifest)

    assert document["evidence_level"] == "limited"


def test_normalize_manifest_is_pure():
    manifest = {"schema_version": "0.1", "qualification": {"level": "limited"}}
    before = copy.deepcopy(manifest)

    document, _ = normalize_manifest(manifest)

    assert manifest == before
    assert document is not manifest
    assert document["qualification"] is not manifest["qualification"]


def test_a_manifest_without_a_version_is_refused():
    with pytest.raises(SchemaMigrationError):
        normalize_manifest({"run_id": "no-version"})
    with pytest.raises(SchemaMigrationError):
        normalize_manifest(None)  # type: ignore[arg-type]


def test_the_registry_document_is_machine_readable_and_read_only():
    document = registry_document()

    assert document["schema"] == SCHEMA_REGISTRY_VERSION
    assert document["current"] == CURRENT_SCHEMA_VERSION
    assert document["may_rewrite"] is False
    assert {"from": "0.1", "to": "0.2"} in document["migrations"]


def test_compatibility_uses_the_registry_for_read_only_and_incompatible():
    readable = assess_package_compatibility({"schema_version": "0.1"})
    unreadable = assess_package_compatibility({"schema_version": "0.0"})

    assert readable.status == "read_only"
    assert readable.migration == {"from": "0.1", "to": "0.2", "path": ["0.1", "0.2"]}
    assert readable.may_rewrite is False

    assert unreadable.status == "incompatible"
    assert unreadable.migration is None
    assert any("no registered migration" in reason for reason in unreadable.reasons)

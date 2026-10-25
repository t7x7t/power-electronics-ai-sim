"""Compatibility classification for published run packages (decision D4).

D4 is *read-with-policy*: a reader classifies a published package and states
the limits of reading it.  It never migrates and never rewrites the package.

Published packages are immutable.  If a migration is ever produced, it is a
new derived package that records a parent link to the package it was derived
from; the original package stays byte-identical.  ``may_rewrite`` is therefore
always ``False``.

This module only classifies manifests.  It never reads or writes files, and a
malformed manifest yields an ``incompatible`` verdict instead of an exception.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import re
from typing import Any

from .contracts import CURRENT_SCHEMA_VERSION
from .schema_registry import SchemaMigrationError, migration_path


COMPATIBILITY_SCHEMA = "pe-sim.compatibility.v1"
COMPATIBILITY_STATUSES: tuple[str, ...] = ("current", "read_only", "incompatible")

_CROSS_VERSION_COMPARISONS: tuple[str, ...] = ("current", "not_assessed", "unsupported")
_VERSION_RE = re.compile(r"[0-9]+\.[0-9]+(?:\.[0-9]+)?")
_MISSING = object()


@dataclass(frozen=True)
class CompatibilityVerdict:
    """One classification of a package's schema compatibility.

    ``status`` is ``current`` (readable as-is), ``read_only`` (readable under
    the policy of D4, with stated limits), or ``incompatible`` (not readable).
    ``may_rewrite`` is always ``False``: a verdict can never authorize a write
    into a published package.
    """

    status: str
    package_schema: str | None
    reader_schema: str
    reasons: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    # Semantically required; defaulted only because dataclasses forbid a
    # non-default field after defaulted ones.  ``not_assessed`` is the
    # conservative reading for direct construction.
    cross_version_comparison: str = "not_assessed"
    #: The reader-side mapping available for an older package, if any.  It is a
    #: description, never an authorization to write into the package.
    migration: Mapping[str, Any] | None = None
    schema: str = COMPATIBILITY_SCHEMA
    may_rewrite: bool = False

    def __post_init__(self) -> None:
        if self.status not in COMPATIBILITY_STATUSES:
            raise ValueError(f"unsupported compatibility status: {self.status!r}")
        if not isinstance(self.reader_schema, str) or not self.reader_schema.strip():
            raise ValueError("reader_schema must be a non-empty string")
        if self.cross_version_comparison not in _CROSS_VERSION_COMPARISONS:
            raise ValueError(
                f"unsupported cross_version_comparison: {self.cross_version_comparison!r}"
            )
        if self.may_rewrite is not False:
            raise ValueError(
                "may_rewrite must be False: published packages are immutable (D4)"
            )
        if self.migration is not None and not isinstance(self.migration, Mapping):
            raise ValueError("migration must be a mapping or None")

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema": self.schema,
            "status": self.status,
            "package_schema": self.package_schema,
            "reader_schema": self.reader_schema,
            "cross_version_comparison": self.cross_version_comparison,
            "reasons": list(self.reasons),
            "limitations": list(self.limitations),
            "may_rewrite": self.may_rewrite,
        }
        if self.migration is not None:
            payload["migration"] = dict(self.migration)
        return payload


def _incompatible(reason: str, *, package_schema: str | None = None) -> CompatibilityVerdict:
    """Build a fail-closed verdict for a package that must not be trusted."""

    return CompatibilityVerdict(
        status="incompatible",
        package_schema=package_schema,
        reader_schema=CURRENT_SCHEMA_VERSION,
        cross_version_comparison="unsupported",
        reasons=(reason,),
    )


def _parse_schema_version(value: str) -> tuple[int, int] | None:
    """Parse ``MAJOR.MINOR[.PATCH]``; the patch component is ignored."""

    if _VERSION_RE.fullmatch(value) is None:
        return None
    major, minor = value.split(".")[:2]
    return int(major), int(minor)


def assess_package_compatibility(manifest: Mapping[str, Any]) -> CompatibilityVerdict:
    """Classify a package manifest without reading or writing anything.

    Same major/minor as the reader -> ``current``.  An older package ->
    ``read_only`` with limitations: absent fields stay absent and cross-version
    comparability is not assessed.  A newer package, a missing or malformed
    ``schema_version``, or a non-mapping manifest -> ``incompatible``.
    Malformed input never raises, and no verdict ever permits a rewrite.
    """

    if not isinstance(manifest, Mapping):
        return _incompatible("manifest is not a mapping")

    declared = manifest.get("schema_version", _MISSING)
    if declared is _MISSING:
        return _incompatible("manifest does not declare a schema_version")
    if not isinstance(declared, str):
        return _incompatible(
            f"schema_version must be a string, got {type(declared).__name__}"
        )
    if not declared.strip():
        return _incompatible("schema_version is empty")

    package_parts = _parse_schema_version(declared)
    if package_parts is None:
        return _incompatible(
            f"schema_version {declared!r} is not MAJOR.MINOR or MAJOR.MINOR.PATCH"
        )

    reader_parts = _parse_schema_version(CURRENT_SCHEMA_VERSION)
    if reader_parts is None:  # pragma: no cover - CURRENT_SCHEMA_VERSION is valid
        return _incompatible(
            f"reader schema_version {CURRENT_SCHEMA_VERSION!r} is not comparable"
        )

    if package_parts == reader_parts:
        return CompatibilityVerdict(
            status="current",
            package_schema=declared,
            reader_schema=CURRENT_SCHEMA_VERSION,
            cross_version_comparison="current",
        )

    if package_parts < reader_parts:
        declared_minor = f"{package_parts[0]}.{package_parts[1]}"
        try:
            path = migration_path(declared_minor, CURRENT_SCHEMA_VERSION)
        except SchemaMigrationError as exc:
            return _incompatible(
                f"no registered migration for schema {declared_minor}: {exc}",
                package_schema=declared,
            )
        return CompatibilityVerdict(
            status="read_only",
            package_schema=declared,
            reader_schema=CURRENT_SCHEMA_VERSION,
            cross_version_comparison="not_assessed",
            reasons=(
                "package schema_version "
                f"{declared} is older than reader schema_version "
                f"{CURRENT_SCHEMA_VERSION}",
                "registered migration path: " + " -> ".join(path),
            ),
            limitations=(
                "the mapping is applied to a reader-side copy only; the published "
                "package is never rewritten",
                "fields absent from the package are not inferred or backfilled; "
                "absence remains absence",
                "cross-version comparability is not assessed; only packages "
                "written at the same schema version are directly comparable",
            ),
            migration={
                "from": declared_minor,
                "to": CURRENT_SCHEMA_VERSION,
                "path": list(path),
            },
        )

    return _incompatible(
        "package schema_version "
        f"{declared} is newer than reader schema_version {CURRENT_SCHEMA_VERSION}",
        package_schema=declared,
    )


__all__ = [
    "COMPATIBILITY_SCHEMA",
    "COMPATIBILITY_STATUSES",
    "CompatibilityVerdict",
    "assess_package_compatibility",
]

"""Read-only schema migration registry (decision D4).

A published package is immutable: a migration is never applied to it.  A
*migration* here is a pure function that maps an older manifest mapping onto the
current vocabulary **for a reader**, returning the changed field paths so the
read can say what it translated.

Why a registry at all?  ``docs/43`` requires the explicit registry to exist at
the first schema bump rather than be retrofitted at the second.  The registry
also gives the compatibility verdict teeth: an older package with a registered
path is ``read_only`` with a stated mapping, and an older package with no
registered path is ``incompatible`` instead of being read optimistically.

Nothing in this module writes, repairs, or re-signs a package.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from .checks import LEGACY_CHECK_LEVELS
from .contracts import CURRENT_SCHEMA_VERSION

#: Schema tag for the registry description document.
SCHEMA_REGISTRY_VERSION = "pe-sim.schema-registry.v1"


class SchemaMigrationError(ValueError):
    """Raised when no registered migration can produce the current schema."""


#: ``source manifest -> (normalized manifest, changed field paths)``.
Migration = Callable[[Mapping[str, Any]], "tuple[dict[str, Any], tuple[str, ...]]"]


def _rewrite_levels(
    value: Any,
    path: str,
    notes: list[str],
) -> Any:
    """Copy ``value``, mapping legacy qualification levels to current ones.

    Only a mapping key named exactly ``level`` is considered, and only when its
    value is one of the legacy qualification words.  ``evidence_level`` and
    unrelated ``level`` keys are untouched, so the mapper cannot silently
    rewrite an evidence claim.
    """

    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            child_path = f"{path}.{key}" if path else str(key)
            if key == "level" and isinstance(item, str) and item in LEGACY_CHECK_LEVELS:
                result[key] = LEGACY_CHECK_LEVELS[item]
                notes.append(child_path)
            else:
                result[key] = _rewrite_levels(item, child_path, notes)
        return result
    if isinstance(value, (list, tuple)):
        return [
            _rewrite_levels(item, f"{path}[{index}]", notes)
            for index, item in enumerate(value)
        ]
    return value


def _v01_to_v02(manifest: Mapping[str, Any]) -> tuple[dict[str, Any], tuple[str, ...]]:
    """Map manifest schema 0.1 to 0.2.

    0.2 renames the qualification ladder to ``assessed`` / ``partial`` /
    ``failed`` so it no longer shares a token with the evidence ladder.  The
    evidence ladder itself is unchanged.
    """

    notes: list[str] = []
    normalized = _rewrite_levels(manifest, "", notes)
    return dict(normalized), tuple(notes)


#: Registered hops.  A hop is ``(from_version, to_version) -> migration``.
MIGRATIONS: dict[tuple[str, str], Migration] = {
    ("0.1", "0.2"): _v01_to_v02,
}


def schema_minor(value: object) -> str | None:
    """Return ``MAJOR.MINOR`` for a schema string, ignoring a patch suffix."""

    if not isinstance(value, str):
        return None
    parts = value.strip().split(".")
    if len(parts) < 2 or not parts[0].isdigit() or not parts[1].isdigit():
        return None
    return f"{int(parts[0])}.{int(parts[1])}"


def registered_versions() -> tuple[str, ...]:
    """Return every schema version the registry can read or produce."""

    versions = {CURRENT_SCHEMA_VERSION}
    for source, target in MIGRATIONS:
        versions.update({source, target})
    return tuple(sorted(versions))


def migration_path(
    source: str, target: str = CURRENT_SCHEMA_VERSION
) -> tuple[str, ...]:
    """Return the ordered versions on a registered path, or raise.

    ``("0.1", "0.2")`` means one hop.  ``("0.2",)`` means no hop is needed.
    """

    if not isinstance(source, str) or not source:
        raise SchemaMigrationError("schema version must be a non-empty string")
    if source == target:
        return (source,)
    # Breadth-first over the registered hops so the shortest path wins.
    queue: list[tuple[str, ...]] = [(source,)]
    seen = {source}
    while queue:
        path = queue.pop(0)
        current = path[-1]
        for (hop_source, hop_target) in MIGRATIONS:
            if hop_source != current or hop_target in seen:
                continue
            candidate = path + (hop_target,)
            if hop_target == target:
                return candidate
            seen.add(hop_target)
            queue.append(candidate)
    raise SchemaMigrationError(
        f"no registered migration from schema {source!r} to {target!r}; "
        f"registered versions: {list(registered_versions())}"
    )


def normalize_manifest(
    manifest: Mapping[str, Any], *, target: str = CURRENT_SCHEMA_VERSION
) -> tuple[dict[str, Any], tuple[str, ...]]:
    """Return a reader-side copy of ``manifest`` at ``target``.

    The input mapping is never mutated.  ``ValueError`` is raised for a
    non-mapping or a version outside the registered graph.
    """

    if not isinstance(manifest, Mapping):
        raise SchemaMigrationError("manifest must be a mapping")
    source = manifest.get("schema_version")
    if not isinstance(source, str) or not source:
        raise SchemaMigrationError("manifest does not declare a schema_version")
    path = migration_path(source, target)
    document: dict[str, Any] = dict(manifest)
    notes: list[str] = []
    for hop_source, hop_target in zip(path, path[1:]):
        migration = MIGRATIONS[(hop_source, hop_target)]
        document, changed = migration(document)
        notes.extend(f"{hop_source}->{hop_target}:{item}" for item in changed)
    document["schema_version"] = target
    return document, tuple(notes)


def registry_document() -> dict[str, Any]:
    """Describe the registry for documentation and tests."""

    return {
        "schema": SCHEMA_REGISTRY_VERSION,
        "current": CURRENT_SCHEMA_VERSION,
        "registered_versions": list(registered_versions()),
        "migrations": [
            {"from": source, "to": target}
            for (source, target) in sorted(MIGRATIONS)
        ],
        "may_rewrite": False,
    }


__all__ = [
    "MIGRATIONS",
    "SCHEMA_REGISTRY_VERSION",
    "SchemaMigrationError",
    "migration_path",
    "normalize_manifest",
    "registered_versions",
    "registry_document",
    "schema_minor",
]

"""Read-only catalog of published run packages.

This module answers "what runs exist here?" without answering "is this package
intact?".  Those are different questions with different costs:

- discovery must be cheap, because a client asks it constantly;
- verification is expensive and already lives in :mod:`pe_sim.inspection`.

Re-implementing verification here would create a second definition of "valid
package", so the catalog deliberately reads recorded metadata only and says so
in ``limitations``.  A caller that needs integrity calls ``inspect_run``.

Nothing in this module writes, repairs, or deletes anything.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
from typing import Any, Mapping

from .jsonutil import json_safe


CATALOG_SCHEMA = "pe-sim.run-catalog.v1"

# Directories that look like a run but are not published yet use a leading dot
# (temporary staging directories), so they are pruned during the walk.
_HIDDEN_PREFIX = "."
_MANIFEST = "manifest.json"
_STATE = ".run-state.json"


class CatalogError(ValueError):
    """Raised when a catalog request cannot be served."""


@dataclass(frozen=True)
class RunEntry:
    """One discovered run, described only by what its files record."""

    run_id: str
    path: str
    experiment_id: str | None = None
    status: str | None = None
    run_state: str | None = None
    published: bool = False
    evidence_level: str | None = None
    qualification_level: str | None = None
    plant_id: str | None = None
    plant_class: str | None = None
    trust_level: str | None = None
    created_at: str | None = None
    manifest_sha256: str | None = None
    problems: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "path": self.path,
            "experiment_id": self.experiment_id,
            "status": self.status,
            "run_state": self.run_state,
            "published": self.published,
            "evidence_level": self.evidence_level,
            "qualification_level": self.qualification_level,
            "plant_id": self.plant_id,
            "plant_class": self.plant_class,
            "trust_level": self.trust_level,
            "created_at": self.created_at,
            "manifest_sha256": self.manifest_sha256,
            "problems": list(self.problems),
        }

    def matches(
        self,
        *,
        plant_id: str | None = None,
        status: str | None = None,
        run_id_prefix: str | None = None,
        published_only: bool = False,
    ) -> bool:
        if plant_id is not None and self.plant_id != plant_id:
            return False
        if status is not None and self.status != status:
            return False
        if run_id_prefix is not None and not self.run_id.startswith(run_id_prefix):
            return False
        if published_only and not self.published:
            return False
        return True


@dataclass(frozen=True)
class RunCatalog:
    root: str
    runs: tuple[RunEntry, ...] = ()
    skipped: tuple[Mapping[str, Any], ...] = ()
    limitations: tuple[str, ...] = (
        "entries are read from recorded metadata only; call inspect_run to verify a package",
        "the catalog never writes, repairs, or deletes a run",
    )
    schema_version: str = CATALOG_SCHEMA

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "root": self.root,
            "count": len(self.runs),
            "runs": [entry.to_dict() for entry in self.runs],
            "skipped": [json_safe(dict(item)) for item in self.skipped],
            "limitations": list(self.limitations),
        }


def _load_object(path: Path) -> Mapping[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, Mapping) else None


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def recorded_plant_class(plant: Mapping[str, Any]) -> str | None:
    """Read the adapter class from either recorded identity shape.

    A fixture component records ``identity.class`` directly; a reference or
    engine adapter records ``identity.implementation.class``.  Both shapes are
    legitimate historical data, so one reader handles both instead of every
    consumer guessing.
    """

    identity = plant.get("identity")
    if not isinstance(identity, Mapping):
        return None
    direct = identity.get("class")
    if isinstance(direct, str) and direct:
        return direct
    implementation = identity.get("implementation")
    nested = implementation.get("class") if isinstance(implementation, Mapping) else None
    return nested if isinstance(nested, str) and nested else None


def _entry(directory: Path, root: Path, manifest: Mapping[str, Any]) -> RunEntry:
    problems: list[str] = []
    state = _load_object(directory / _STATE)
    run_state = _text(state.get("status")) if state else None
    if state is None:
        problems.append("run state marker is missing or unreadable")
    plant = manifest.get("plant") if isinstance(manifest.get("plant"), Mapping) else {}
    identity = plant.get("identity") if isinstance(plant.get("identity"), Mapping) else {}
    qualification = manifest.get("qualification") if isinstance(manifest.get("qualification"), Mapping) else {}
    run_id = _text(manifest.get("run_id")) or directory.name
    try:
        relative = directory.relative_to(root).as_posix()
    except ValueError:  # pragma: no cover - directory always comes from the walk
        relative = directory.as_posix()
    return RunEntry(
        run_id=run_id,
        path=relative,
        experiment_id=_text(manifest.get("experiment_id")),
        status=_text(manifest.get("status")),
        run_state=run_state,
        published=run_state == "PUBLISHED",
        evidence_level=_text(manifest.get("evidence_level")),
        qualification_level=_text(qualification.get("level")),
        plant_id=_text(plant.get("id")),
        plant_class=recorded_plant_class(plant),
        trust_level=_text(plant.get("trust_level")),
        created_at=_text(manifest.get("created_at")),
        manifest_sha256=_text(manifest.get("manifest_sha256")),
        problems=tuple(problems),
    )


def scan_runs(
    root: str | Path,
    *,
    plant_id: str | None = None,
    status: str | None = None,
    run_id_prefix: str | None = None,
    published_only: bool = False,
    limit: int | None = None,
    max_depth: int = 6,
) -> RunCatalog:
    """Walk ``root`` and describe every run package found below it.

    Ordering is deterministic: newest ``created_at`` first, then ``run_id``.
    Directories that look like a run but cannot be read are reported in
    ``skipped`` rather than silently dropped, because a missing entry is
    indistinguishable from a missing run.
    """

    if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit < 0):
        raise CatalogError("limit must be a non-negative integer or None")
    if isinstance(max_depth, bool) or not isinstance(max_depth, int) or max_depth < 0:
        raise CatalogError("max_depth must be a non-negative integer")
    base = Path(root).expanduser()
    if not base.is_dir():
        raise CatalogError(f"runs root does not exist or is not a directory: {base}")
    base = base.resolve()

    entries: list[RunEntry] = []
    skipped: list[dict[str, Any]] = []
    for current, dirnames, filenames in os.walk(base):
        depth = len(Path(current).relative_to(base).parts)
        # Prune hidden staging directories and anything past the depth limit so
        # a stray node_modules-style tree cannot turn discovery into a crawl.
        dirnames[:] = sorted(name for name in dirnames if not name.startswith(_HIDDEN_PREFIX))
        if depth >= max_depth:
            dirnames[:] = []
        if _MANIFEST not in filenames:
            continue
        directory = Path(current)
        try:
            relative = directory.relative_to(base).as_posix()
        except ValueError:  # pragma: no cover
            relative = directory.as_posix()
        manifest = _load_object(directory / _MANIFEST)
        if manifest is None:
            skipped.append({"path": relative, "reason": "manifest.json is missing or unreadable"})
            continue
        entries.append(_entry(directory, base, manifest))

    entries = [
        entry
        for entry in entries
        if entry.matches(
            plant_id=plant_id,
            status=status,
            run_id_prefix=run_id_prefix,
            published_only=published_only,
        )
    ]
    entries.sort(key=lambda item: (item.created_at or "", item.run_id), reverse=True)
    if limit is not None:
        entries = entries[:limit]
    return RunCatalog(root=str(base), runs=tuple(entries), skipped=tuple(skipped))


__all__ = [
    "CATALOG_SCHEMA",
    "CatalogError",
    "RunCatalog",
    "RunEntry",
    "recorded_plant_class",
    "scan_runs",
]

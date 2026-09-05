"""Release/baseline evidence record.

The report is a template around existing provenance.  CI and human review are
never inferred: omitted values are explicitly ``not_recorded``.
"""

from __future__ import annotations

from pathlib import Path
import json
from typing import Any, Mapping


def _manifest(value: str | Path | Mapping[str, Any]) -> tuple[dict[str, Any], str]:
    if isinstance(value, Mapping):
        return dict(value), "manifest"
    path = Path(value)
    if path.is_dir():
        path = path / "manifest.json"
    return json.loads(path.read_text(encoding="utf-8")), str(path)


def build_baseline_report(
    manifest: str | Path | Mapping[str, Any],
    *,
    ci: Mapping[str, Any] | None = None,
    human_review: Mapping[str, Any] | None = None,
    release_id: str | None = None,
) -> dict[str, Any]:
    """Build a JSON-safe baseline report without inventing approval values."""

    value, source = _manifest(manifest)
    environment = value.get("environment")
    report: dict[str, Any] = {
        "schema_version": "1.0",
        "report_type": "release_baseline",
        "release_id": release_id,
        "source": source,
        "source_commit": value.get("source_commit"),
        "branch": value.get("branch"),
        "working_tree_status": value.get("working_tree_status"),
        "provenance_status": value.get("provenance", {}).get("status") if isinstance(value.get("provenance"), Mapping) else None,
        "manifest_sha256": value.get("manifest_sha256"),
        "package_sha256": value.get("package_sha256"),
        "environment": environment if isinstance(environment, Mapping) else None,
        "tests": {"status": "not_recorded", "command": None, "evidence": None},
        "ci": dict(ci) if ci is not None else {"status": "not_recorded", "workflow": None, "run_id": None, "url": None},
        "human_review": dict(human_review) if human_review is not None else {"status": "not_recorded", "reviewer": None, "reviewed_at": None, "decision": None, "notes": None},
        "limitations": ["This report records evidence and review fields; it does not approve downstream engineering use."],
    }
    if value.get("working_tree_status") != "clean":
        report["limitations"].append("The source worktree is not clean; this is not a formal baseline.")
    if not value.get("source_commit"):
        report["limitations"].append("No source commit was recorded.")
    return report


def write_baseline_report(path: str | Path, manifest: str | Path | Mapping[str, Any], **kwargs: Any) -> Path:
    """Write canonical JSON and return its path."""

    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(build_baseline_report(manifest, **kwargs), ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return destination


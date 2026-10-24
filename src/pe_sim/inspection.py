"""Read-only verification and summary of a published run package.

This module answers the user questions that do not require importing a Plant,
Controller, or solver adapter:

- is the package still internally consistent?
- which backend and implementation produced it, and does the implementation
  match the checked-in review pin?
- what do the recorded samples and qualification findings actually contain?
- did the run fail, and if so, what did the backend report?

It never writes into the run package and never executes a simulator.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import json
import math
from pathlib import Path
from typing import Any, Mapping

from .artifacts import artifact_index, manifest_digest, package_digest
from .credibility import CredibilityPolicy, assess_credibility


#: Schema tag for the read-only view returned by ``pe-sim inspect-run``.  It
#: labels this JSON view only: the authoritative evidence remains the
#: published package plus its manifest.
INSPECTION_SCHEMA = "pe-sim.run-inspection.v1"


class RunInspectionError(ValueError):
    """Raised when a run directory cannot be inspected at all."""


@dataclass(frozen=True)
class RunInspection:
    run_dir: Path
    status: str
    problems: tuple[str, ...]
    summary: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": INSPECTION_SCHEMA,
            "run_dir": str(self.run_dir),
            "status": self.status,
            "problems": list(self.problems),
            **dict(self.summary),
        }


def _load_json(path: Path, description: str) -> Mapping[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RunInspectionError(f"{description} is unreadable: {type(exc).__name__}: {exc}") from exc
    if not isinstance(value, Mapping):
        raise RunInspectionError(f"{description} must be a JSON object")
    return value


def _load_optional_json(path: Path) -> Mapping[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, Mapping) else None


def _load_samples(path: Path) -> list[Mapping[str, Any]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RunInspectionError(f"samples.json is unreadable: {type(exc).__name__}: {exc}") from exc
    if not isinstance(value, list):
        raise RunInspectionError("samples.json must be an array")
    return [row for row in value if isinstance(row, Mapping)]


def _numeric_stats(rows: list[Mapping[str, Any]]) -> tuple[dict[str, dict[str, float]], list[dict[str, Any]]]:
    keys: set[str] = set()
    for row in rows:
        keys.update(
            key
            for key, value in row.items()
            if isinstance(value, (int, float)) and not isinstance(value, bool)
        )
    stats: dict[str, dict[str, float]] = {}
    non_finite: list[dict[str, Any]] = []
    for key in sorted(keys):
        values: list[float] = []
        for index, row in enumerate(rows):
            value = row.get(key)
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                continue
            number = float(value)
            if not math.isfinite(number):
                non_finite.append({"key": key, "index": index})
                continue
            values.append(number)
        if values:
            stats[key] = {
                "count": len(values),
                "min": min(values),
                "max": max(values),
                "mean": sum(values) / len(values),
                "last": values[-1],
            }
    return stats, non_finite


def _time_is_monotonic(rows: list[Mapping[str, Any]], tolerance_s: float = 1e-12) -> bool:
    previous: float | None = None
    for row in rows:
        value = row.get("time_s")
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            continue
        current = float(value)
        if not math.isfinite(current):
            return False
        if previous is not None and current < previous - tolerance_s:
            return False
        previous = current
    return True


def inspect_run(
    run_dir: str | Path,
    *,
    credibility_policy: CredibilityPolicy | None = None,
    attestation_path: str | Path | None = None,
    repo_root: str | Path | None = None,
    verify_anchor: bool = True,
) -> RunInspection:
    """Verify hashes, artifact indexes, samples, and provenance of one run.

    ``status`` describes package integrity only.  ``run_status`` and
    ``run_ok`` report whether the recorded execution succeeded; a failed but
    intact package is still inspectable and is not reported as tampered.
    The added ``credibility`` section is a read-only assessment of recorded
    evidence; it never changes ``status``.
    """

    directory = Path(run_dir).expanduser().resolve()
    required = ("manifest.json", ".run-state.json", "samples.json")
    missing = [name for name in required if not (directory / name).is_file()]
    if missing:
        raise RunInspectionError(f"run package is missing required files: {missing}")

    manifest = _load_json(directory / "manifest.json", "manifest.json")
    state = _load_json(directory / ".run-state.json", ".run-state.json")
    samples = _load_samples(directory / "samples.json")
    problems: list[str] = []

    run_id = manifest.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        problems.append("manifest run_id is missing or invalid")
    if state.get("status") != "PUBLISHED":
        problems.append(f"run state is {state.get('status')!r}, not 'PUBLISHED'")
    if state.get("run_id") not in {None, run_id}:
        problems.append("run state marker does not match manifest run_id")

    actual_artifacts: Mapping[str, Any] = {}
    try:
        actual_artifacts = artifact_index(directory)
        recorded = manifest.get("artifacts")
        if not isinstance(recorded, Mapping) or dict(recorded) != dict(actual_artifacts):
            problems.append("manifest artifact index does not match package contents")
    except (OSError, ValueError) as exc:
        problems.append(f"artifact index could not be verified: {type(exc).__name__}: {exc}")

    try:
        if manifest_digest(manifest) != manifest.get("manifest_sha256"):
            problems.append("manifest_sha256 does not match manifest contents")
    except (TypeError, ValueError) as exc:
        problems.append(f"manifest_sha256 could not be verified: {type(exc).__name__}: {exc}")

    try:
        if package_digest(directory, manifest) != manifest.get("package_sha256"):
            problems.append("package_sha256 does not match package contents")
    except (OSError, TypeError, ValueError) as exc:
        problems.append(f"package_sha256 could not be verified: {type(exc).__name__}: {exc}")

    stats, non_finite = _numeric_stats(samples)
    if non_finite:
        problems.append("samples contain non-finite numeric values")
    time_monotonic = _time_is_monotonic(samples)
    if not time_monotonic:
        problems.append("sample time_s is not monotonic")

    metrics_path = directory / "metrics.json"
    if metrics_path.is_file():
        try:
            metrics = _load_json(metrics_path, "metrics.json")
            recorded_count = metrics.get("sample_count")
            if isinstance(recorded_count, int) and recorded_count != len(samples):
                problems.append(
                    f"metrics sample_count {recorded_count} does not match samples.json length {len(samples)}"
                )
        except RunInspectionError as exc:
            problems.append(str(exc))

    plant = manifest.get("plant")
    plant_map = plant if isinstance(plant, Mapping) else {}
    identity = plant_map.get("identity")
    identity_map = identity if isinstance(identity, Mapping) else {}
    implementation = identity_map.get("implementation")
    implementation_map = implementation if isinstance(implementation, Mapping) else {}
    review = implementation_map.get("review")
    recorded_implementation = implementation_map.get("sha256")
    current_review = None
    backend_id = plant_map.get("id")
    if isinstance(recorded_implementation, str) and isinstance(backend_id, str):
        try:
            # Compare against the current checkout's pin, not only the
            # status that the producing process recorded at run time.
            from .identity import review_status

            current_review = review_status(backend_id, recorded_implementation)
        except (KeyError, ValueError, FileNotFoundError):
            current_review = None

    backends = manifest.get("environment")
    backend_list = backends.get("backends") if isinstance(backends, Mapping) else None
    if not isinstance(backend_list, list):
        backend_list = []

    times = [float(row["time_s"]) for row in samples if isinstance(row.get("time_s"), (int, float))]
    summary: dict[str, Any] = {
        "run_id": run_id,
        "run_status": manifest.get("status"),
        "run_ok": manifest.get("status") in {"RUN_OK", "QUALIFIED", "COMPARABLE"},
        "run_mode": manifest.get("run_mode"),
        "qualification": manifest.get("qualification"),
        "safety_passed": (manifest.get("safety") or {}).get("passed") if isinstance(manifest.get("safety"), Mapping) else None,
        "error": manifest.get("error"),
        "failure": manifest.get("failure"),
        "source_commit": manifest.get("source_commit"),
        "working_tree_status": manifest.get("working_tree_status"),
        "hashes": manifest.get("hashes"),
        "artifact_count": len(actual_artifacts),
        "backend": backend_list,
        "plant": {
            "id": plant_map.get("id"),
            "hash": plant_map.get("hash"),
            "identity_sha256": identity_map.get("sha256"),
            "implementation": {
                "sha256": recorded_implementation,
                "review": review,
                "current_checkout_review": current_review,
            },
            "evidence": plant_map.get("evidence"),
        },
        "samples": {
            "count": len(samples),
            "first_time_s": times[0] if times else None,
            "last_time_s": times[-1] if times else None,
            "time_monotonic": time_monotonic,
            "non_finite": non_finite,
            "signals": stats,
        },
        "artifact_index_verified": not any("artifact index" in item for item in problems),
    }
    qualification_document = _load_optional_json(directory / "qualification.json")
    preflight_document = _load_optional_json(directory / "preflight.json")
    attestation_section: dict[str, Any] | None = None
    authenticity: Mapping[str, Any] | None = None
    credibility_policy_value = credibility_policy
    if attestation_path is not None:
        from .attestation import verify_attestation

        verification = verify_attestation(
            directory,
            attestation_path,
            repo_root=repo_root,
            check_git=verify_anchor,
        )
        attestation_section = verification.to_dict()
        authenticity = attestation_section
        credibility_policy_value = replace(
            credibility_policy or CredibilityPolicy(),
            include_authenticity=True,
        )
    assessment = assess_credibility(
        directory,
        manifest=manifest,
        qualification=qualification_document,
        preflight=preflight_document,
        safety=_load_optional_json(directory / "safety.json"),
        current_review=current_review,
        integrity_problems=tuple(problems),
        authenticity=authenticity,
        policy=credibility_policy_value,
    )
    summary["qualification_level"] = (
        qualification_document.get("level")
        if isinstance(qualification_document, Mapping)
        else (manifest.get("qualification") or {}).get("level")
        if isinstance(manifest.get("qualification"), Mapping)
        else None
    )
    summary["credibility"] = assessment.to_dict()
    if attestation_section is not None:
        summary["attestation"] = attestation_section
    status = "ok" if not problems else "invalid"
    return RunInspection(directory, status, tuple(problems), summary)


__all__ = ["RunInspection", "RunInspectionError", "inspect_run"]

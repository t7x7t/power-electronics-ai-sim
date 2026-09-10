"""Evidence-only comparison for deterministic run artifacts.

This is intentionally a small comparison helper, not a claim that a physical
simulation is correct.  Dirty source trees, missing provenance, and unknown
environments produce ``unknown`` rather than a successful comparison.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json
import hashlib
from itertools import combinations
import math
import re
from dataclasses import field
from typing import Any, Mapping, Sequence

from .artifacts import ArtifactWriter, artifact_index, manifest_digest, package_digest


_SOURCE_COMMIT_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$", re.IGNORECASE)
_PUBLISHED_STATUSES = frozenset({"RUN_OK", "QUALIFIED", "COMPARABLE"})


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _run_dir(value: str | Path) -> Path:
    path = Path(value)
    if path.is_file():
        path = path.parent
    if not (path / "manifest.json").exists():
        raise FileNotFoundError(f"run Manifest not found: {path / 'manifest.json'}")
    return path


def _samples(path: Path) -> list[Mapping[str, Any]]:
    value = _load_json(path / "samples.json")
    if not isinstance(value, list) or not all(isinstance(item, Mapping) for item in value):
        raise ValueError("samples.json must contain an array of objects")
    return [dict(item) for item in value]


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def _canonical_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _numeric_differences(left: Sequence[Mapping[str, Any]], right: Sequence[Mapping[str, Any]], tolerance: float) -> tuple[float, list[dict[str, Any]], list[dict[str, Any]]]:
    if len(left) != len(right):
        difference = {"path": "samples.length", "left": len(left), "right": len(right)}
        return math.inf, [difference], [difference]
    maximum = 0.0
    differences: list[dict[str, Any]] = []
    all_differences: list[dict[str, Any]] = []
    for index, (a, b) in enumerate(zip(left, right)):
        if set(a) != set(b):
            difference = {"path": f"samples[{index}].keys", "left": sorted(a), "right": sorted(b)}
            differences.append(difference)
            all_differences.append(difference)
            continue
        for key in sorted(a):
            first, second = a[key], b[key]
            if _number(first) and _number(second):
                delta = abs(float(first) - float(second))
                maximum = max(maximum, delta)
                if delta > 0:
                    difference = {"path": f"samples[{index}].{key}", "left": first, "right": second, "absolute_error": delta}
                    all_differences.append(difference)
                    if delta > tolerance:
                        differences.append(difference)
            elif first != second:
                difference = {"path": f"samples[{index}].{key}", "left": first, "right": second}
                differences.append(difference)
                all_differences.append(difference)
    return maximum, differences, all_differences


def _environment_gate(manifest: Mapping[str, Any]) -> tuple[bool, str | None]:
    status = manifest.get("working_tree_status")
    if status != "clean":
        return False, f"working tree is {status or 'missing'}, not clean"
    source_commit = manifest.get("source_commit")
    if not isinstance(source_commit, str) or not _SOURCE_COMMIT_RE.fullmatch(source_commit):
        return False, "source commit is missing or not a full hexadecimal Git object id"
    environment = manifest.get("environment")
    if not isinstance(environment, Mapping):
        return False, "environment provenance is missing"
    environment_status = environment.get("status")
    if environment_status not in {"known", "partial"}:
        return False, f"environment provenance is {environment_status or 'missing'}"
    provenance = manifest.get("provenance")
    if isinstance(provenance, Mapping) and provenance.get("status") == "unknown":
        return False, "source provenance is unknown"
    python = environment.get("python")
    if not isinstance(python, Mapping) or not python.get("version") or str(python.get("version")).lower() in {"unknown", "unavailable", "not_declared"}:
        return False, "Python environment version is missing"
    # Built-in fixtures deliberately have ``not_declared``/``not_assessed``
    # backend records and remain comparable within this preparatory scope.
    # An explicitly attempted backend, however, must not be treated as
    # reproducible when its identity or numerical settings are incomplete.
    backends = environment.get("backends")
    if isinstance(backends, Sequence) and not isinstance(backends, (str, bytes)):
        for backend in backends:
            if not isinstance(backend, Mapping):
                return False, "backend provenance entry is malformed"
            backend_status = str(backend.get("status", "unknown"))
            if backend_status in {"unknown", "unavailable"}:
                return False, f"backend provenance is {backend_status}"
            if backend_status == "partial":
                return False, "backend provenance is partial; executable identity or solver settings are incomplete"
            if backend_status == "known" and not backend.get("version"):
                return False, "backend version is missing"
    # ``partial`` is accepted for deterministic project fixtures because the
    # built-in models intentionally have no real backend.  The report retains
    # this limitation and never calls it a real-backend reproduction.
    return True, None


def _integrity_gate(directory: Path, manifest: Mapping[str, Any]) -> tuple[bool, str | None, bool]:
    """Validate the immutable evidence required for a published comparison.

    The project originally used tiny hand-written manifests in its
    preparation fixtures.  Those documents intentionally have no runtime
    status, artifact index, or derived hashes.  They remain usable for the
    fixture-only examples, but are never mistaken for a publishable package.
    Any document that declares a runtime status or one of the derived hashes
    opts into the strict package gate.
    """

    has_summary = any(key in manifest for key in ("status", "artifacts", "manifest_sha256", "package_sha256", "hashes"))
    if not has_summary:
        # Only the deliberately tiny, project-owned fixture documents retain
        # the preparation-era compatibility path. Any user/run manifest that
        # omits publication evidence is rejected instead of being treated as
        # a complete package.
        plant = manifest.get("plant")
        controller = manifest.get("controller")
        environment = manifest.get("environment")
        is_fixture = (
            isinstance(plant, Mapping) and plant.get("id") == "fixture"
            and isinstance(controller, Mapping) and controller.get("id") == "fixture"
            and isinstance(environment, Mapping) and environment.get("status") == "partial"
        )
        if is_fixture:
            return True, None, True
        return False, "publication status and package digest summary are missing", False

    status = manifest.get("status")
    if status not in _PUBLISHED_STATUSES:
        return False, f"run status {status or 'missing'} is not publishable", False

    required = set(ArtifactWriter.REQUIRED)
    missing = [name for name in required if not (directory / name).is_file()]
    if not (directory / "manifest.json").is_file():
        missing.append("manifest.json")
    if missing:
        return False, f"required artifacts are missing: {sorted(set(missing))}", False

    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, Mapping):
        return False, "artifact index is missing or malformed", False
    try:
        actual_index = artifact_index(directory)
    except (OSError, ValueError) as exc:
        return False, f"artifact index could not be recomputed: {type(exc).__name__}: {exc}", False
    if set(artifacts) != set(actual_index):
        return False, "artifact index does not cover the published package", False
    for name, expected in actual_index.items():
        declared = artifacts.get(name)
        if not isinstance(declared, Mapping) or declared.get("path") != name or declared.get("sha256") != expected.get("sha256"):
            return False, f"artifact hash mismatch for {name}", False

    manifest_hash = manifest.get("manifest_sha256")
    package_hash = manifest.get("package_sha256")
    if not isinstance(manifest_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", manifest_hash):
        return False, "manifest_sha256 is missing or malformed", False
    if not isinstance(package_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", package_hash):
        return False, "package_sha256 is missing or malformed", False
    hashes = manifest.get("hashes")
    if not isinstance(hashes, Mapping) or hashes.get("manifest_sha256") != manifest_hash or hashes.get("package_sha256") != package_hash:
        return False, "manifest hash summary is missing or inconsistent", False
    try:
        expected_manifest = manifest_digest(manifest)
        expected_package = package_digest(directory, manifest)
    except (OSError, TypeError, ValueError) as exc:
        return False, f"published hash could not be recomputed: {type(exc).__name__}: {exc}", False
    if manifest_hash != expected_manifest:
        return False, "manifest_sha256 does not match manifest contents", False
    if package_hash != expected_package:
        return False, "package_sha256 does not match package contents", False
    state_path = directory / ".run-state.json"
    if not state_path.is_file():
        return False, "published run state marker is missing", False
    try:
        state = _load_json(state_path)
    except (OSError, ValueError, json.JSONDecodeError):
        return False, "published run state marker is unreadable", False
    if not isinstance(state, Mapping) or state.get("status") != "PUBLISHED":
        return False, "run state marker is not PUBLISHED", False
    return True, None, False


def _environment_identity(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Select stable environment fields for a same-environment comparison."""

    environment = manifest.get("environment", {})
    if not isinstance(environment, Mapping):
        return {}
    return {
        "python": environment.get("python"),
        "platform": environment.get("platform"),
        "dependencies": environment.get("dependencies"),
        "backends": environment.get("backends"),
        "execution": environment.get("execution"),
    }


def _manifest_identity(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Select run inputs whose equality is required before sample comparison."""

    return {field: manifest.get(field) for field in (
        "source_commit", "plant", "controller", "contracts", "timebase",
        "initial_state", "random_seed", "action_policy",
    )}


def _sample_digest(samples: Sequence[Mapping[str, Any]]) -> str | None:
    try:
        return _canonical_hash([dict(item) for item in samples])
    except (TypeError, ValueError):
        # Invalid/non-finite JSON is still compared structurally below, but
        # it cannot receive a canonical exact hash.
        return None


@dataclass(frozen=True)
class ReproducibilityReport:
    """Serializable result with exact, tolerance, mismatch, or unknown outcome."""

    outcome: str
    left_run: str
    right_run: str
    exact: bool
    within_tolerance: bool
    maximum_absolute_error: float | None
    differences: tuple[Mapping[str, Any], ...]
    limitations: tuple[str, ...] = ()
    identity_hash_left: str | None = None
    identity_hash_right: str | None = None
    identity_equal: bool | None = None
    environment_equal: bool | None = None
    sample_hash_left: str | None = None
    sample_hash_right: str | None = None
    sample_hash_equal: bool | None = None
    difference_summary: Mapping[str, int] = field(default_factory=dict)
    scope: str = "same_machine"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "1.0",
            "outcome": self.outcome,
            "left_run": self.left_run,
            "right_run": self.right_run,
            "exact": self.exact,
            "within_tolerance": self.within_tolerance,
            "maximum_absolute_error": self.maximum_absolute_error,
            "differences": [dict(item) for item in self.differences],
            "limitations": list(self.limitations),
            "identity": {
                "left_hash": self.identity_hash_left,
                "right_hash": self.identity_hash_right,
                "equal": self.identity_equal,
            },
            "environment": {"equal": self.environment_equal},
            "hashes": {
                "identity_left": self.identity_hash_left,
                "identity_right": self.identity_hash_right,
                "samples_left": self.sample_hash_left,
                "samples_right": self.sample_hash_right,
                "samples_equal": self.sample_hash_equal,
            },
            "difference_summary": dict(self.difference_summary),
            "scope": self.scope,
        }


def compare_runs(left: str | Path, right: str | Path, *, tolerance: float = 1e-9) -> ReproducibilityReport:
    """Compare two published runs without silently accepting weak provenance."""

    if not math.isfinite(float(tolerance)) or float(tolerance) < 0:
        raise ValueError("tolerance must be finite and non-negative")
    left_dir, right_dir = _run_dir(left), _run_dir(right)
    left_manifest, right_manifest = _load_json(left_dir / "manifest.json"), _load_json(right_dir / "manifest.json")
    left_name, right_name = left_dir.name, right_dir.name
    left_identity_hash = _canonical_hash(_manifest_identity(left_manifest)) if isinstance(left_manifest, Mapping) else None
    right_identity_hash = _canonical_hash(_manifest_identity(right_manifest)) if isinstance(right_manifest, Mapping) else None
    identity_equal = None if left_identity_hash is None or right_identity_hash is None else left_identity_hash == right_identity_hash
    limitations: list[str] = []
    fixture_only: list[str] = []
    for label, directory, manifest in (("left", left_dir, left_manifest), ("right", right_dir, right_manifest)):
        if not isinstance(manifest, Mapping):
            return ReproducibilityReport(
                "unknown", left_name, right_name, False, False, None,
                ({"path": f"{label}.manifest", "reason": "not an object", "attribution": "structure"},),
                identity_hash_left=left_identity_hash, identity_hash_right=right_identity_hash,
                identity_equal=identity_equal, difference_summary={"structure": 1},
            )
        integrity_ok, integrity_reason, preparatory_fixture = _integrity_gate(directory, manifest)
        if not integrity_ok:
            return ReproducibilityReport(
                "unknown", left_name, right_name, False, False, None,
                ({"path": f"{label}.integrity", "reason": integrity_reason, "attribution": "integrity"},),
                identity_hash_left=left_identity_hash, identity_hash_right=right_identity_hash,
                identity_equal=identity_equal, environment_equal=None,
                difference_summary={"integrity": 1},
            )
        if preparatory_fixture:
            fixture_only.append(label)
        ok, reason = _environment_gate(manifest)
        if not ok:
            return ReproducibilityReport(
                "unknown", left_name, right_name, False, False, None,
                ({"path": f"{label}.provenance", "reason": reason, "attribution": "environment"},),
                identity_hash_left=left_identity_hash, identity_hash_right=right_identity_hash,
                identity_equal=identity_equal, environment_equal=None,
                difference_summary={"environment": 1},
            )
        if manifest.get("environment", {}).get("status") == "partial":
            limitations.append(f"{label} environment is partial; no real backend reproducibility is claimed")
    if fixture_only:
        limitations.append(
            "preparatory fixture manifest has no publication status or package digests; result is not release-grade reproducibility evidence"
        )
    left_environment, right_environment = _environment_identity(left_manifest), _environment_identity(right_manifest)
    environment_equal = left_environment == right_environment
    if not environment_equal:
        difference = {"path": "environment", "left": left_environment, "right": right_environment, "attribution": "environment"}
        return ReproducibilityReport(
            "unknown", left_name, right_name, False, False, None, (difference,),
            tuple(limitations) + ("environment identities differ; cross-environment equivalence is not established",),
            identity_hash_left=left_identity_hash, identity_hash_right=right_identity_hash,
            identity_equal=identity_equal, environment_equal=False,
            difference_summary={"environment": 1},
        )
    identity_differences: list[dict[str, Any]] = []
    for field_name, left_value in _manifest_identity(left_manifest).items():
        right_value = _manifest_identity(right_manifest).get(field_name)
        if left_value != right_value:
            identity_differences.append({"path": field_name, "left": left_value, "right": right_value, "attribution": "identity"})
    if identity_differences:
        return ReproducibilityReport(
            "mismatch", left_name, right_name, False, False, None, tuple(identity_differences), tuple(limitations),
            identity_hash_left=left_identity_hash, identity_hash_right=right_identity_hash,
            identity_equal=False, environment_equal=True,
            difference_summary={"identity": len(identity_differences)},
        )
    left_samples, right_samples = _samples(left_dir), _samples(right_dir)
    left_sample_hash, right_sample_hash = _sample_digest(left_samples), _sample_digest(right_samples)
    maximum, sample_diffs, all_sample_diffs = _numeric_differences(left_samples, right_samples, float(tolerance))
    numeric_diffs = [item for item in all_sample_diffs if "absolute_error" in item]
    numeric_exceeded = [item for item in numeric_diffs if float(item["absolute_error"]) > tolerance]
    structural_diffs = [item for item in all_sample_diffs if "absolute_error" not in item]
    summary = {
        "identity": 0,
        "environment": 0,
        "numeric": len(numeric_diffs),
        "numeric_within_tolerance": len(numeric_diffs) - len(numeric_exceeded),
        "numeric_exceeded_tolerance": len(numeric_exceeded),
        "structural": len(structural_diffs),
        "total": len(all_sample_diffs),
    }
    common = {
        "identity_hash_left": left_identity_hash,
        "identity_hash_right": right_identity_hash,
        "identity_equal": True,
        "environment_equal": True,
        "sample_hash_left": left_sample_hash,
        "sample_hash_right": right_sample_hash,
        "sample_hash_equal": left_sample_hash is not None and left_sample_hash == right_sample_hash,
        "difference_summary": summary,
    }
    if not all_sample_diffs:
        if common["sample_hash_equal"]:
            return ReproducibilityReport("exact_match", left_name, right_name, True, True, maximum, (), tuple(limitations), **common)
        # Numeric values compare equal, but canonical bytes differ (for
        # example ``1`` versus ``1.0``).  Keep exact-hash and value/tolerance
        # semantics distinct so a consumer can see the representation drift.
        summary["hash_only"] = 1
        hash_difference = {
            "path": "samples.hash",
            "left": left_sample_hash,
            "right": right_sample_hash,
            "attribution": "numeric_representation",
        }
        return ReproducibilityReport("tolerance_match", left_name, right_name, False, True, maximum, (hash_difference,), tuple(limitations), **common)
    only_numeric = not structural_diffs
    if only_numeric and maximum <= tolerance:
        attributed = tuple({**item, "attribution": "numeric_within_tolerance"} for item in all_sample_diffs)
        return ReproducibilityReport("tolerance_match", left_name, right_name, False, True, maximum, attributed, tuple(limitations), **common)
    attributed = tuple({**item, "attribution": "numeric_exceeded_tolerance" if "absolute_error" in item and float(item["absolute_error"]) > tolerance else "structural"} for item in sample_diffs)
    return ReproducibilityReport("mismatch", left_name, right_name, False, False, maximum, attributed, tuple(limitations), **common)


@dataclass(frozen=True)
class RepetitionAudit:
    """Aggregate evidence for two or more repeated runs on one machine."""

    outcome: str
    runs: tuple[str, ...]
    pairwise: tuple[Mapping[str, Any], ...]
    exact_pairs: int
    tolerance_pairs: int
    mismatch_pairs: int
    unknown_pairs: int
    limitations: tuple[str, ...] = ()

    @property
    def reproducible(self) -> bool:
        return self.outcome in {"exact_match", "tolerance_match"}

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "1.0",
            "scope": "same_machine",
            "outcome": self.outcome,
            "reproducible": self.reproducible,
            "runs": list(self.runs),
            "pairwise": [dict(item) for item in self.pairwise],
            "counts": {
                "exact_match": self.exact_pairs,
                "tolerance_match": self.tolerance_pairs,
                "mismatch": self.mismatch_pairs,
                "unknown": self.unknown_pairs,
            },
            "limitations": list(self.limitations),
        }


def audit_repeated_runs(runs: Sequence[str | Path], *, tolerance: float = 1e-9) -> RepetitionAudit:
    """Compare every pair in a repeated-run set and aggregate conservatively."""

    paths = tuple(_run_dir(item) for item in runs)
    if len(paths) < 2:
        raise ValueError("at least two runs are required for a repetition audit")
    reports: list[Mapping[str, Any]] = []
    counts = {"exact_match": 0, "tolerance_match": 0, "mismatch": 0, "unknown": 0}
    for left, right in combinations(paths, 2):
        report = compare_runs(left, right, tolerance=tolerance)
        counts[report.outcome] = counts.get(report.outcome, 0) + 1
        reports.append(report.to_dict())
    if counts["unknown"]:
        outcome = "unknown"
    elif counts["mismatch"]:
        outcome = "mismatch"
    elif counts["tolerance_match"]:
        outcome = "tolerance_match"
    else:
        outcome = "exact_match"
    limitations = ("same-machine scope is inferred from equal manifest environment identities; independent host attestation is not provided",)
    return RepetitionAudit(outcome, tuple(path.name for path in paths), tuple(reports), counts["exact_match"], counts["tolerance_match"], counts["mismatch"], counts["unknown"], limitations)


def cross_machine_evidence(runs: Sequence[str | Path]) -> dict[str, Any]:
    """Build a portable cross-machine evidence matrix without claiming success.

    The function records machine/environment identities and package hashes. It
    reports ``ready_for_trial`` only when at least two distinct platform
    identities are present and all manifests pass the provenance gate. Numeric
    equivalence still requires an adapter-reviewed trial and tolerance policy.
    """

    if not runs:
        raise ValueError("at least one run is required")
    records: list[dict[str, Any]] = []
    limitations: list[str] = []
    blocking_limitations: list[str] = []
    source_commits: set[str] = set()
    for value in runs:
        directory = _run_dir(value)
        manifest = _load_json(directory / "manifest.json")
        if not isinstance(manifest, Mapping):
            records.append({"run": directory.name, "status": "invalid_manifest"})
            message = f"{directory.name}: manifest is not an object"
            limitations.append(message)
            blocking_limitations.append(message)
            continue
        environment = manifest.get("environment") if isinstance(manifest.get("environment"), Mapping) else {}
        platform_identity = environment.get("platform") if isinstance(environment.get("platform"), Mapping) else None
        records.append({
            "run": directory.name,
            "source_commit": manifest.get("source_commit"),
            "manifest_sha256": manifest.get("manifest_sha256"),
            "package_sha256": manifest.get("package_sha256"),
            "environment_status": environment.get("status"),
            "platform": platform_identity,
            "environment_identity_sha256": _canonical_hash(_environment_identity(manifest)),
        })
        source_commit = manifest.get("source_commit")
        if isinstance(source_commit, str) and _SOURCE_COMMIT_RE.fullmatch(source_commit):
            source_commits.add(source_commit)
        integrity_ok, integrity_reason, preparatory_fixture = _integrity_gate(directory, manifest)
        if not integrity_ok:
            message = f"{directory.name}: {integrity_reason}"
            limitations.append(message)
            blocking_limitations.append(message)
        elif preparatory_fixture:
            limitations.append(
                f"{directory.name}: preparatory fixture has no publication status or package digests; evidence is not release-grade"
            )
        ok, reason = _environment_gate(manifest)
        if not ok:
            message = f"{directory.name}: {reason}"
            limitations.append(message)
            blocking_limitations.append(message)
    if len(source_commits) > 1:
        limitations.append("source commits differ across runs; cross-machine trial inputs are not aligned")
    identities = {_canonical_hash(item.get("platform")) for item in records if item.get("platform") is not None}
    if len(source_commits) > 1:
        status = "not_assessed"
    elif len(identities) < 2:
        status = "not_assessed"
        limitations.append("fewer than two distinct platform identities were provided")
    elif blocking_limitations:
        status = "not_assessed"
    else:
        status = "ready_for_trial"
    limitations.append("cross-machine numeric equivalence is not inferred; run an adapter-reviewed trial with declared tolerances")
    return {"schema_version": "1.0", "scope": "cross_machine", "status": status, "runs": records, "limitations": sorted(set(limitations))}


# Descriptive aliases keep the API discoverable for callers that prefer a
# verb-first name while preserving the concise public function above.
audit_same_machine_runs = audit_repeated_runs
build_cross_machine_evidence = cross_machine_evidence

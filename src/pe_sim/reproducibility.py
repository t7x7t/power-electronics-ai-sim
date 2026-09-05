"""Evidence-only comparison for deterministic run artifacts.

This is intentionally a small comparison helper, not a claim that a physical
simulation is correct.  Dirty source trees, missing provenance, and unknown
environments produce ``unknown`` rather than a successful comparison.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json
import math
from typing import Any, Mapping, Sequence


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
    if not isinstance(source_commit, str) or source_commit in {"", "unknown", "unavailable"}:
        return False, "source commit is missing or unavailable"
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
    # ``partial`` is accepted for deterministic project fixtures because the
    # built-in models intentionally have no real backend.  The report retains
    # this limitation and never calls it a real-backend reproduction.
    return True, None


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
    }


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
        }


def compare_runs(left: str | Path, right: str | Path, *, tolerance: float = 1e-9) -> ReproducibilityReport:
    """Compare two published runs without silently accepting weak provenance."""

    if not math.isfinite(float(tolerance)) or float(tolerance) < 0:
        raise ValueError("tolerance must be finite and non-negative")
    left_dir, right_dir = _run_dir(left), _run_dir(right)
    left_manifest, right_manifest = _load_json(left_dir / "manifest.json"), _load_json(right_dir / "manifest.json")
    left_name, right_name = left_dir.name, right_dir.name
    limitations: list[str] = []
    for label, manifest in (("left", left_manifest), ("right", right_manifest)):
        if not isinstance(manifest, Mapping):
            return ReproducibilityReport("unknown", left_name, right_name, False, False, None, ({"path": f"{label}.manifest", "reason": "not an object"},))
        ok, reason = _environment_gate(manifest)
        if not ok:
            return ReproducibilityReport("unknown", left_name, right_name, False, False, None, ({"path": f"{label}.provenance", "reason": reason},))
        if manifest.get("environment", {}).get("status") == "partial":
            limitations.append(f"{label} environment is partial; no real backend reproducibility is claimed")
    environment_differences: list[dict[str, Any]] = []
    left_environment, right_environment = _environment_identity(left_manifest), _environment_identity(right_manifest)
    if left_environment != right_environment:
        environment_differences.append({"path": "environment", "left": left_environment, "right": right_environment})
    if environment_differences:
        return ReproducibilityReport("unknown", left_name, right_name, False, False, None, tuple(environment_differences), tuple(limitations) + ("environment identities differ; cross-environment equivalence is not established",))
    identity_fields = ("source_commit", "plant", "controller", "contracts", "timebase", "initial_state", "random_seed")
    differences: list[dict[str, Any]] = []
    for field in identity_fields:
        if left_manifest.get(field) != right_manifest.get(field):
            differences.append({"path": field, "left": left_manifest.get(field), "right": right_manifest.get(field)})
    if differences:
        return ReproducibilityReport("mismatch", left_name, right_name, False, False, None, tuple(differences), tuple(limitations))
    left_samples, right_samples = _samples(left_dir), _samples(right_dir)
    maximum, sample_diffs, all_sample_diffs = _numeric_differences(left_samples, right_samples, float(tolerance))
    if not all_sample_diffs:
        return ReproducibilityReport("exact_match", left_name, right_name, True, True, maximum, (), tuple(limitations))
    only_numeric = all("absolute_error" in item for item in all_sample_diffs)
    if only_numeric and maximum <= tolerance:
        return ReproducibilityReport("tolerance_match", left_name, right_name, False, True, maximum, tuple(all_sample_diffs), tuple(limitations))
    return ReproducibilityReport("mismatch", left_name, right_name, False, False, maximum, tuple(sample_diffs), tuple(limitations))

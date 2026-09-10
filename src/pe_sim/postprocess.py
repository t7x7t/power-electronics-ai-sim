"""Restricted Stage 10 metrics and export helpers.

The post-processing slice deliberately consumes evidence packages rather than
running a Plant or inferring physical conclusions.  It is limited to the
project-owned Fake/FakeLoad/Buck/Boost reference Plant families and accepts
only a published, qualified, hash-complete run with clean source evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
import csv
import hashlib
import json
import math
from pathlib import Path
import re
from statistics import fmean
from typing import Any, Callable, Iterable, Mapping, Sequence

from .artifacts import ArtifactWriter, artifact_index, canonical_json, manifest_digest, package_digest


_SOURCE_COMMIT_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$", re.IGNORECASE)
_PUBLISHED_MARKER = "PUBLISHED"
_ALLOWED_PLANT_CLASSES = frozenset({"FakePlant", "FakeLoadPlant", "BuckPlant", "BoostPlant"})
_ALLOWED_BACKEND_STATUSES = frozenset({"known", "not_declared", "not_assessed"})
_METRIC_SCHEMA = "stage10-metrics-v1"
_COMPARISON_SCHEMA = "stage10-metric-comparison-v1"
_REPORT_SCHEMA = "stage10-evidence-report-v1"
_SAMPLE_DATA_SCHEMA = "samples-v1"
_RUN_METRICS_SCHEMA = "run-metrics-v1"
_REGISTRY_SCHEMA = "stage10-metric-registry-v1"


class PostprocessEligibilityError(ValueError):
    """Raised when a run package cannot be consumed by the Stage 10 slice."""

    def __init__(self, reasons: Sequence[str]):
        self.reasons = tuple(str(reason) for reason in reasons)
        super().__init__("post-processing input rejected: " + "; ".join(self.reasons))


class UnknownMetricError(PostprocessEligibilityError):
    """Raised when a caller asks for a metric absent from the registry."""


@dataclass(frozen=True)
class MetricDefinition:
    """Versioned numeric metric contract for restricted post-processing."""

    metric_id: str
    version: str
    unit: str
    input_fields: tuple[str, ...]
    calculator: Callable[["RunEvidence"], int | float]

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", self.metric_id):
            raise ValueError("metric_id must contain only letters, numbers, '.', '_' or '-'")
        if not self.version or not self.unit:
            raise ValueError("metric version and unit are required")
        if not callable(self.calculator):
            raise TypeError("metric calculator must be callable")
        object.__setattr__(self, "input_fields", tuple(str(item) for item in self.input_fields))
        if any(not item for item in self.input_fields):
            raise ValueError("metric input_fields must contain non-empty names")

    def compute(self, evidence: "RunEvidence") -> int | float:
        for field in self.input_fields:
            _series(evidence, field)
        value = self.calculator(evidence)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            raise PostprocessEligibilityError((f"metric {self.metric_id} produced a non-finite or non-numeric value",))
        return value

    def contract(self) -> dict[str, Any]:
        """Return the serializable part of the metric contract.

        The calculator implementation is deliberately excluded.  Its
        behavior is governed by the reviewed metric version; the contract is
        intended to identify the data/units/semantics that a report used.
        """

        return {
            "metric_id": self.metric_id,
            "version": self.version,
            "unit": self.unit,
            "input_fields": list(self.input_fields),
        }


class MetricRegistry:
    """Small explicit registry that rejects unreviewed metric identifiers."""

    def __init__(
        self,
        definitions: Iterable[MetricDefinition] = (),
        *,
        registry_id: str = "stage10-default",
        version: str = "1",
    ) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", str(registry_id)):
            raise ValueError("registry_id must contain only letters, numbers, '.', '_' or '-'")
        if not str(version):
            raise ValueError("registry version is required")
        self.registry_id = str(registry_id)
        self.version = str(version)
        self._definitions: dict[str, MetricDefinition] = {}
        for definition in definitions:
            self.register(definition)

    def register(self, definition: MetricDefinition, *, replace: bool = False) -> None:
        if definition.metric_id in self._definitions and not replace:
            raise ValueError(f"metric is already registered: {definition.metric_id}")
        self._definitions[definition.metric_id] = definition

    def resolve(self, metric_id: str) -> MetricDefinition:
        try:
            return self._definitions[str(metric_id)]
        except KeyError as exc:
            raise UnknownMetricError((f"unknown metric_id: {metric_id}",)) from exc

    @property
    def metric_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._definitions))

    @property
    def fingerprint(self) -> str:
        """Stable identity of the reviewed metric contract set."""

        payload = {
            "schema": _REGISTRY_SCHEMA,
            "registry_id": self.registry_id,
            "version": self.version,
            "metrics": [self._definitions[item].contract() for item in self.metric_ids],
        }
        return hashlib.sha256(canonical_json(payload)).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": _REGISTRY_SCHEMA,
            "registry_id": self.registry_id,
            "version": self.version,
            "fingerprint": self.fingerprint,
            "metric_ids": list(self.metric_ids),
        }

    def compute(self, evidence: "RunEvidence", metric_ids: Sequence[str] | None = None) -> dict[str, dict[str, Any]]:
        selected = tuple(metric_ids) if metric_ids is not None else self.metric_ids
        if not selected:
            raise ValueError("at least one metric_id must be selected")
        if len(set(selected)) != len(selected):
            raise ValueError("metric_ids must be unique")
        result: dict[str, dict[str, Any]] = {}
        for metric_id in selected:
            definition = self.resolve(metric_id)
            result[definition.metric_id] = {
                "metric_id": definition.metric_id,
                "version": definition.version,
                "unit": definition.unit,
                "input_fields": list(definition.input_fields),
                "value": definition.compute(evidence),
            }
        return result


def _series(evidence: "RunEvidence", field: str) -> list[float]:
    values: list[float] = []
    for index, row in enumerate(evidence.samples):
        if field not in row or not _finite(row[field]):
            raise PostprocessEligibilityError((f"metric input field {field!r} is missing or invalid at sample {index}",))
        values.append(float(row[field]))
    if not values:
        raise PostprocessEligibilityError((f"metric input field {field!r} has no samples",))
    return values


def _time_span(evidence: "RunEvidence") -> float:
    values = _series(evidence, "time_s")
    if any(right < left for left, right in zip(values, values[1:])):
        raise PostprocessEligibilityError(("metric input time_s is not monotonic",))
    return values[-1] - values[0]


def _mean(field: str) -> Callable[["RunEvidence"], float]:
    return lambda evidence: fmean(_series(evidence, field))


def _last(field: str) -> Callable[["RunEvidence"], float]:
    return lambda evidence: _series(evidence, field)[-1]


def default_metric_registry() -> MetricRegistry:
    """Return the reviewed metrics available to the Stage 10 slice."""

    return MetricRegistry(
        (
            MetricDefinition("samples.count", "1", "count", (), lambda evidence: len(evidence.samples)),
            MetricDefinition("audit.calls", "1", "count", (), lambda evidence: evidence.audit_count),
            MetricDefinition("time.duration", "1", "s", ("time_s",), _time_span),
            MetricDefinition("series.vout.mean", "1", "V", ("vout",), _mean("vout")),
            MetricDefinition("series.vout.final", "1", "V", ("vout",), _last("vout")),
            MetricDefinition("series.action.mean", "1", "normalized", ("action",), _mean("action")),
            MetricDefinition("series.action.final", "1", "normalized", ("action",), _last("action")),
        )
    )


@dataclass(frozen=True)
class RunEvidence:
    """Immutable evidence loaded after the strict package gate."""

    run_dir: Path
    manifest: Mapping[str, Any]
    samples: tuple[Mapping[str, Any], ...]
    audit_count: int
    sample_schema_version: str = _SAMPLE_DATA_SCHEMA
    metrics_schema_version: str = _RUN_METRICS_SCHEMA
    sample_fields: tuple[str, ...] = ()


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise PostprocessEligibilityError((f"unreadable JSON artifact: {path.name}",)) from exc


def _as_run_dir(value: str | Path) -> Path:
    path = Path(value)
    if path.is_file():
        path = path.parent
    return path


def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def _plant_class(plant: Mapping[str, Any]) -> str | None:
    """Read the class from either fixture or L1 reference identity shape."""

    identity = plant.get("identity")
    if not isinstance(identity, Mapping):
        return None
    direct = identity.get("class")
    if isinstance(direct, str):
        return direct
    implementation = identity.get("implementation")
    nested = implementation.get("class") if isinstance(implementation, Mapping) else None
    return nested if isinstance(nested, str) else None


def _check_package(run_dir: Path, manifest: Mapping[str, Any]) -> list[str]:
    """Recompute all immutable package evidence before reading samples."""

    reasons: list[str] = []
    if manifest.get("status") != "QUALIFIED":
        reasons.append(f"run status is {manifest.get('status') or 'missing'}, expected QUALIFIED")
    if manifest.get("working_tree_status") != "clean":
        reasons.append(f"working tree is {manifest.get('working_tree_status') or 'missing'}, not clean")
    source_commit = manifest.get("source_commit")
    if not isinstance(source_commit, str) or not _SOURCE_COMMIT_RE.fullmatch(source_commit):
        reasons.append("source commit is missing or not a full hexadecimal Git object id")
    provenance = manifest.get("provenance")
    if not isinstance(provenance, Mapping) or provenance.get("status") != "known":
        reasons.append("source provenance is missing or not known")

    environment = manifest.get("environment")
    if not isinstance(environment, Mapping):
        reasons.append("environment provenance is missing")
    else:
        if environment.get("status") not in {"known", "partial"}:
            reasons.append(f"environment provenance is {environment.get('status') or 'missing'}")
        python = environment.get("python")
        if not isinstance(python, Mapping) or not python.get("version") or str(python.get("version")).lower() in {"unknown", "unavailable", "not_declared"}:
            reasons.append("Python environment version is missing")
        backends = environment.get("backends")
        if not isinstance(backends, Sequence) or isinstance(backends, (str, bytes)) or not backends:
            reasons.append("backend provenance is missing")
        else:
            for index, backend in enumerate(backends):
                if not isinstance(backend, Mapping):
                    reasons.append(f"backend provenance entry {index} is malformed")
                    continue
                status = str(backend.get("status", "unknown"))
                if status not in _ALLOWED_BACKEND_STATUSES:
                    reasons.append(f"backend provenance entry {index} is {status}")
                elif status == "known" and (not backend.get("version") or backend.get("solver_settings") is None):
                    reasons.append(f"backend provenance entry {index} is incomplete")

    plant = manifest.get("plant")
    plant_class = _plant_class(plant) if isinstance(plant, Mapping) else None
    plant_id = plant.get("id") if isinstance(plant, Mapping) else None
    if not isinstance(plant, Mapping) or not isinstance(plant.get("hash"), str) or len(plant.get("hash", "")) != 64:
        reasons.append("Plant identity/hash is missing")
    if plant_class not in _ALLOWED_PLANT_CLASSES:
        reasons.append(f"Plant family {plant_class or plant_id or 'missing'} is outside the restricted Stage 10 slice")
    controller = manifest.get("controller")
    if not isinstance(controller, Mapping) or not isinstance(controller.get("hash"), str) or len(controller.get("hash", "")) != 64:
        reasons.append("Controller identity/hash is missing")

    qualification = manifest.get("qualification")
    if not isinstance(qualification, Mapping) or qualification.get("passed") is not True:
        reasons.append("qualification evidence is not passed")
    safety = manifest.get("safety")
    if not isinstance(safety, Mapping) or safety.get("passed") is not True:
        reasons.append("safety evidence is not passed")

    state_path = run_dir / ".run-state.json"
    if not state_path.is_file():
        reasons.append("published run-state marker is missing")
    else:
        try:
            state = _load_json(state_path)
        except PostprocessEligibilityError as exc:
            reasons.extend(exc.reasons)
        else:
            if not isinstance(state, Mapping) or state.get("status") != _PUBLISHED_MARKER:
                reasons.append("run-state marker is not PUBLISHED")

    required = set(ArtifactWriter.REQUIRED) | {"audit.json", "state_transitions.json"}
    missing = sorted(name for name in required if not (run_dir / name).is_file())
    if missing:
        reasons.append(f"required artifacts are missing: {missing}")
    declared_artifacts = manifest.get("artifacts")
    if not isinstance(declared_artifacts, Mapping):
        reasons.append("artifact index is missing or malformed")
    else:
        try:
            actual_artifacts = artifact_index(run_dir)
        except (OSError, ValueError) as exc:
            reasons.append(f"artifact index could not be recomputed: {type(exc).__name__}: {exc}")
        else:
            if dict(declared_artifacts) != actual_artifacts:
                reasons.append("artifact index is incomplete or inconsistent")

    manifest_hash = manifest.get("manifest_sha256")
    package_hash = manifest.get("package_sha256")
    hashes = manifest.get("hashes")
    if not isinstance(manifest_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", manifest_hash):
        reasons.append("manifest_sha256 is missing or malformed")
    if not isinstance(package_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", package_hash):
        reasons.append("package_sha256 is missing or malformed")
    if not isinstance(hashes, Mapping) or hashes.get("manifest_sha256") != manifest_hash or hashes.get("package_sha256") != package_hash:
        reasons.append("manifest hash summary is missing or inconsistent")
    if isinstance(manifest_hash, str) and len(manifest_hash) == 64:
        try:
            if manifest_digest(manifest) != manifest_hash:
                reasons.append("manifest_sha256 does not match manifest contents")
        except (TypeError, ValueError):
            reasons.append("manifest cannot be canonically hashed")
    if isinstance(package_hash, str) and len(package_hash) == 64:
        try:
            if package_digest(run_dir, manifest) != package_hash:
                reasons.append("package_sha256 does not match package contents")
        except (OSError, TypeError, ValueError):
            reasons.append("package cannot be canonically hashed")
    return reasons


def load_run_evidence(run_dir: str | Path) -> RunEvidence:
    """Load and validate one package for restricted post-processing."""

    directory = _as_run_dir(run_dir)
    manifest_path = directory / "manifest.json"
    if not manifest_path.is_file():
        raise PostprocessEligibilityError(("manifest.json is missing",))
    manifest = _load_json(manifest_path)
    if not isinstance(manifest, Mapping):
        raise PostprocessEligibilityError(("manifest.json must contain an object",))
    reasons = _check_package(directory, manifest)
    samples_path = directory / "samples.json"
    if not samples_path.is_file():
        reasons.append("samples.json is missing")
        samples: list[Mapping[str, Any]] = []
    else:
        loaded = _load_json(samples_path)
        if not isinstance(loaded, list) or not all(isinstance(row, Mapping) for row in loaded):
            reasons.append("samples.json must contain an array of objects")
            samples = []
        else:
            samples = [dict(row) for row in loaded]
    if not samples:
        reasons.append("samples.json contains no samples")
    for index, row in enumerate(samples):
        for key, value in row.items():
            if isinstance(value, (int, float)) and not _finite(value):
                reasons.append(f"sample {index} field {key} is non-finite")
    audit_count = 0
    audit_path = directory / "audit.json"
    if audit_path.is_file():
        audit = _load_json(audit_path)
        if not isinstance(audit, Mapping) or not isinstance(audit.get("calls"), list):
            reasons.append("audit.json call list is missing or malformed")
        else:
            audit_count = len(audit["calls"])
    if reasons:
        raise PostprocessEligibilityError(tuple(dict.fromkeys(reasons)))
    metrics = _load_json(directory / "metrics.json")
    if not isinstance(metrics, Mapping):
        raise PostprocessEligibilityError(("metrics.json must contain an object",))
    # Older v1 runtime packages did not carry these fields.  They remain
    # readable as an explicitly identified legacy input, while new packages
    # can be required to declare the versions at publication time.
    sample_schema_version = str(metrics.get("sample_schema_version", _SAMPLE_DATA_SCHEMA))
    metrics_schema_version = str(metrics.get("schema_version", _RUN_METRICS_SCHEMA))
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", sample_schema_version):
        raise PostprocessEligibilityError(("metrics.json sample_schema_version is malformed",))
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", metrics_schema_version):
        raise PostprocessEligibilityError(("metrics.json schema_version is malformed",))
    expected_count = metrics.get("sample_count")
    if expected_count != len(samples):
        raise PostprocessEligibilityError((f"metrics.json sample_count {expected_count!r} does not match samples.json ({len(samples)})",))
    declared_audit_count = metrics.get("audit_call_count", metrics.get("audit_count"))
    if declared_audit_count is not None and declared_audit_count != audit_count:
        raise PostprocessEligibilityError((f"metrics.json audit count {declared_audit_count!r} does not match audit.json ({audit_count})",))
    sample_fields = tuple(sorted({str(key) for row in samples for key in row}))
    return RunEvidence(
        directory,
        dict(manifest),
        tuple(samples),
        audit_count,
        sample_schema_version,
        metrics_schema_version,
        sample_fields,
    )


def _series_summary(values: Sequence[float], times: Sequence[float] | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {
        "count": len(values),
        "min": min(values),
        "max": max(values),
        "mean": fmean(values),
        "first": values[0],
        "last": values[-1],
    }
    if times is not None and len(values) > 1:
        result["trapezoid_integral"] = sum((times[i] - times[i - 1]) * (values[i] + values[i - 1]) / 2.0 for i in range(1, len(values)))
    return result


def summarize_run(
    run_dir: str | Path,
    *,
    registry: MetricRegistry | None = None,
    metric_ids: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Return deterministic, evidence-linked summaries for one qualified run."""

    evidence = load_run_evidence(run_dir)
    manifest = evidence.manifest
    rows = evidence.samples
    times: list[float] | None = None
    if all(_finite(row.get("time_s")) for row in rows):
        times = [float(row["time_s"]) for row in rows]
        if any(right < left for left, right in zip(times, times[1:])):
            raise PostprocessEligibilityError(("sample time_s values are not monotonic",))
    numeric_keys = sorted({str(key) for row in rows for key, value in row.items() if _finite(value)})
    series: dict[str, Any] = {}
    for key in numeric_keys:
        values = [float(row[key]) for row in rows if _finite(row.get(key))]
        if len(values) != len(rows):
            continue
        series[key] = _series_summary(values, times if key != "time_s" else None)
    metric_registry = registry or default_metric_registry()
    metrics = metric_registry.compute(evidence, metric_ids)
    schema_descriptor = {
        "schema": "stage10-data-descriptor-v1",
        "sample_schema_version": evidence.sample_schema_version,
        "metrics_schema_version": evidence.metrics_schema_version,
        "sample_fields": list(evidence.sample_fields),
    }
    schema_fingerprint = hashlib.sha256(canonical_json(schema_descriptor)).hexdigest()
    data_descriptor = {
        **schema_descriptor,
        "sample_count": len(rows),
        "schema_fingerprint": schema_fingerprint,
        # The package digest is the immutable identity of the actual values;
        # this derived fingerprint makes the relationship explicit without
        # duplicating the sample payload in the report.
        "dataset_fingerprint": hashlib.sha256(
            canonical_json(
                {
                    "schema_fingerprint": schema_fingerprint,
                    "manifest_sha256": manifest.get("manifest_sha256"),
                    "package_sha256": manifest.get("package_sha256"),
                }
            )
        ).hexdigest(),
    }
    # Retain the short name as a compatibility alias for early v1 consumers.
    data_descriptor["fingerprint"] = data_descriptor["dataset_fingerprint"]
    return {
        "schema": _METRIC_SCHEMA,
        "provenance": {
            "run_id": manifest.get("run_id"),
            "manifest_sha256": manifest.get("manifest_sha256"),
            "package_sha256": manifest.get("package_sha256"),
            "working_tree_status": manifest.get("working_tree_status"),
            "source_provenance_status": (manifest.get("provenance") or {}).get("status") if isinstance(manifest.get("provenance"), Mapping) else None,
            "environment_status": (manifest.get("environment") or {}).get("status") if isinstance(manifest.get("environment"), Mapping) else None,
        },
        "data": data_descriptor,
        "metric_registry": metric_registry.to_dict(),
        "plant": {"id": manifest["plant"]["id"], "class": _plant_class(manifest["plant"])},
        "status": manifest["status"],
        "sample_count": len(rows),
        "audit_count": evidence.audit_count,
        "time": {
            "first_s": times[0] if times else None,
            "last_s": times[-1] if times else None,
            "duration_s": (times[-1] - times[0]) if times else None,
        },
        "metrics": metrics,
        "series": series,
    }


def _assert_outside_run(run_dir: Path, destination: Path) -> None:
    try:
        destination.resolve().relative_to(run_dir.resolve())
    except ValueError:
        return
    raise ValueError("post-processing output must be outside the immutable run package")


def export_metrics(run_dir: str | Path, destination: str | Path, *, format: str | None = None) -> Path:
    """Export a linked JSON summary or CSV time series without mutating a run."""

    source = _as_run_dir(run_dir).resolve()
    target = Path(destination)
    _assert_outside_run(source, target)
    summary = summarize_run(source)
    output_format = (format or target.suffix.lstrip(".") or "json").lower()
    target.parent.mkdir(parents=True, exist_ok=True)
    if output_format == "json":
        target.write_text(json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    elif output_format == "csv":
        evidence = load_run_evidence(source)
        keys = sorted({str(key) for row in evidence.samples for key in row})
        provenance = summary["provenance"]
        with target.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=["run_id", "manifest_sha256", "package_sha256", *keys], extrasaction="ignore")
            writer.writeheader()
            for row in evidence.samples:
                writer.writerow({**provenance, **dict(row)})
    else:
        raise ValueError("unsupported metrics export format; use json or csv")
    return target


def compare_metric_runs(
    run_dirs: Sequence[str | Path],
    *,
    metric_ids: Sequence[str] | None = None,
    tolerance: float = 1e-9,
    registry: MetricRegistry | None = None,
) -> dict[str, Any]:
    """Compare registered numeric metrics from two or more eligible runs."""

    if len(run_dirs) < 2:
        raise ValueError("metric comparison requires at least two run packages")
    if not math.isfinite(float(tolerance)) or float(tolerance) < 0:
        raise ValueError("tolerance must be finite and non-negative")
    metric_registry = registry or default_metric_registry()
    evidences = [load_run_evidence(item) for item in run_dirs]
    data_schema_fingerprints = {
        hashlib.sha256(
            canonical_json(
                {
                    "schema": "stage10-data-descriptor-v1",
                    "sample_schema_version": evidence.sample_schema_version,
                    "metrics_schema_version": evidence.metrics_schema_version,
                    "sample_fields": list(evidence.sample_fields),
                }
            )
        ).hexdigest()
        for evidence in evidences
    }
    if len(data_schema_fingerprints) != 1:
        raise PostprocessEligibilityError(
            ("input runs use incompatible data schema versions; normalize or rerun before comparison",)
        )
    selected = tuple(metric_ids) if metric_ids is not None else metric_registry.metric_ids
    if not selected:
        raise ValueError("at least one metric_id must be selected")
    if len(set(selected)) != len(selected):
        raise ValueError("metric_ids must be unique")
    # Resolve all IDs before calculating any output so an unknown request cannot
    # produce a partially useful report.
    definitions = [metric_registry.resolve(item) for item in selected]
    records = [
        {
            "run_id": evidence.manifest.get("run_id"),
            "experiment_id": evidence.manifest.get("experiment_id"),
            "manifest_sha256": evidence.manifest.get("manifest_sha256"),
            "package_sha256": evidence.manifest.get("package_sha256"),
            "working_tree_status": evidence.manifest.get("working_tree_status"),
            "source_provenance_status": (evidence.manifest.get("provenance") or {}).get("status") if isinstance(evidence.manifest.get("provenance"), Mapping) else None,
            "environment_status": (evidence.manifest.get("environment") or {}).get("status") if isinstance(evidence.manifest.get("environment"), Mapping) else None,
            "status": evidence.manifest.get("status"),
            "plant_id": (evidence.manifest.get("plant") or {}).get("id"),
            "controller_id": (evidence.manifest.get("controller") or {}).get("id"),
            "sample_schema_version": evidence.sample_schema_version,
            "metrics_schema_version": evidence.metrics_schema_version,
            "sample_fields": list(evidence.sample_fields),
        }
        for evidence in evidences
    ]
    computed = [metric_registry.compute(evidence, selected) for evidence in evidences]
    metric_results: dict[str, dict[str, Any]] = {}
    for definition in definitions:
        values = [computed[index][definition.metric_id]["value"] for index in range(len(computed))]
        pairs: list[dict[str, Any]] = []
        maximum = 0.0
        for left in range(len(values)):
            for right in range(left + 1, len(values)):
                error = abs(float(values[left]) - float(values[right]))
                maximum = max(maximum, error)
                pairs.append(
                    {
                        "left_run": records[left]["run_id"],
                        "right_run": records[right]["run_id"],
                        "left": values[left],
                        "right": values[right],
                        "absolute_error": error,
                        "within_tolerance": error <= float(tolerance),
                    }
                )
        outcome = "exact_match" if maximum == 0.0 else "tolerance_match" if maximum <= float(tolerance) else "mismatch"
        metric_results[definition.metric_id] = {
            "metric_id": definition.metric_id,
            "version": definition.version,
            "unit": definition.unit,
            "input_fields": list(definition.input_fields),
            "values": [
                {"run_id": records[index]["run_id"], "value": values[index]}
                for index in range(len(values))
            ],
            "outcome": outcome,
            "maximum_absolute_error": maximum,
            "pairs": pairs,
        }
    outcomes = {item["outcome"] for item in metric_results.values()}
    overall = "mismatch" if "mismatch" in outcomes else "tolerance_match" if "tolerance_match" in outcomes else "exact_match"
    return {
        "schema": _COMPARISON_SCHEMA,
        "metric_schema": _METRIC_SCHEMA,
        "data": {
            "schema": "stage10-data-descriptor-v1",
            "sample_schema_version": evidences[0].sample_schema_version,
            "metrics_schema_version": evidences[0].metrics_schema_version,
            "schema_fingerprint": next(iter(data_schema_fingerprints)),
        },
        "metric_registry": metric_registry.to_dict(),
        "tolerance": float(tolerance),
        "runs": records,
        "metrics": metric_results,
        "outcome": overall,
        "limitations": [
            "This is a numeric metric comparison only; it does not establish physical equivalence or controller quality.",
            "Each input package passed the restricted Stage 10 qualification and integrity gate.",
        ],
    }


def build_evidence_report(
    run_dirs: Sequence[str | Path],
    *,
    metric_ids: Sequence[str] | None = None,
    tolerance: float = 1e-9,
    registry: MetricRegistry | None = None,
) -> dict[str, Any]:
    """Build a machine-readable, source-linked Stage 10 evidence report."""

    comparison = compare_metric_runs(run_dirs, metric_ids=metric_ids, tolerance=tolerance, registry=registry)
    return {
        "schema": _REPORT_SCHEMA,
        "report_type": "restricted_metric_comparison",
        "provenance": {
            "runs": comparison["runs"],
            "metric_schema": comparison["metric_schema"],
            "metric_registry": comparison["metric_registry"],
            "data": comparison["data"],
        },
        "comparison": comparison,
        "limitations": list(comparison["limitations"])
        + ["This report is evidence for review, not an engineering or hardware approval."],
    }


def write_evidence_report(
    run_dirs: Sequence[str | Path],
    destination: str | Path,
    *,
    metric_ids: Sequence[str] | None = None,
    tolerance: float = 1e-9,
    registry: MetricRegistry | None = None,
) -> Path:
    """Write a JSON evidence report without modifying any source run."""

    sources = [_as_run_dir(item).resolve() for item in run_dirs]
    target = Path(destination)
    for source in sources:
        _assert_outside_run(source, target)
    report = build_evidence_report(sources, metric_ids=metric_ids, tolerance=tolerance, registry=registry)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return target


__all__ = [
    "PostprocessEligibilityError",
    "UnknownMetricError",
    "MetricDefinition",
    "MetricRegistry",
    "default_metric_registry",
    "RunEvidence",
    "load_run_evidence",
    "summarize_run",
    "export_metrics",
    "compare_metric_runs",
    "build_evidence_report",
    "write_evidence_report",
]

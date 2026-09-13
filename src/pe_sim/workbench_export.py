"""Export a published reference run into the Stage 11 workbench contracts.

This deliberately narrow offline adapter reads an immutable run package,
verifies its published integrity information, and writes derived JSON fixtures
elsewhere. The browser still consumes only the three public contracts.
"""

from __future__ import annotations

from dataclasses import dataclass
import argparse
import json
import math
import os
from pathlib import Path
from typing import Any, Mapping, Sequence

from .artifacts import artifact_index, manifest_digest, package_digest
from .outputs import OutputCollector, OutputDefinition, OutputRegistry
from .reference_plants import BoostPlant, BuckPlant
from .topology import TopologyDescriptor, export_topology_descriptor
from .traces import SamplingPolicy, SignalDefinition, SignalRegistry, TraceCollector, TraceDataset, TraceProvenance


_FIXTURE_FILENAMES = ("topology.json", "trace.json", "outputs.json")
_PUBLISHED_STATUSES = frozenset({"RUN_OK", "QUALIFIED", "COMPARABLE"})
_PLANT_PARAMETERS = (
    "input_voltage_v",
    "inductance_h",
    "capacitance_f",
    "load_resistance_ohm",
    "integration_step_s",
)


class WorkbenchExportError(ValueError):
    """Raised when a run cannot safely be projected into display contracts."""


@dataclass(frozen=True)
class WorkbenchFixtureExport:
    """Summary of three derived files and their source identity."""

    destination: Path
    run_id: str
    manifest_sha256: str
    package_sha256: str
    topology: TopologyDescriptor
    trace: TraceDataset
    outputs_fingerprint: str

    @property
    def files(self) -> dict[str, Path]:
        return {name: self.destination / name for name in _FIXTURE_FILENAMES}

    def to_dict(self) -> dict[str, Any]:
        return {
            "destination": str(self.destination),
            "files": {name: str(path) for name, path in self.files.items()},
            "run_id": self.run_id,
            "manifest_sha256": self.manifest_sha256,
            "package_sha256": self.package_sha256,
            "topology_fingerprint": self.topology.fingerprint,
            "trace_sample_count": len(self.trace.samples),
            "outputs_fingerprint": self.outputs_fingerprint,
        }


@dataclass(frozen=True)
class WorkbenchContracts:
    """Verified, in-memory visualization contracts for one published run."""

    run_id: str
    manifest_sha256: str
    package_sha256: str
    topology: TopologyDescriptor
    trace: TraceDataset
    outputs: Any

    def to_dict(self) -> dict[str, Any]:
        return {
            "topology": self.topology.to_dict(),
            "trace": self.trace.to_dict(),
            "outputs": self.outputs.to_dict(),
        }


def _load_json(path: Path, description: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WorkbenchExportError(f"{description} is unreadable: {type(exc).__name__}: {exc}") from exc


def _mapping(value: Any, description: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise WorkbenchExportError(f"{description} must be a JSON object")
    return value


def _hash(value: Any, field: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(character not in "0123456789abcdef" for character in value.lower()):
        raise WorkbenchExportError(f"{field} must be a SHA-256 hex digest")
    return value


def _published_manifest(run_dir: Path) -> Mapping[str, Any]:
    manifest_path = run_dir / "manifest.json"
    state_path = run_dir / ".run-state.json"
    samples_path = run_dir / "samples.json"
    if not manifest_path.is_file() or not samples_path.is_file() or not state_path.is_file():
        raise WorkbenchExportError("published run requires manifest.json, samples.json, and .run-state.json")

    manifest = _mapping(_load_json(manifest_path, "manifest.json"), "manifest.json")
    state = _mapping(_load_json(state_path, ".run-state.json"), ".run-state.json")
    if manifest.get("status") not in _PUBLISHED_STATUSES:
        raise WorkbenchExportError(f"run status {manifest.get('status')!r} is not displayable")
    if state.get("status") != "PUBLISHED":
        raise WorkbenchExportError("run state marker is not PUBLISHED")

    run_id = manifest.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        raise WorkbenchExportError("manifest run_id is missing")
    if state.get("run_id") not in {None, run_id}:
        raise WorkbenchExportError("run state marker does not match manifest run_id")

    recorded_artifacts = manifest.get("artifacts")
    if not isinstance(recorded_artifacts, Mapping) or dict(recorded_artifacts) != artifact_index(run_dir):
        raise WorkbenchExportError("manifest artifact index does not match package contents")
    manifest_sha256 = _hash(manifest.get("manifest_sha256"), "manifest_sha256")
    package_sha256 = _hash(manifest.get("package_sha256"), "package_sha256")
    hashes = manifest.get("hashes")
    if not isinstance(hashes, Mapping) or hashes.get("manifest_sha256") != manifest_sha256 or hashes.get("package_sha256") != package_sha256:
        raise WorkbenchExportError("manifest hash summary is missing or inconsistent")
    if manifest_digest(manifest) != manifest_sha256:
        raise WorkbenchExportError("manifest_sha256 does not match manifest contents")
    if package_digest(run_dir, manifest) != package_sha256:
        raise WorkbenchExportError("package_sha256 does not match package contents")
    return manifest


def _reference_plant(manifest: Mapping[str, Any]) -> BuckPlant | BoostPlant:
    plant = _mapping(manifest.get("plant"), "manifest plant")
    plant_id = plant.get("id")
    identity = _mapping(plant.get("identity"), "manifest plant identity")
    parameters = _mapping(identity.get("parameters"), "manifest plant parameters")
    if (
        plant_id not in {"buck", "boost"}
        or identity.get("topology") != plant_id
        or identity.get("kind") != "ideal_averaged_reference"
    ):
        raise WorkbenchExportError(f"no reviewed workbench topology exporter for {plant_id!r}")
    missing = set(_PLANT_PARAMETERS) - set(parameters)
    if missing:
        raise WorkbenchExportError("manifest plant parameters are incomplete: " + ", ".join(sorted(missing)))
    try:
        kwargs = {name: float(parameters[name]) for name in _PLANT_PARAMETERS}
        return BuckPlant(**kwargs) if plant_id == "buck" else BoostPlant(**kwargs)
    except (TypeError, ValueError) as exc:
        raise WorkbenchExportError(f"manifest plant parameters are invalid: {exc}") from exc


def _provenance(manifest: Mapping[str, Any]) -> TraceProvenance:
    return TraceProvenance(
        run_id=str(manifest["run_id"]),
        manifest_sha256=str(manifest["manifest_sha256"]),
        package_sha256=str(manifest["package_sha256"]),
        source_commit=manifest.get("source_commit") if isinstance(manifest.get("source_commit"), str) else None,
        producer="pe_sim.workbench_export.v1",
        metadata={
            "derived_from": "published_run_package",
            "integrity": "verified",
            "run_status": manifest["status"],
            "run_mode": manifest.get("run_mode", "unknown"),
            "evidence_level": manifest.get("evidence_level", "unknown"),
            "working_tree_status": manifest.get("working_tree_status", "unknown"),
        },
    )


def _samples_to_trace(samples: Any, provenance: TraceProvenance) -> TraceDataset:
    if not isinstance(samples, list) or not samples:
        raise WorkbenchExportError("samples.json must contain at least one sample")
    registry = SignalRegistry(
        (
            SignalDefinition("plant.vout", "Output voltage", "V", source="measurement"),
            SignalDefinition("plant.inductor_current", "Inductor current", "A", source="measurement"),
            SignalDefinition("controller.duty", "Applied duty command", "1", source="action"),
        )
    )
    collector = TraceCollector(
        registry,
        sampling_policy=SamplingPolicy(mode="every_step", include_initial=True),
        provenance=provenance,
        metadata={"derived_from": "samples.json", "source_sample_schema": "samples-v1"},
    )
    previous_time: float | None = None
    for index, raw_row in enumerate(samples):
        row = _mapping(raw_row, f"samples.json row {index}")
        try:
            time_s = float(row["time_s"])
            values = {
                "plant.vout": float(row["vout"]),
                "plant.inductor_current": float(row["inductor_current"]),
                "controller.duty": float(row["action"]),
            }
        except (KeyError, TypeError, ValueError) as exc:
            raise WorkbenchExportError(f"samples.json row {index} lacks a usable Buck/Boost observation: {exc}") from exc
        if not math.isfinite(time_s) or time_s < 0 or not all(math.isfinite(value) for value in values.values()):
            raise WorkbenchExportError(f"samples.json row {index} contains non-finite values")
        if previous_time is not None and time_s < previous_time:
            raise WorkbenchExportError("samples.json timestamps must be monotonic")
        previous_time = time_s
        collector.record(time_s, values)
    return collector.dataset()


def _outputs_from_trace(trace: TraceDataset, provenance: TraceProvenance):
    samples = trace.samples
    final = samples[-1].values
    registry = OutputRegistry(
        (
            OutputDefinition("vout.final", "Final output voltage", "scalar", source="samples", unit="V"),
            OutputDefinition("inductor_current.final", "Final inductor current", "scalar", source="samples", unit="A"),
            OutputDefinition("duty.final", "Final applied duty command", "scalar", source="samples", unit="1"),
            OutputDefinition("vout.response", "Output-voltage response", "series", source="samples", unit="V"),
            OutputDefinition("inductor_current.response", "Inductor-current response", "series", source="samples", unit="A"),
        )
    )
    collector = OutputCollector(
        registry,
        provenance=provenance,
        metadata={"derived_from": "samples.json", "source_sample_count": len(samples)},
    )
    times = [sample.time_s for sample in samples]
    collector.publish("vout.final", final["plant.vout"])
    collector.publish("inductor_current.final", final["plant.inductor_current"])
    collector.publish("duty.final", final["controller.duty"])
    collector.publish("vout.response", {"time_s": times, "values": [sample.values["plant.vout"] for sample in samples]})
    collector.publish("inductor_current.response", {"time_s": times, "values": [sample.values["plant.inductor_current"] for sample in samples]})
    return collector.dataset()


def _write_json_atomically(path: Path, document: Mapping[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(document, ensure_ascii=True, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def export_run_to_workbench_fixture(
    run_dir: str | Path,
    destination: str | Path,
    *,
    overwrite: bool = False,
) -> WorkbenchFixtureExport:
    """Create derived workbench files from a verified published Buck/Boost run.

    ``destination`` must be outside ``run_dir``. Existing fixture files are
    rejected unless ``overwrite`` is explicit. No source-package file is
    opened for writing.
    """

    source = Path(run_dir).resolve()
    target = Path(destination).resolve()
    if not source.is_dir():
        raise WorkbenchExportError(f"run directory does not exist: {source}")
    if target == source or source in target.parents:
        raise WorkbenchExportError("derived workbench fixtures must be outside the immutable run directory")
    if target.exists() and not target.is_dir():
        raise WorkbenchExportError("fixture destination must be a directory")
    existing = [target / name for name in _FIXTURE_FILENAMES if (target / name).exists()]
    if existing and not overwrite:
        raise WorkbenchExportError("destination already contains fixture data; pass overwrite=True to replace it")

    contracts = load_run_workbench_contracts(source)
    topology = contracts.topology
    trace = contracts.trace
    outputs = contracts.outputs

    target.mkdir(parents=True, exist_ok=True)
    _write_json_atomically(target / "topology.json", topology.to_dict())
    _write_json_atomically(target / "trace.json", trace.to_dict())
    _write_json_atomically(target / "outputs.json", outputs.to_dict())
    return WorkbenchFixtureExport(
        destination=target,
        run_id=contracts.run_id,
        manifest_sha256=contracts.manifest_sha256,
        package_sha256=contracts.package_sha256,
        topology=topology,
        trace=trace,
        outputs_fingerprint=outputs.fingerprint,
    )


def load_run_workbench_contracts(run_dir: str | Path) -> WorkbenchContracts:
    """Verify a published Buck/Boost run and return display contracts in memory.

    This is the read-only boundary used by local services and exporters.  It
    never writes to the run directory and only accepts the reviewed reference
    plant identities handled by this module.
    """

    source = Path(run_dir).resolve()
    if not source.is_dir():
        raise WorkbenchExportError(f"run directory does not exist: {source}")
    manifest = _published_manifest(source)
    samples = _load_json(source / "samples.json", "samples.json")
    provenance = _provenance(manifest)
    topology = export_topology_descriptor(_reference_plant(manifest))
    trace = _samples_to_trace(samples, provenance)
    outputs = _outputs_from_trace(trace, provenance)
    return WorkbenchContracts(
        run_id=str(manifest["run_id"]),
        manifest_sha256=str(manifest["manifest_sha256"]),
        package_sha256=str(manifest["package_sha256"]),
        topology=topology,
        trace=trace,
        outputs=outputs,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Export a verified Buck/Boost run as read-only workbench fixtures.")
    parser.add_argument("run_dir", type=Path, help="published run-package directory")
    parser.add_argument("destination", type=Path, help="derived fixture directory outside the run package")
    parser.add_argument("--overwrite", action="store_true", help="replace the three existing derived fixture files")
    args = parser.parse_args(argv)
    try:
        exported = export_run_to_workbench_fixture(args.run_dir, args.destination, overwrite=args.overwrite)
    except WorkbenchExportError as exc:
        parser.error(str(exc))
    print(json.dumps(exported.to_dict(), ensure_ascii=True, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

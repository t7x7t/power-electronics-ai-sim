"""Gate G-A: a second engine whose time behaviour differs actually runs.

The stepped route keeps kernel-owned time and in-run enforcement.  A whole-run
engine owns its own time axis, so the kernel can only check the returned trace
afterwards.  These tests hold the route to that narrower claim:

1. the engine's package is a normal package - same manifest keys, same
   inspection, same catalogue, same determinism;
2. the route cannot exceed the evidence its enforcement supports, and it says
   so in the manifest;
3. everything it cannot honour (formal comparison, checkpoints, resume, step
   interruption) is refused before a run directory exists;
4. the stepped route's own evidence level is unchanged.
"""

from __future__ import annotations

import ast
import contextlib
import io
import json
from pathlib import Path

import pytest

from pe_sim.cancellation import CallbackCancelToken
from pe_sim.catalog import scan_runs
from pe_sim.cli import main
from pe_sim.contracts import ExperimentSpec, Timebase
from pe_sim.error_codes import KERNEL_FAILURE_CODES
from pe_sim.inspection import inspect_run
from pe_sim.run_events import (
    EVENT_RUN_FINISHED,
    EVENT_RUN_STARTED,
    RUN_EVENT_SCHEMA,
    ProgressPolicy,
)
from pe_sim.runner import RunOptions
from pe_sim.runtime import FakePIController, Runner
from pe_sim.time_model import (
    TIME_MODEL_WHOLE_RUN,
    TraceContract,
    WholeRunResult,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]

_CONTRACTS = {
    "safety": {
        "id": "default-safety",
        "hash": "c8af0de8cfeec979bf5fd17dbf1c819df90d6fc5a95fc3a2f53d84271ba0ef58",
    },
    "qualification": {
        "id": "default-qualification",
        "hash": "a3ef375ff21b886d76977854178c77a5bd397e551170fe13e4db9e606f9f121e",
    },
}


def _whole_run_document(tmp_path: Path, run_id: str = "whole") -> dict:
    return {
        "schema_version": "0.1",
        "experiment_id": "gate-ga",
        "run_id": run_id,
        "plant_id": "whole-run-buck",
        "controller_id": "pi",
        "plant_config": {
            "input_voltage_v": 12.0,
            "inductance_h": 100e-6,
            "capacitance_f": 470e-6,
            "load_resistance_ohm": 1.0,
            "integration_step_s": 1e-6,
            "cadence_s": 1e-4,
            "default_vref_v": 3.0,
            "kp": 0.1,
            "ki": 60.0,
        },
        "controller_config": {"kp": 0.1, "ki": 60.0},
        "timebase": {"unit": "s", "control_period_s": 1e-4, "duration_s": 0.02},
        "initial_state": {"mode": "cold_start"},
        "input_schedule": [
            {"time_s": 0.0, "vref": 3.0},
            {"time_s": 0.01, "vref": 5.0},
        ],
        "seed": 0,
        "contracts": _CONTRACTS,
        "output": {"directory": str(tmp_path / "runs"), "retention": "keep"},
    }


def _write(tmp_path: Path, document: dict, name: str = "config.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    return path


def _run_cli(tmp_path: Path, document: dict, *extra: str) -> tuple[int, dict, str]:
    config = _write(tmp_path, document)
    return _capture(config, *extra)


def _capture(config: Path, *extra: str) -> tuple[int, dict, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(["run", str(config), *extra])
    text = out.getvalue().strip()
    payload = json.loads(text) if text.startswith("{") else {"raw": text}
    return code, payload, err.getvalue()


def _events(stderr: str) -> list[dict]:
    events = []
    for line in stderr.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        payload = json.loads(line)
        if payload.get("schema") == RUN_EVENT_SCHEMA:
            events.append(payload)
    return events


class _ShortTracePlant:
    """A whole-run engine that breaks the contract it declared."""

    time_model = TIME_MODEL_WHOLE_RUN

    def trace_contract(self) -> TraceContract:
        return TraceContract(cadence_s=1e-3)

    def run_whole(self, request) -> WholeRunResult:
        return WholeRunResult(time_s=(0.0, 1e-4), signals={"vout": (0.0, 1.0)})


def _spec(tmp_path: Path, run_id: str, plant_id: str = "whole-run-buck") -> ExperimentSpec:
    return ExperimentSpec(
        "gate-ga",
        run_id,
        plant_id,
        "pi",
        Timebase(duration_s=0.003, control_period_s=1e-3),
        output_dir=str(tmp_path / "runs"),
        seed=0,
    )


def test_cli_publishes_a_normal_whole_run_package(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    code, payload, stderr = _run_cli(tmp_path, _whole_run_document(tmp_path))

    assert code == 0, payload
    assert payload["status"] == "QUALIFIED"
    assert payload["failure_category"] is None
    # A whole-run exploratory run is priced below ``limited``: the kernel never
    # enforced anything while the engine ran, and formal comparison is refused
    # outright, so the structural ``limited`` cap is never the binding one.
    assert payload["evidence_level"] == "functional"
    assert stderr == ""

    run_dir = Path(payload["run_dir"])
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["time_model"] == TIME_MODEL_WHOLE_RUN
    assert manifest["status"] == "QUALIFIED"
    assert manifest["evidence_level"] == "functional"
    assert manifest["route_limitations"] == [
        "in_run_enforcement_unavailable",
        "post_hoc_validation_only",
    ]
    assert manifest["enforcement"] == "post_hoc_only"

    samples = json.loads((run_dir / "samples.json").read_text(encoding="utf-8"))
    assert samples[0]["time_s"] == 0.0
    assert samples[-1]["time_s"] == 0.02
    assert all({"step_index", "time_s", "vout"} <= set(row) for row in samples)
    assert all(
        samples[index]["time_s"] < samples[index + 1]["time_s"]
        for index in range(len(samples) - 1)
    )
    assert (run_dir / "samples.npz").exists()

    inspection = inspect_run(run_dir)
    assert inspection.status == "ok"

    catalog = scan_runs(tmp_path / "runs")
    assert [entry.run_id for entry in catalog.runs] == ["whole"]


def test_the_whole_run_package_carries_every_stepped_manifest_key(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    document = _whole_run_document(tmp_path, "whole-keys")
    code, whole_payload, _ = _run_cli(tmp_path, document)
    assert code == 0

    stepped = {
        "schema_version": "0.1",
        "experiment_id": "gate-ga",
        "run_id": "stepped-keys",
        "plant_id": "fake",
        "controller_id": "pi",
        "plant_config": {},
        "controller_config": {"kp": 1.0, "ki": 10.0},
        "timebase": {"unit": "s", "control_period_s": 0.001, "duration_s": 0.01},
        "initial_state": {"mode": "cold_start"},
        "input_schedule": [{"time_s": 0.0, "vref": 1.0}],
        "seed": 0,
        "contracts": _CONTRACTS,
        "output": {"directory": str(tmp_path / "runs"), "retention": "keep"},
    }
    stepped_config = _write(tmp_path, stepped, "stepped.json")
    stepped_code, stepped_payload, _ = _capture(stepped_config)
    assert stepped_code == 0

    whole_manifest = json.loads(
        (Path(whole_payload["run_dir"]) / "manifest.json").read_text(encoding="utf-8")
    )
    stepped_manifest = json.loads(
        (Path(stepped_payload["run_dir"]) / "manifest.json").read_text(encoding="utf-8")
    )
    missing = set(stepped_manifest) - set(whole_manifest)
    assert not missing, f"whole-run manifest is missing {sorted(missing)}"

    whole_metrics = json.loads(
        (Path(whole_payload["run_dir"]) / "metrics.json").read_text(encoding="utf-8")
    )
    stepped_metrics = json.loads(
        (Path(stepped_payload["run_dir"]) / "metrics.json").read_text(encoding="utf-8")
    )
    assert not set(stepped_metrics) - set(whole_metrics)

    # The stepped route keeps its own evidence level; a second engine must not
    # move the first one's price.
    assert stepped_manifest["time_model"] == "stepped"
    assert stepped_manifest["evidence_level"] == "functional"


def test_whole_run_is_deterministic(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    first_document = _whole_run_document(tmp_path, "determinism-a")
    second_document = _whole_run_document(tmp_path, "determinism-b")

    assert _run_cli(tmp_path, first_document)[0] == 0
    assert _run_cli(tmp_path, second_document)[0] == 0

    root = tmp_path / "runs"
    left = json.loads((root / "determinism-a" / "samples.json").read_text(encoding="utf-8"))
    right = json.loads((root / "determinism-b" / "samples.json").read_text(encoding="utf-8"))
    assert left == right


def test_a_trace_that_breaks_its_contract_is_published_as_run_failed(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    spec = _spec(tmp_path, "short-trace", plant_id="short-trace")
    result = Runner().run(
        spec,
        _ShortTracePlant(),
        FakePIController(),
        time_model=TIME_MODEL_WHOLE_RUN,
    )

    assert result.status == "RUN_FAILED"
    assert result.evidence_level == "diagnostic_only"
    assert result.failure_category == "trace_contract_violation"
    assert result.failure_category in KERNEL_FAILURE_CODES

    manifest = json.loads((result.run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "RUN_FAILED"
    assert manifest["evidence_level"] == "diagnostic_only"
    assert manifest["failure"]["category"] == "trace_contract_violation"

    log = json.loads((result.run_dir / "logs" / "run.json").read_text(encoding="utf-8"))
    assert log["problems"], "the validator problems are the evidence"


def test_formal_comparison_is_refused_before_any_directory_exists(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    document = _whole_run_document(tmp_path, "formal")
    config = _write(tmp_path, document)

    code, payload, _ = _capture(config, "--mode", "formal_comparison")

    assert code == 2
    assert payload["schema"] == "pe-sim.rejection.v1"
    assert payload["error_code"] == "formal_comparison_rejected"
    assert not (tmp_path / "runs" / "formal").exists()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"checkpoint_interval_steps": 2},
        {"resume_from": "some-checkpoint.json"},
        {"interrupt_after_steps": 2},
        {"cancel_after_steps": 2},
    ],
)
def test_recovery_options_are_refused_before_any_directory_exists(tmp_path, monkeypatch, kwargs):
    monkeypatch.chdir(tmp_path)
    spec = _spec(tmp_path, "recovery-refused")

    with pytest.raises(ValueError) as excinfo:
        Runner().run(
            spec,
            _ShortTracePlant(),
            FakePIController(),
            time_model=TIME_MODEL_WHOLE_RUN,
            **kwargs,
        )

    assert "whole-run" in str(excinfo.value)
    assert not (tmp_path / "runs" / "recovery-refused").exists()


def test_cancellation_before_the_engine_call_publishes_an_incomplete_package(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    spec = _spec(tmp_path, "cancelled-before")
    options = RunOptions(cancellation=CallbackCancelToken(lambda: True))

    result = Runner().run(
        spec,
        _ShortTracePlant(),
        FakePIController(),
        options=options,
        time_model=TIME_MODEL_WHOLE_RUN,
    )

    assert result.status == "INCOMPLETE"
    assert result.evidence_level == "diagnostic_only"
    samples = json.loads((result.run_dir / "samples.json").read_text(encoding="utf-8"))
    assert samples == []
    manifest = json.loads((result.run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["failure"]["cancellation_observed"] == "before_engine_call"


def test_progress_events_are_versioned_and_a_broken_sink_changes_nothing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    document = _whole_run_document(tmp_path, "progress")
    config = _write(tmp_path, document)

    code, payload, stderr = _capture(config, "--progress")
    assert code == 0
    events = _events(stderr)
    assert events[0]["event"] == EVENT_RUN_STARTED
    assert events[0]["data"]["time_model"] == TIME_MODEL_WHOLE_RUN
    assert events[-1]["event"] == EVENT_RUN_FINISHED
    assert events[-1]["data"]["evidence_level"] == payload["evidence_level"]
    assert [event["sequence"] for event in events] == list(range(len(events)))

    broken_spec = _spec(tmp_path, "broken-sink", plant_id="whole-run-buck")
    from pe_sim.builtin_plants import PlantBuildContext, load_builtin_plants
    from pe_sim.plant_registry import resolve

    load_builtin_plants()
    registration = resolve("whole-run-buck")
    plant = registration.factory(
        {"cadence_s": 1e-3, "load_resistance_ohm": 1.0},
        PlantBuildContext(timebase=broken_spec.timebase),
    )

    def _explode(_event):
        raise RuntimeError("sink is broken")

    result = Runner().run(
        broken_spec,
        plant,
        FakePIController(),
        options=RunOptions(progress=ProgressPolicy(sink=_explode)),
        time_model=TIME_MODEL_WHOLE_RUN,
    )
    assert result.status == "QUALIFIED"
    samples = json.loads((result.run_dir / "samples.json").read_text(encoding="utf-8"))
    assert samples


_KERNEL_MODULES = (
    "src/pe_sim/runtime.py",
    "src/pe_sim/whole_run_route.py",
    "src/pe_sim/cli.py",
    "src/pe_sim/time_model.py",
)


def test_the_kernel_modules_name_no_engine_or_backend():
    forbidden_names = {"WholeRunBuckPlant", "NgspiceBuckPlant", "whole_run_engine"}
    for relative in _KERNEL_MODULES:
        path = _REPO_ROOT / relative
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
        assert not [
            name for name in imported if "backends" in name and "whole_run" in name
        ], f"{relative} imports an engine module"

        declared = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.ClassDef, ast.FunctionDef))
        }
        assert not forbidden_names & declared, f"{relative} declares an engine name"

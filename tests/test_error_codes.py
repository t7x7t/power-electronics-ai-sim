"""B3: stable rejection codes and the kernel failure-category vocabulary."""

from __future__ import annotations

import ast
import json
import re
import subprocess
from pathlib import Path

import pytest

import pe_sim.cli as cli
from pe_sim.checks import CheckFailureError, CheckFinding, CheckReport
from pe_sim.error_codes import (
    KERNEL_FAILURE_CODES,
    REJECTION_SCHEMA,
    REQUEST_REJECTION_CODES,
    RequestRejected,
    rejection_document,
)


def _document(tmp_path: Path, run_id: str = "error-codes", *, duration_s: float = 0.01) -> dict:
    return {
        "schema_version": "0.1",
        "experiment_id": "stage-b3",
        "run_id": run_id,
        "plant_id": "fake",
        "controller_id": "pi",
        "plant_config": {},
        "controller_config": {"kp": 1.0, "ki": 10.0},
        "timebase": {"unit": "s", "control_period_s": 0.001, "duration_s": duration_s},
        "initial_state": {"mode": "cold_start"},
        "input_schedule": [{"time_s": 0.0, "vref": 1.0}],
        "seed": 0,
        "contracts": {
            "safety": {
                "id": "default-safety",
                "hash": "c8af0de8cfeec979bf5fd17dbf1c819df90d6fc5a95fc3a2f53d84271ba0ef58",
            },
            "qualification": {
                "id": "default-qualification",
                "hash": "a3ef375ff21b886d76977854178c77a5bd397e551170fe13e4db9e606f9f121e",
            },
        },
        "output": {"directory": str(tmp_path / "runs"), "retention": "keep"},
    }


def _write(tmp_path: Path, document: dict, name: str) -> Path:
    path = tmp_path / f"{name}.json"
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    return path


def _payload(capsys) -> dict:
    return json.loads(capsys.readouterr().out)


def _dirty_repo(path: Path) -> None:
    """Create a committed repository and then modify a tracked file.

    The formal rejection test must not depend on the surrounding developer
    checkout being dirty; CI checks out a clean tree.  A dirty tracked file
    makes the worktree gate itself the reason for the rejection.
    """

    def run(*args: str) -> None:
        subprocess.run(
            ["git", "-C", str(path), *args],
            check=True,
            capture_output=True,
            text=True,
        )

    run("init", "-q")
    run("config", "user.email", "test@example.invalid")
    run("config", "user.name", "Test")
    tracked = path / "tracked.txt"
    tracked.write_text("baseline\n", encoding="utf-8")
    run("add", "tracked.txt")
    run("commit", "-qm", "baseline")
    tracked.write_text("modified\n", encoding="utf-8")


def test_unknown_plant_is_a_typed_rejection(tmp_path, capsys, monkeypatch):
    document = _document(tmp_path)
    document["plant_id"] = "no-such-plant"
    config = _write(tmp_path, document, "unknown-plant")
    monkeypatch.chdir(tmp_path)

    assert cli.main(["run", str(config)]) == 2
    payload = _payload(capsys)
    assert payload["schema"] == REJECTION_SCHEMA
    assert payload["status"] == "CONFIG_REJECTED"
    assert payload["error_code"] == "unknown_plant"
    assert isinstance(payload["error"], str) and payload["error"]


def test_backend_mismatch_is_a_typed_rejection(tmp_path, capsys, monkeypatch):
    config = _write(tmp_path, _document(tmp_path), "mismatch")
    monkeypatch.chdir(tmp_path)

    assert cli.main(["run", str(config), "--backend", "buck"]) == 2
    payload = _payload(capsys)
    assert payload["schema"] == REJECTION_SCHEMA
    assert payload["error_code"] == "backend_mismatch"
    assert "requires --backend" in payload["error"]


def test_schema_invalid_document_is_invalid_request(tmp_path, capsys, monkeypatch):
    document = _document(tmp_path, "schema-invalid")
    document["unknown"] = "must reject"
    config = _write(tmp_path, document, "schema-invalid")
    monkeypatch.chdir(tmp_path)

    assert cli.main(["run", str(config)]) == 2
    payload = _payload(capsys)
    assert payload["schema"] == REJECTION_SCHEMA
    assert payload["error_code"] == "invalid_request"


def test_unknown_backend_is_a_typed_rejection(capsys):
    assert cli.main(["backend", "doctor", "--name", "no-such-backend"]) == 2
    payload = _payload(capsys)
    assert payload["schema"] == REJECTION_SCHEMA
    assert payload["status"] == "UNKNOWN_BACKEND"
    assert payload["error_code"] == "unknown_backend"


def test_missing_run_package_is_run_inspection_rejected(tmp_path, capsys):
    assert cli.main(["inspect-run", str(tmp_path / "missing")]) == 2
    payload = _payload(capsys)
    assert payload["schema"] == REJECTION_SCHEMA
    assert payload["status"] == "RUN_INSPECTION_REJECTED"
    assert payload["error_code"] == "run_inspection_rejected"


def test_formal_comparison_rejection_is_typed(tmp_path, capsys, monkeypatch):
    _dirty_repo(tmp_path)
    config = _write(tmp_path, _document(tmp_path, "formal"), "formal")
    monkeypatch.chdir(tmp_path)

    assert cli.main(["run", str(config), "--mode", "formal_comparison"]) == 2
    payload = _payload(capsys)
    assert payload["schema"] == REJECTION_SCHEMA
    assert payload["status"] == "FORMAL_COMPARISON_REJECTED"
    assert payload["error_code"] == "formal_comparison_rejected"
    assert "worktree_analysis" in payload


def test_terminal_result_reports_the_failure_category(tmp_path, capsys, monkeypatch):
    success = _write(tmp_path, _document(tmp_path, "success"), "success")
    monkeypatch.chdir(tmp_path)
    assert cli.main(["run", str(success)]) == 0
    payload = _payload(capsys)
    assert payload["failure_category"] is None

    cancelled = _write(tmp_path, _document(tmp_path, "cancelled"), "cancelled")
    request = tmp_path / "cancel.request"
    request.write_text("cancel\n", encoding="utf-8")
    assert cli.main(["run", str(cancelled), "--cancel-file", str(request)]) == 1
    payload = _payload(capsys)
    assert payload["status"] == "INCOMPLETE"
    assert payload["failure_category"] == "cancelled"
    assert request.exists()


def test_adapter_protocol_violation_is_json_not_a_traceback(tmp_path, capsys, monkeypatch):
    config = _write(tmp_path, _document(tmp_path, "protocol"), "protocol")
    monkeypatch.chdir(tmp_path)

    def broken(spec):
        raise TypeError("adapter returned an unexpected shape")

    monkeypatch.setattr(cli, "_build_controller", broken)
    assert cli.main(["run", str(config)]) == 2
    payload = _payload(capsys)
    assert payload["schema"] == REJECTION_SCHEMA
    assert payload["error_code"] == "invalid_request"
    assert "unexpected shape" in payload["error"]


def test_rejection_helpers_reject_unknown_codes():
    with pytest.raises(ValueError, match="unknown request rejection code"):
        RequestRejected("not_a_code", "message")
    with pytest.raises(ValueError, match="unknown request rejection code"):
        rejection_document("CONFIG_REJECTED", "not_a_code", "message")
    with pytest.raises(ValueError, match="status"):
        rejection_document("", "invalid_request", "message")

    document = rejection_document(
        "CONFIG_REJECTED", "invalid_request", "message", {"status": "HIDDEN", "extra": 1}
    )
    assert document["status"] == "CONFIG_REJECTED", "contract keys win over extra keys"
    assert document["extra"] == 1


def test_vocabulary_invariants():
    snake_case = re.compile(r"^[a-z][a-z0-9_]*$")
    assert all(snake_case.match(code) for code in REQUEST_REJECTION_CODES)
    assert all(snake_case.match(code) for code in KERNEL_FAILURE_CODES)
    assert REQUEST_REJECTION_CODES.isdisjoint(KERNEL_FAILURE_CODES)
    assert {
        "cancelled",
        "interrupted",
        "runtime_error",
        "missing_capability",
        "safety_violation",
        "stale_observation",
        "checker_error",
        "ccm_boundary_violated",
    } <= KERNEL_FAILURE_CODES


def test_blocking_finding_wins_over_an_earlier_advisory():
    advisory = CheckFinding("rule.advisory", "future_observation", "advisory", severity="warning")
    blocking = CheckFinding(
        "rule.blocking", "stale_observation", "blocked", severity="error", stop_requested=True
    )
    error = CheckFailureError("blocked", CheckReport(False, (advisory, blocking)), phase="observation")

    assert error.category == "stale_observation"


def test_kernel_check_findings_are_documented():
    """A new built-in finding category must be added to the vocabulary."""

    source_root = Path(__file__).resolve().parents[1] / "src" / "pe_sim"
    categories: set[str] = set()
    for name in ("checks.py", "applicability.py"):
        tree = ast.parse((source_root / name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            function = node.func
            called = getattr(function, "id", None) or getattr(function, "attr", None)
            argument = None
            if called == "CheckFinding" and len(node.args) >= 2:
                argument = node.args[1]
            elif called in {"failure", "_record"} and len(node.args) >= 2:
                argument = node.args[1] if called == "_record" else node.args[0]
            if argument is None:
                continue
            if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                categories.add(argument.value)

    assert categories, "the scan must find the built-in finding categories"
    assert categories <= KERNEL_FAILURE_CODES, (
        f"undocumented kernel finding categories: {sorted(categories - KERNEL_FAILURE_CODES)}"
    )

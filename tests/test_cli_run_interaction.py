"""B2: the CLI's documented start/progress/cancel/terminal-result surface."""

from __future__ import annotations

import json
from pathlib import Path

from pe_sim.cli import main
from pe_sim.inspection import inspect_run
from pe_sim.run_events import (
    EVENT_RUN_FINISHED,
    EVENT_RUN_STARTED,
    RUN_EVENT_SCHEMA,
    RUN_RESULT_SCHEMA,
)


def _config(tmp_path: Path, run_id: str = "interaction", *, duration_s: float = 0.01) -> Path:
    document = {
        "schema_version": "0.1",
        "experiment_id": "stage-b2",
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
    path = tmp_path / f"{run_id}.json"
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    return path


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


def test_cli_prints_a_versioned_terminal_result(tmp_path, capsys, monkeypatch):
    config = _config(tmp_path)
    monkeypatch.chdir(tmp_path)

    assert main(["run", str(config)]) == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert payload["schema"] == RUN_RESULT_SCHEMA
    assert payload["run_id"] == "interaction"
    assert payload["status"] in {"RUN_OK", "QUALIFIED"}
    assert payload["evidence_level"] in {
        "diagnostic_only",
        "functional",
        "limited",
        "comparable",
    }
    assert captured.err == "", "no interaction surface is paid for unless requested"


def test_cli_streams_versioned_progress_events_on_stderr(tmp_path, capsys, monkeypatch):
    config = _config(tmp_path)
    monkeypatch.chdir(tmp_path)

    assert main(["run", str(config), "--progress"]) == 0
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    events = _events(captured.err)

    assert events, "the flag must produce a parseable stream"
    assert events[0]["event"] == EVENT_RUN_STARTED
    assert events[-1]["event"] == EVENT_RUN_FINISHED
    assert [event["sequence"] for event in events] == list(range(len(events)))
    assert events[-1]["data"]["status"] == result["status"]
    assert events[-1]["data"]["run_dir"] == result["run_dir"]
    assert events[-1]["data"]["evidence_level"] == result["evidence_level"]


def test_cli_cancel_file_stops_the_run_and_leaves_the_request_in_place(
    tmp_path, capsys, monkeypatch
):
    config = _config(tmp_path)
    request = tmp_path / "cancel.request"
    request.write_text("cancel\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    assert main(["run", str(config), "--cancel-file", str(request), "--progress"]) == 1
    captured = capsys.readouterr()
    result = json.loads(captured.out)

    assert result["status"] == "INCOMPLETE"
    assert "cancelled" in result["qualification"]["reasons"]
    run_dir = Path(result["run_dir"])
    assert (run_dir / "checkpoint.json").exists()
    assert json.loads((run_dir / "samples.json").read_text(encoding="utf-8")) == []
    assert request.exists(), "the runner never deletes the client's request"

    events = _events(captured.err)
    assert events[-1]["event"] == EVENT_RUN_FINISHED
    assert events[-1]["data"]["status"] == "INCOMPLETE"
    assert events[-1]["data"]["failure_category"] == "cancelled"

    # The cancelled package is still a published, verifiable package: the
    # interaction surface never becomes a second definition of validity.
    inspection = inspect_run(run_dir)
    assert inspection.status == "ok"
    assert inspection.summary["run_ok"] is False

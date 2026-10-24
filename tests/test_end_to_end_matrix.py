"""Composed end-to-end matrix across the documented surfaces.

Each feature has its own focused tests.  This file checks the *composition*:
that the run, inspect, catalog, comparison, export, evidence and capability
surfaces agree with each other, that every payload carries its documented
schema tag, and that reading a published package never changes it (invariant
I3).

Nothing here asserts a physical or hardware claim.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
from pathlib import Path

import pytest

from pe_sim.capabilities import CAPABILITY_SCHEMA
from pe_sim.catalog import CATALOG_SCHEMA
from pe_sim.cli import main
from pe_sim.error_codes import REJECTION_SCHEMA
from pe_sim.inspection import INSPECTION_SCHEMA
from pe_sim.outputs import OutputDataset
from pe_sim.run_events import (
    DEMO_RESULT_SCHEMA,
    RUN_EVENT_SCHEMA,
    RUN_RESULT_SCHEMA,
)
from pe_sim.topology import TopologyDescriptor
from pe_sim.traces import TraceDataset

_REPOSITORY = Path(__file__).resolve().parents[1]


def _capture(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(argv)
    return code, out.getvalue(), err.getvalue()


def _payload(argv: list[str]) -> tuple[int, dict, str]:
    code, stdout, stderr = _capture(argv)
    return code, json.loads(stdout), stderr


def _snapshot(directory: Path) -> dict[str, str]:
    return {
        path.relative_to(directory).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


def test_the_documented_journey_is_composed_and_leaves_the_package_unchanged(tmp_path):
    code, demo, stderr = _payload(["demo", str(tmp_path / "demo"), "--run-id", "matrix-demo"])
    assert code == 0
    assert stderr == ""
    run_dir = Path(demo["run"]["run_dir"])
    before = _snapshot(run_dir)
    assert before, "a published package must contain artifacts"

    code, inspection, _ = _payload(["inspect-run", str(run_dir), "--compact"])
    assert code == 0
    assert inspection["status"] == "ok"
    assert inspection["artifact_index_verified"] is True
    assert inspection["run_ok"] is True
    assert inspection["compatibility"]["status"] == "current"
    assert inspection["evidence_level"] == demo["run"]["evidence_level"]

    code, catalog, _ = _payload(["list-runs", str(tmp_path / "demo" / "runs")])
    assert code == 0
    assert [entry["run_id"] for entry in catalog["runs"]] == ["matrix-demo"]

    code, comparison, _ = _payload(["compare-runs", str(run_dir), str(run_dir)])
    # The documented contract is the relation, not a fixed outcome: exit 0 only
    # for an exact or tolerance match.  A dirty worktree yields ``unknown``.
    assert code == ((0) if comparison["outcome"] in {"exact_match", "tolerance_match"} else 1)

    code, exported, _ = _payload(
        ["export-workbench", str(run_dir), str(tmp_path / "derived")]
    )
    assert code == 0
    assert set(exported["files"]) == {"topology.json", "trace.json", "outputs.json"}
    TopologyDescriptor.from_dict(
        json.loads((tmp_path / "derived" / "topology.json").read_text(encoding="utf-8"))
    )
    TraceDataset.from_dict(
        json.loads((tmp_path / "derived" / "trace.json").read_text(encoding="utf-8"))
    )
    OutputDataset.from_dict(
        json.loads((tmp_path / "derived" / "outputs.json").read_text(encoding="utf-8"))
    )

    # Reading, comparing, and exporting must not rewrite the evidence they name.
    assert _snapshot(run_dir) == before


def test_every_surface_carries_its_documented_schema_tag(tmp_path):
    code, demo, _ = _payload(["demo", str(tmp_path / "demo"), "--run-id", "matrix-tags"])
    assert code == 0
    assert demo["schema"] == DEMO_RESULT_SCHEMA
    assert demo["run"]["schema"] == RUN_RESULT_SCHEMA

    run_dir = demo["run"]["run_dir"]
    _, inspection, _ = _payload(["inspect-run", run_dir, "--compact"])
    assert inspection["schema"] == INSPECTION_SCHEMA

    _, catalog, _ = _payload(["list-runs", str(tmp_path / "demo" / "runs")])
    assert catalog["schema_version"] == CATALOG_SCHEMA

    _, capabilities, _ = _payload(["capabilities", "--compact"])
    assert capabilities["schema"] == CAPABILITY_SCHEMA

    code, rejection, _ = _payload(["inspect-run", str(tmp_path / "does-not-exist")])
    assert code == 2
    assert rejection["schema"] == REJECTION_SCHEMA


def test_the_whole_run_route_composes_with_inspect_export_and_catalog(tmp_path):
    document = json.loads(
        (_REPOSITORY / "examples" / "whole_run_buck_step" / "config.json").read_text(encoding="utf-8")
    )
    document["run_id"] = "matrix-whole-run"
    document["output"]["directory"] = str(tmp_path / "runs")
    config = tmp_path / "whole-run.json"
    config.write_text(json.dumps(document) + "\n", encoding="utf-8")

    code, result, _ = _payload(["run", str(config)])
    assert code == 0
    assert result["status"] == "QUALIFIED"
    assert result["evidence_level"] == "functional"
    run_dir = Path(result["run_dir"])
    before = _snapshot(run_dir)

    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["time_model"] == "whole_run"
    assert manifest["enforcement"] == "post_hoc_only"

    code, inspection, _ = _payload(["inspect-run", str(run_dir), "--compact"])
    assert code == 0
    assert inspection["status"] == "ok"

    code, exported, _ = _payload(
        ["export-workbench", str(run_dir), str(tmp_path / "derived")]
    )
    assert code == 0
    topology = TopologyDescriptor.from_dict(
        json.loads((tmp_path / "derived" / "topology.json").read_text(encoding="utf-8"))
    )
    assert topology.metadata["time_model"] == "whole_run"

    code, catalog, _ = _payload(["list-runs", str(tmp_path / "runs")])
    assert code == 0
    assert [entry["run_id"] for entry in catalog["runs"]] == ["matrix-whole-run"]

    assert _snapshot(run_dir) == before


def test_whole_run_progress_and_terminal_result_agree(tmp_path):
    document = json.loads(
        (_REPOSITORY / "examples" / "whole_run_buck_step" / "config.json").read_text(encoding="utf-8")
    )
    document["run_id"] = "matrix-progress"
    document["output"]["directory"] = str(tmp_path / "runs")
    config = tmp_path / "whole-run.json"
    config.write_text(json.dumps(document) + "\n", encoding="utf-8")

    code, stdout, stderr = _capture(["run", str(config), "--progress"])
    assert code == 0
    result = json.loads(stdout)
    events = [
        json.loads(line)
        for line in stderr.splitlines()
        if line.strip().startswith("{") and json.loads(line).get("schema") == RUN_EVENT_SCHEMA
    ]
    assert events[0]["event"] == "run.started"
    assert events[-1]["event"] == "run.finished"
    assert [event["sequence"] for event in events] == list(range(len(events)))
    assert events[-1]["data"]["evidence_level"] == result["evidence_level"]
    assert events[-1]["data"]["status"] == result["status"]


def test_a_cancelled_whole_run_still_publishes_a_readable_package(tmp_path):
    document = json.loads(
        (_REPOSITORY / "examples" / "whole_run_buck_step" / "config.json").read_text(encoding="utf-8")
    )
    document["run_id"] = "matrix-cancelled"
    document["output"]["directory"] = str(tmp_path / "runs")
    config = tmp_path / "whole-run.json"
    config.write_text(json.dumps(document) + "\n", encoding="utf-8")
    request = tmp_path / "cancel.request"
    request.write_text("cancel\n", encoding="utf-8")

    code, result, _ = _payload(["run", str(config), "--cancel-file", str(request)])
    assert code == 1
    assert result["status"] == "INCOMPLETE"
    run_dir = result["run_dir"]

    code, inspection, _ = _payload(["inspect-run", run_dir, "--compact"])
    assert code == 0
    assert inspection["status"] == "ok"
    assert inspection["run_status"] == "INCOMPLETE"
    assert inspection["run_ok"] is False


@pytest.mark.parametrize(
    ("required", "expected_code"),
    [
        ("workbench_visualization_service", 0),
        ("pyspice_netlist_builder", None),
        ("not_a_capability", 2),
    ],
)
def test_capabilities_require_is_scriptable(tmp_path, required, expected_code):
    code, payload, _ = _payload(["capabilities", "--require", required, "--compact"])

    if expected_code is None:
        # PySpice may be installed or not; the contract is that the exit code
        # follows the probe rather than a fixed assumption.
        status = {
            item["capability_id"]: item["status"] for item in payload["capabilities"]
        }[required]
        assert code == (0 if status == "installed" else 1)
    else:
        assert code == expected_code
    if expected_code == 2:
        assert payload["schema"] == REJECTION_SCHEMA
    else:
        assert payload["schema"] == CAPABILITY_SCHEMA

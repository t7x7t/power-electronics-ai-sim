"""T6: a user who does not read source can run, view, and verify.

The documented workflow is a single `pe-sim demo` command that writes a bundled
experiment document and runs it through the same path as `pe-sim run`, followed
by the read-only `inspect-run` and `visualization-serve` commands.  Nothing in
that workflow grants the caller a Runner, a Plant, a Controller, or write
authority over a published package.

T2 is also guarded here: no kernel module and no schema may enumerate
topologies, so a new plant is added by registration rather than by editing a
list.
"""

from __future__ import annotations

import ast
import contextlib
import io
import json
from pathlib import Path

import pytest

from pe_sim.catalog import scan_runs
from pe_sim.cli import main
from pe_sim.contracts import validate_experiment_document
from pe_sim.inspection import inspect_run
from pe_sim.run_events import DEMO_RESULT_SCHEMA

_REPOSITORY = Path(__file__).resolve().parents[1]
_TOPOLOGY_NAMES = {"buck", "boost", "fake", "fake-load", "ngspice-buck", "whole-run-buck"}


def _demo(directory: Path, *extra: str) -> tuple[int, dict, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(["demo", str(directory), *extra])
    return code, json.loads(out.getvalue()), err.getvalue()


def test_demo_runs_a_bundled_experiment_and_publishes_a_verifiable_package(tmp_path):
    code, payload, stderr = _demo(tmp_path / "demo", "--run-id", "workflow-buck")

    assert code == 0
    assert stderr == ""
    assert payload["schema"] == DEMO_RESULT_SCHEMA
    assert payload["name"] == "buck"

    config_path = Path(payload["config"])
    assert config_path.is_file()
    document = json.loads(config_path.read_text(encoding="utf-8"))
    validate_experiment_document(document)
    assert document["run_id"] == "workflow-buck"
    assert document["output"]["directory"] == str((tmp_path / "demo" / "runs").resolve())

    run = payload["run"]
    assert run["schema"] == "pe-sim.run-result.v1"
    assert run["status"] == "QUALIFIED"
    assert run["failure_category"] is None

    inspection = inspect_run(run["run_dir"])
    assert inspection.status == "ok"
    assert inspection.summary["run_ok"] is True
    assert inspection.summary["artifact_index_verified"] is True


def test_demo_can_run_the_boost_template_and_the_catalogue_finds_both_runs(tmp_path):
    assert _demo(tmp_path / "demo", "--run-id", "workflow-a")[0] == 0
    assert _demo(tmp_path / "demo", "--name", "boost", "--run-id", "workflow-b")[0] == 0

    catalog = scan_runs(tmp_path / "demo" / "runs")
    assert {entry.run_id for entry in catalog.runs} == {"workflow-a", "workflow-b"}
    assert {entry.plant_id for entry in catalog.runs} == {"buck", "boost"}


def test_the_documented_next_steps_are_read_only(tmp_path):
    _, payload, _ = _demo(tmp_path / "demo", "--run-id", "workflow-next")

    commands = payload["next"]
    assert commands and all(command.startswith("pe-sim ") for command in commands)
    # The demo hands the user read-only commands; it never prints a mutation
    # command such as export with --overwrite or a re-run of the package.
    assert all("inspect-run" in command or "visualization-serve" in command for command in commands)


def test_the_schema_does_not_enumerate_topologies():
    schema = json.loads((_REPOSITORY / "schemas" / "experiment.schema.json").read_text(encoding="utf-8"))
    plant_id = schema["properties"]["plant_id"]

    assert "enum" not in plant_id
    assert plant_id["type"] == "string"


def test_no_kernel_module_holds_a_topology_allow_list():
    """A new topology must be declared, never added to a kernel list."""

    for relative in ("src/pe_sim/cli.py", "src/pe_sim/runtime.py", "src/pe_sim/plant_registry.py"):
        tree = ast.parse((_REPOSITORY / relative).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.List, ast.Set, ast.Tuple)):
                continue
            literals = {
                element.value
                for element in node.elts
                if isinstance(element, ast.Constant) and isinstance(element.value, str)
            }
            overlap = literals & _TOPOLOGY_NAMES
            assert len(overlap) < 2, f"{relative} enumerates topologies: {sorted(overlap)}"


def test_demo_rejects_an_unknown_template_with_a_branchable_code(tmp_path):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = main(["demo", str(tmp_path / "demo"), "--name", "not-a-template"])

    payload = json.loads(out.getvalue())
    assert code == 2
    assert payload["schema"] == "pe-sim.rejection.v1"
    assert payload["error_code"] == "invalid_request"

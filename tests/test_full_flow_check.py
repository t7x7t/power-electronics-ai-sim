"""End-to-end proof of ``scripts/full_flow_check.py`` (``docs/56``).

The check itself walks the documented flow; these tests run it twice in a
temporary directory and assert the contract: the exit-code mapping, the step
order, the required/optional outcome rules, and the machine-readable record.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = SOURCE_ROOT / "scripts" / "full_flow_check.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("full_flow_check", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # ``from __future__ import annotations`` makes dataclasses resolve string
    # annotations through ``sys.modules``, so the module must be registered
    # before it is executed.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


FLOW = _load_module()


def _record(output: Path) -> dict:
    path = output / "full-flow-check.json"
    assert path.is_file(), f"the check wrote no record at {path}"
    return json.loads(path.read_text(encoding="utf-8"))


def test_full_flow_end_to_end(tmp_path):
    output = tmp_path / "flow"
    exit_code = FLOW.main(["--output", str(output)])

    assert exit_code in (0, 2), f"the flow check returned {exit_code}"
    record = _record(output)
    assert record["schema_version"] == FLOW.FULL_FLOW_SCHEMA
    assert record["rule_version"] == FLOW.FULL_FLOW_RULE_VERSION
    assert record["outcome"] == ("pass" if exit_code == 0 else "partial")

    steps = record["steps"]
    assert tuple(step["id"] for step in steps) == FLOW.STEP_ORDER
    for step in steps:
        assert set(step) == {"id", "title", "required", "status", "detail", "evidence"}
        assert step["title"]
        if step["required"]:
            assert step["status"] == "pass", step
        else:
            assert step["status"] in {"pass", "skip"}, step
            if step["status"] == "skip":
                assert step["detail"].strip(), step

    run_dir = Path(record["workspace"]["run_dir"])
    assert run_dir.is_dir()
    assert (run_dir / "manifest.json").is_file()

    assert record["limitations"]
    assert (output / "workbench" / "topology.json").is_file()


def test_json_output_is_the_contract_envelope(tmp_path, capsys):
    output = tmp_path / "flow-json"
    exit_code = FLOW.main(["--output", str(output), "--json"])

    assert exit_code in (0, 2), f"the flow check returned {exit_code}"
    document = json.loads(capsys.readouterr().out)
    assert document["schema_version"] == "pe-sim.full-flow-check.v1"
    assert document["rule_version"] == "full-flow-check-v1"
    assert document["outcome"] in {"pass", "partial"}
    assert tuple(step["id"] for step in document["steps"]) == FLOW.STEP_ORDER

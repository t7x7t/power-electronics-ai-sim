"""Acceptance checks for the independent Stage 11 extension example."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "visualization_consumer" / "verify_contracts.py"
FIXTURE = ROOT / "workbench" / "public" / "fixtures" / "run-demo"


def test_stdlib_consumer_reads_three_published_contracts_without_pe_sim_import():
    text = EXAMPLE.read_text(encoding="utf-8")
    assert "import pe_sim" not in text
    result = subprocess.run(
        [sys.executable, str(EXAMPLE), "--directory", str(FIXTURE)],
        check=True,
        capture_output=True,
        text=True,
    )
    assert "run_id: workbench-buck-demo-20260913" in result.stdout
    assert "manifest_sha256:" in result.stdout
    assert "latest_values:" in result.stdout


def test_consumer_rejects_mismatched_contract_provenance():
    temporary_root = ROOT / ".tmp"
    temporary_root.mkdir(exist_ok=True)
    with TemporaryDirectory(dir=temporary_root) as name:
        directory = Path(name)
        for filename in ("topology.json", "trace.json", "outputs.json"):
            (directory / filename).write_bytes((FIXTURE / filename).read_bytes())
        trace_path = directory / "trace.json"
        trace = json.loads(trace_path.read_text(encoding="utf-8"))
        trace["provenance"]["run_id"] = "different-run"
        trace_path.write_text(json.dumps(trace), encoding="utf-8")
        result = subprocess.run(
            [sys.executable, str(EXAMPLE), "--directory", str(directory)],
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0
        assert "do not identify the same provenance.run_id" in result.stderr

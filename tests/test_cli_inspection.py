"""CLI entry points for read-only verification and netlist preview."""

from __future__ import annotations

import json
from pathlib import Path

from pe_sim.cli import main

from test_workbench_export import _published_buck_run

REPOSITORY = Path(__file__).resolve().parents[1]
NGSPICE_CONFIG = REPOSITORY / "examples" / "ngspice_buck_step" / "config.json"


def test_preview_netlist_renders_without_an_installed_ngspice(capsys):
    exit_code = main(["preview-netlist", str(NGSPICE_CONFIG), "--duty", "0.4"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "V1 vin 0 DC" in captured.out
    assert ".tran" in captured.out


def test_preview_netlist_json_includes_the_digest(capsys):
    exit_code = main(["preview-netlist", str(NGSPICE_CONFIG), "--duty", "0.4", "--json"])

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert exit_code == 0
    assert payload["status"] == "rendered"
    assert payload["backend"] == "ngspice-buck"
    assert len(payload["netlist_sha256"]) == 64
    assert "V1 vin 0 DC" in payload["netlist"]


def test_inspect_run_is_a_read_only_cli_command(tmp_path, capsys):
    run_dir, _, _ = _published_buck_run(tmp_path)

    exit_code = main(["inspect-run", str(run_dir), "--compact"])

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["status"] == "ok"
    assert payload["samples"]["count"] == 2

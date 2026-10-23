"""Process-boundary tests using a fake executable; no real ngspice required."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import sys

import pytest

from pe_sim.backends.process import (
    NgspiceExecutableAdapter,
    NgspiceProcess,
    parse_ngspice_version,
)
from pe_sim.contracts import BackendProcessError, BackendTimeoutError, NumericalNonconvergenceError


FIXTURE = Path(__file__).parent / "fixtures" / "ngspice" / "fake_ngspice.py"


def _wrapper(tmp_path: Path) -> Path:
    """Create a platform-native launcher for the fake ngspice script."""

    if os.name == "nt":
        wrapper = tmp_path / "fake_ngspice.cmd"
        wrapper.write_text(
            f'@echo off\r\n"{sys.executable}" "{FIXTURE}" %*\r\n',
            encoding="utf-8",
        )
        return wrapper
    wrapper = tmp_path / "fake_ngspice.sh"
    wrapper.write_text(
        f'#!/bin/sh\nexec "{sys.executable}" "{FIXTURE}" "$@"\n',
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    return wrapper


def test_success_returns_validated_table_and_hashes(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_NGSPICE_MODE", "success")
    process = NgspiceProcess(_wrapper(tmp_path), timeout_s=10.0)

    netlist = "* test\n.end\n"
    result = process.run(netlist, label="unit")

    assert result.returncode == 0
    assert result.table.point_count == 21
    assert result.table.end_time_s == pytest.approx(1e-6)
    assert result.signals["v(vout)"][-1] == pytest.approx(5.0)
    assert result.netlist_sha256 == hashlib.sha256(netlist.encode("utf-8")).hexdigest()
    assert result.output_sha256 and len(result.output_sha256) == 64
    assert result.log_sha256 and len(result.log_sha256) == 64
    assert result.duration_s > 0
    assert result.command[0].endswith(("fake_ngspice.cmd", "fake_ngspice.sh"))


def test_convergence_failure_is_classified(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_NGSPICE_MODE", "convergence")
    process = NgspiceProcess(_wrapper(tmp_path), timeout_s=10.0)

    with pytest.raises(NumericalNonconvergenceError) as caught:
        process.run("* test\n.end\n")
    assert caught.value.category == "numerical_nonconvergence"


def test_process_failure_is_classified(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_NGSPICE_MODE", "process_error")
    process = NgspiceProcess(_wrapper(tmp_path), timeout_s=10.0)

    with pytest.raises(BackendProcessError) as caught:
        process.run("* test\n.end\n")
    assert caught.value.category == "backend_process_failure"


def test_missing_output_and_garbage_output_fail_closed(tmp_path, monkeypatch):
    process = NgspiceProcess(_wrapper(tmp_path), timeout_s=10.0)

    monkeypatch.setenv("FAKE_NGSPICE_MODE", "no_output")
    with pytest.raises(BackendProcessError):
        process.run("* test\n.end\n")

    monkeypatch.setenv("FAKE_NGSPICE_MODE", "garbage")
    with pytest.raises(BackendProcessError):
        process.run("* test\n.end\n")


def test_timeout_is_classified(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_NGSPICE_MODE", "timeout")
    process = NgspiceProcess(_wrapper(tmp_path), timeout_s=0.3)

    with pytest.raises(BackendTimeoutError) as caught:
        process.run("* test\n.end\n")
    assert caught.value.category == "backend_timeout"


def test_missing_executable_is_rejected():
    process = NgspiceProcess("definitely-not-an-ngspice-binary", timeout_s=1.0)

    with pytest.raises(BackendProcessError):
        process.run("* test\n.end\n")


def test_keep_artifacts_preserves_the_work_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_NGSPICE_MODE", "success")
    work_root = tmp_path / "artifacts"
    process = NgspiceProcess(_wrapper(tmp_path), timeout_s=10.0, keep_artifacts=True, work_root=work_root)

    process.run("* test\n.end\n", label="kept")

    directories = [path for path in work_root.iterdir() if path.is_dir()]
    assert len(directories) == 1
    assert (directories[0] / "netlist.cir").is_file()
    assert (directories[0] / "output.dat").is_file()


def test_version_parsing_accepts_common_ngspice_output():
    assert parse_ngspice_version("** ngspice-44.2 : Circuit level simulation program") == "44.2"
    assert parse_ngspice_version("ngspice version 45") == "45"
    assert parse_ngspice_version("no version here") is None


def test_executable_adapter_reports_known_identity(tmp_path):
    adapter = NgspiceExecutableAdapter(_wrapper(tmp_path))

    evidence = adapter.backend_provenance()

    assert evidence["status"] == "known"
    assert evidence["version"] == "44.2"
    assert evidence["solver_settings"]["integration_method"] == "gear"
    assert evidence["executable_sha256"] and len(evidence["executable_sha256"]) == 64
    assert evidence["executable_name"] in {"fake_ngspice.cmd", "fake_ngspice.sh"}
    assert "executable" not in evidence


def test_executable_adapter_reports_unavailable_for_missing_binary():
    adapter = NgspiceExecutableAdapter("definitely-not-an-ngspice-binary")

    evidence = adapter.backend_provenance()

    assert evidence["status"] == "unavailable"
    assert evidence["version"] is None
    assert evidence["limitations"]


def test_convergence_failure_preserves_bounded_log_evidence(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_NGSPICE_MODE", "convergence")
    process = NgspiceProcess(_wrapper(tmp_path), timeout_s=10.0)

    with pytest.raises(NumericalNonconvergenceError) as caught:
        process.run("* test\n.end\n")

    evidence = caught.value.evidence
    assert evidence["netlist_sha256"]
    assert evidence["executable_name"] in {"fake_ngspice.cmd", "fake_ngspice.sh"}
    assert evidence["arguments"] == ["-b", "-o", "<log>", "<netlist>"]
    assert "timestep too small" in evidence["log_excerpt"]
    assert evidence["log_truncated"] is False

"""The versioned capability report and the ``pe-sim capabilities`` surface."""

from __future__ import annotations

import importlib.util as importlib_util
import json
import subprocess

import pytest

import pe_sim.capabilities as capabilities
from pe_sim.backends.discovery import (
    BackendConfigError,
    BackendNotFoundError,
    BackendResolution,
)
from pe_sim.cli import main
from pe_sim.error_codes import REJECTION_SCHEMA


EXPECTED_IDS = (
    "pyspice_netlist_builder",
    "external_ngspice_backend",
    "workbench_visualization_service",
    "attestation_anchor",
)

ENTRY_KEYS = {
    "capability_id",
    "description",
    "status",
    "provided_by",
    "install_hint",
    "limitations",
}


def _entry(report: dict, capability_id: str) -> dict:
    for item in report["capabilities"]:
        if item["capability_id"] == capability_id:
            return item
    raise AssertionError(f"{capability_id!r} is missing from {report['capabilities']!r}")


def _patch_pyspice_spec(monkeypatch, spec) -> None:
    """Patch ``importlib.util.find_spec`` only for the PySpice probe."""

    real_find_spec = importlib_util.find_spec

    def find_spec(name, *args, **kwargs):
        if name == "PySpice":
            return spec
        return real_find_spec(name, *args, **kwargs)

    monkeypatch.setattr(importlib_util, "find_spec", find_spec)


def test_schema_tag_and_closed_status_vocabulary():
    assert capabilities.CAPABILITY_SCHEMA == "pe-sim.capabilities.v1"
    assert capabilities.CAPABILITY_STATUSES == ("installed", "available", "unavailable")

    report = capabilities.capability_report()

    assert report["schema"] == capabilities.CAPABILITY_SCHEMA
    assert report["limitations"]
    for item in report["capabilities"]:
        assert set(item) == ENTRY_KEYS
        assert item["status"] in capabilities.CAPABILITY_STATUSES
        assert item["capability_id"]
        assert item["description"]
        assert item["provided_by"]
        assert item["install_hint"]
        assert isinstance(item["limitations"], list)


def test_capability_order_is_fixed_and_the_document_is_json_serializable():
    first = capabilities.capability_report()
    second = capabilities.capability_report()

    assert [item["capability_id"] for item in first["capabilities"]] == list(EXPECTED_IDS)
    assert [item["capability_id"] for item in second["capabilities"]] == list(EXPECTED_IDS)
    assert json.loads(json.dumps(first, sort_keys=True)) == first


def test_optional_capability_rejects_empty_ids_and_unknown_statuses():
    with pytest.raises(ValueError, match="capability_id"):
        capabilities.OptionalCapability("", "d", "installed", "p", "h", ())
    with pytest.raises(ValueError, match="capability_id"):
        capabilities.OptionalCapability("   ", "d", "installed", "p", "h", ())
    with pytest.raises(ValueError, match="status"):
        capabilities.OptionalCapability("x", "d", "ready", "p", "h", ())


def test_pyspice_capability_is_available_when_the_module_is_absent(monkeypatch):
    _patch_pyspice_spec(monkeypatch, None)

    entry = _entry(capabilities.capability_report(), "pyspice_netlist_builder")

    assert entry["status"] == "available"
    assert "requirements-pyspice-tested.txt" in entry["install_hint"]
    assert any("not a package extra" in item for item in entry["limitations"])
    assert any("excludes Python 3.13" in item for item in entry["limitations"])


def test_pyspice_capability_is_installed_when_the_module_is_present(monkeypatch):
    _patch_pyspice_spec(monkeypatch, object())

    entry = _entry(capabilities.capability_report(), "pyspice_netlist_builder")

    assert entry["status"] == "installed"
    assert any("not imported" in item for item in entry["limitations"])


def test_a_raising_probe_becomes_unavailable_without_propagating(monkeypatch):
    def broken_find_spec(name, *args, **kwargs):
        raise RuntimeError("probe exploded")

    monkeypatch.setattr(importlib_util, "find_spec", broken_find_spec)

    report = capabilities.capability_report()
    entry = _entry(report, "pyspice_netlist_builder")

    assert entry["status"] == "unavailable"
    assert any("RuntimeError" in item for item in entry["limitations"])
    # The other probes still run and report independently.
    assert _entry(report, "workbench_visualization_service")["status"] == "installed"


def test_ngspice_capability_uses_the_resolver_without_starting_a_process(monkeypatch):
    calls = []

    def missing_resolver(*args, **kwargs):
        calls.append((args, kwargs))
        raise BackendNotFoundError("ngspice", ["PATH: ngspice"])

    def no_process(*args, **kwargs):
        raise AssertionError("the capability report must not start a process")

    monkeypatch.setattr(capabilities, "resolve_ngspice_executable", missing_resolver)
    monkeypatch.setattr(subprocess, "run", no_process)
    monkeypatch.setattr(subprocess, "Popen", no_process)
    monkeypatch.setattr(subprocess, "call", no_process)

    entry = _entry(capabilities.capability_report(), "external_ngspice_backend")

    assert entry["status"] == "available"
    assert calls, "the report must use the reviewed discovery resolver"
    assert any("no executable was started" in item for item in entry["limitations"])
    assert "pe-sim backend doctor" in entry["install_hint"]


def test_ngspice_capability_is_unavailable_when_its_configuration_is_broken(monkeypatch):
    def broken_resolver(*args, **kwargs):
        raise BackendConfigError("backend configuration could not be read: broken")

    monkeypatch.setattr(capabilities, "resolve_ngspice_executable", broken_resolver)

    entry = _entry(capabilities.capability_report(), "external_ngspice_backend")

    assert entry["status"] == "unavailable"
    assert any("could not be read" in item for item in entry["limitations"])


def test_ngspice_capability_is_installed_when_the_resolver_finds_a_path(monkeypatch):
    resolution = BackendResolution(
        backend="ngspice",
        requested="ngspice_con.exe",
        executable="/fake/ngspice_con.exe",
        source="path",
    )
    monkeypatch.setattr(capabilities, "resolve_ngspice_executable", lambda *a, **k: resolution)

    entry = _entry(capabilities.capability_report(), "external_ngspice_backend")

    assert entry["status"] == "installed"
    assert any("via path" in item for item in entry["limitations"])
    assert any("version was not queried" in item for item in entry["limitations"])


def test_workbench_visualization_service_ships_with_the_package():
    entry = _entry(capabilities.capability_report(), "workbench_visualization_service")

    assert entry["status"] == "installed"
    assert any("does not start it" in item for item in entry["limitations"])


def test_attestation_anchor_follows_git_on_path(monkeypatch):
    real_which = capabilities.shutil.which

    monkeypatch.setattr(
        capabilities.shutil,
        "which",
        lambda name: None if name == "git" else real_which(name),
    )
    assert _entry(capabilities.capability_report(), "attestation_anchor")["status"] == "available"

    monkeypatch.setattr(
        capabilities.shutil,
        "which",
        lambda name: "/fake/git" if name == "git" else real_which(name),
    )
    assert _entry(capabilities.capability_report(), "attestation_anchor")["status"] == "installed"


def test_cli_capabilities_prints_the_versioned_document(capsys):
    assert main(["capabilities", "--compact"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["schema"] == capabilities.CAPABILITY_SCHEMA
    assert [item["capability_id"] for item in payload["capabilities"]] == list(EXPECTED_IDS)


def test_cli_require_installed_capability_returns_zero(capsys):
    exit_code = main(
        ["capabilities", "--require", "workbench_visualization_service", "--compact"]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["schema"] == capabilities.CAPABILITY_SCHEMA


def test_cli_require_uninstalled_capability_returns_one_and_prints_the_document(
    monkeypatch, capsys
):
    _patch_pyspice_spec(monkeypatch, None)

    exit_code = main(["capabilities", "--require", "pyspice_netlist_builder", "--compact"])

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert payload["schema"] == capabilities.CAPABILITY_SCHEMA
    assert _entry(payload, "pyspice_netlist_builder")["status"] == "available"


def test_cli_require_unknown_capability_is_an_invalid_request(capsys):
    exit_code = main(["capabilities", "--require", "not_a_capability", "--compact"])

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 2
    assert payload["schema"] == REJECTION_SCHEMA
    assert payload["status"] == "CONFIG_REJECTED"
    assert payload["error_code"] == "invalid_request"
    assert "not_a_capability" in payload["error"]
    assert "known_capabilities" in payload

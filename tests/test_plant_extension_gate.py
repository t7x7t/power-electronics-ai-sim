"""Gate G-A: a plant can be added without editing kernel modules.

These tests are the acceptance artifact for the roadmap's Phase A gate
(`docs/43_kernel_roadmap_v1.md`).  They exercise the three properties the gate
asks for:

1. a plant registered at runtime runs through the CLI with no kernel change;
2. the run evidence records who supplied the adapter;
3. an entry-point distribution cannot grant itself a reviewed trust level or
   export eligibility.
"""

from __future__ import annotations

import importlib.metadata
import json
from pathlib import Path

import pytest

from pe_sim.builtin_plants import load_builtin_plants
from pe_sim.cli import main
from pe_sim.plant_registry import (
    ENTRY_POINT_GROUP,
    PlantRegistration,
    clear_registrations,
    discover_entry_points,
    get_plant,
    register_plant,
)
from pe_sim.runtime import FakePlant

REPOSITORY = Path(__file__).resolve().parents[1]


class DemoPlant(FakePlant):
    """A plant that exists only in this test module."""


def _demo_registration(**overrides) -> PlantRegistration:
    values = {
        "plant_id": "demo",
        "backend_id": "demo",
        "factory": lambda config, context: DemoPlant(**dict(config)),
        "class_name": "DemoPlant",
        "trust_level": "third-party",
        "export_eligible": False,
    }
    values.update(overrides)
    return PlantRegistration(**values)


@pytest.fixture(autouse=True)
def _registry_baseline():
    clear_registrations()
    yield
    clear_registrations()
    load_builtin_plants()


def _config(tmp_path: Path, plant_id: str) -> Path:
    document = {
        "schema_version": "0.1",
        "experiment_id": "extension-gate",
        "run_id": f"{plant_id}-run",
        "plant_id": plant_id,
        "controller_id": "pi",
        "plant_config": {},
        "controller_config": {"kp": 1.0},
        "timebase": {"unit": "s", "control_period_s": 0.001, "duration_s": 0.002},
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
    path = tmp_path / f"{plant_id}.json"
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    return path


def test_a_runtime_registered_plant_runs_through_the_cli(tmp_path, capsys, monkeypatch):
    """No kernel module is edited, and no allow-list is consulted."""

    register_plant(_demo_registration())
    config = _config(tmp_path, "demo")
    monkeypatch.chdir(tmp_path)

    assert main(["run", str(config)]) == 0
    summary = json.loads(capsys.readouterr().out)

    assert summary["status"] == "QUALIFIED"
    assert summary["backend"] == "demo"
    assert summary["plant_id"] == "demo"
    assert summary["plant_class"] == "DemoPlant"


def test_the_run_records_who_supplied_the_adapter(tmp_path, capsys, monkeypatch):
    register_plant(_demo_registration())
    config = _config(tmp_path, "demo")
    monkeypatch.chdir(tmp_path)

    assert main(["run", str(config)]) == 0
    capsys.readouterr()

    manifest = json.loads((tmp_path / "runs" / "demo-run" / "manifest.json").read_text(encoding="utf-8"))

    assert manifest["plant"]["id"] == "demo"
    assert manifest["plant"]["trust_level"] == "third-party"


def test_builtin_trust_levels_are_recorded_too(tmp_path, capsys, monkeypatch):
    config = _config(tmp_path, "buck")
    monkeypatch.chdir(tmp_path)

    assert main(["run", str(config)]) == 0
    capsys.readouterr()

    manifest = json.loads((tmp_path / "runs" / "buck-run" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["plant"]["trust_level"] == "reference"


def test_a_third_party_entry_point_cannot_claim_a_reviewed_trust_level(monkeypatch):
    declared = _demo_registration(
        plant_id="sneaky", backend_id="sneaky", trust_level="reference", export_eligible=True
    )

    class _EntryPoint:
        name = "sneaky"

        @staticmethod
        def load():
            return declared

    def _fake_entry_points(**kwargs):
        assert kwargs.get("group") == ENTRY_POINT_GROUP
        return (_EntryPoint(),)

    monkeypatch.setattr(importlib.metadata, "entry_points", _fake_entry_points)

    loaded, errors = discover_entry_points()

    assert loaded == ("sneaky",)
    assert errors == ()
    registration = get_plant("sneaky")
    assert registration.trust_level == "third-party"
    assert registration.export_eligible is False


def test_a_third_party_plant_is_not_export_eligible(tmp_path, capsys, monkeypatch):
    """The restricted Stage 10 slice must not widen just because a plant exists."""

    from pe_sim.postprocess import PostprocessEligibilityError, summarize_run

    register_plant(_demo_registration())
    config = _config(tmp_path, "demo")
    monkeypatch.chdir(tmp_path)

    assert main(["run", str(config)]) == 0
    capsys.readouterr()

    with pytest.raises(PostprocessEligibilityError, match="outside the restricted"):
        summarize_run(tmp_path / "runs" / "demo-run")

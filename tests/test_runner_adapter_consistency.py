"""Python API adapter/registration consistency (P0-2).

``run_experiment`` is a thin facade over ``Runner.run``.  Before this gate, the
CLI-only protections (registered id, adapter class, backend, time model, and
schedule keys) could be bypassed by calling the Python API, so a run could
publish ``verified``/``comparable`` evidence for a model that never ran.

Two rules close that gap:

- ``ExperimentSpec.from_dict`` validates the JSON experiment schema first, so
  unknown or misspelled keys are rejected exactly as they are at the CLI.
- ``Runner.run`` cross-checks any plant that declares ``manifest_identity``
  against its registration before a run directory exists.

An ad-hoc adapter without ``manifest_identity`` keeps the historical
behaviour: it cannot claim a registered model identity, and its evidence can
never reach ``verified`` or ``comparable``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pe_sim import FakePIController, FakePlant, RunOptions
from pe_sim.contracts import ExperimentSpec, Timebase
from pe_sim.reference_plants import BoostPlant, BuckPlant
from pe_sim.runner import run_experiment
from pe_sim.runtime import Runner

_REPOSITORY = Path(__file__).resolve().parents[1]
_TEMPLATE = json.loads(
    (_REPOSITORY / "examples" / "buck_pi_step" / "config.json").read_text(
        encoding="utf-8"
    )
)


def _document(tmp_path: Path, run_id: str = "adapter-consistency", **overrides) -> dict:
    """Return the example experiment pointed at a per-test output directory."""

    document = json.loads(json.dumps(_TEMPLATE))
    document["run_id"] = run_id
    document["output"] = {"directory": str(tmp_path / "runs"), "retention": "keep"}
    document.update(overrides)
    return document


def _buck() -> BuckPlant:
    return BuckPlant(
        input_voltage_v=12.0,
        inductance_h=100e-6,
        capacitance_f=470e-6,
        load_resistance_ohm=10.0,
        integration_step_s=1e-6,
    )


def _boost() -> BoostPlant:
    return BoostPlant(
        input_voltage_v=12.0,
        inductance_h=100e-6,
        capacitance_f=470e-6,
        load_resistance_ohm=10.0,
        integration_step_s=1e-6,
    )


def test_a_mismatched_plant_id_is_refused_before_a_run_directory_exists(tmp_path):
    """``plant_id="ngspice-buck"`` with the averaged Buck is not a Buck run."""

    spec = ExperimentSpec.from_dict(
        _document(tmp_path, "label-mismatch", plant_id="ngspice-buck")
    )

    with pytest.raises(ValueError) as excinfo:
        run_experiment(
            spec,
            _buck(),
            FakePIController(kp=0.2),
            RunOptions(mode="formal_comparison"),
        )

    message = str(excinfo.value)
    assert "ngspice-buck" in message
    assert "NgspiceBuckPlant" in message and "BuckPlant" in message
    # Fail closed before the writer exists: no directory, no partial package.
    assert not (tmp_path / "runs").exists()


def test_from_dict_rejects_an_unknown_schedule_key(tmp_path):
    """The Python reader now rejects ``v_ref`` exactly like the CLI does."""

    document = _document(
        tmp_path, "schedule-typo", input_schedule=[{"time_s": 0.0, "v_ref": 0.5}]
    )

    with pytest.raises(ValueError) as excinfo:
        ExperimentSpec.from_dict(document)

    message = str(excinfo.value)
    assert "input_schedule.0" in message
    assert "Additional properties are not allowed" in message
    assert "v_ref" in message


def test_a_hand_built_spec_with_an_unknown_schedule_key_is_refused(tmp_path):
    """A spec constructed in Python skips the schema, so the runner checks too.

    The default (exploratory) mode is used deliberately: a silently ignored
    schedule key is never acceptable, not only for formal comparisons.
    """

    spec = ExperimentSpec(
        "adapter-consistency",
        "schedule-typo",
        "buck",
        "pi",
        Timebase(duration_s=1e-4, control_period_s=1e-5),
        input_schedule=({"time_s": 0.0, "v_ref": 0.5},),
        output_dir=str(tmp_path / "runs"),
        seed=0,
    )

    with pytest.raises(ValueError) as excinfo:
        Runner().run(spec, _buck(), FakePIController(kp=0.2))

    message = str(excinfo.value)
    assert "v_ref" in message
    assert "allowed" in message
    assert not (tmp_path / "runs" / "schedule-typo").exists()


def test_the_registered_buck_pair_still_runs(tmp_path):
    spec = ExperimentSpec.from_dict(_document(tmp_path, "buck-ok"))

    result = run_experiment(spec, _buck(), FakePIController(kp=0.2))

    assert result.status == "QUALIFIED"
    manifest = json.loads((result.run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["plant"]["id"] == "buck"
    assert manifest["plant"]["identity"]["topology"] == "buck"


def test_the_registered_fake_pair_still_runs(tmp_path):
    spec = ExperimentSpec.from_dict(
        _document(tmp_path, "fake-ok", plant_id="fake", plant_config={})
    )

    result = run_experiment(spec, FakePlant(), FakePIController(kp=0.2))

    assert result.status == "QUALIFIED"


def test_an_unregistered_id_is_refused_for_an_identity_declaring_plant(tmp_path):
    spec = ExperimentSpec.from_dict(
        _document(tmp_path, "unregistered", plant_id="laboratory-buck")
    )

    with pytest.raises(ValueError) as excinfo:
        run_experiment(spec, _buck(), FakePIController(kp=0.2))

    assert "laboratory-buck" in str(excinfo.value)
    assert not (tmp_path / "runs" / "unregistered").exists()


def test_an_ad_hoc_adapter_without_manifest_identity_keeps_working(tmp_path):
    """Deliberate boundary: no identity claim, no registration cross-check.

    An adapter that does not publish ``manifest_identity`` falls back to the
    generic ``runtime_component`` identity, so it can never reach
    ``verified``/``comparable`` evidence.  Requiring every in-process fixture
    to be registered would narrow the documented low-friction Python API
    without strengthening any evidence claim.
    """

    spec = ExperimentSpec.from_dict(
        _document(tmp_path, "ad-hoc", plant_id="laboratory-buck")
    )

    result = run_experiment(spec, FakePlant(), FakePIController(kp=0.2))

    assert result.status == "QUALIFIED"
    manifest = json.loads((result.run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["plant"]["identity"]["kind"] == "runtime_component"
    assert "verification" not in manifest["plant"]


def test_a_registered_id_rejects_a_different_plant_class(tmp_path):
    """``plant_id="buck"`` plus ``BoostPlant`` must not publish Buck evidence."""

    spec = ExperimentSpec.from_dict(_document(tmp_path, "class-mismatch"))

    with pytest.raises(ValueError) as excinfo:
        run_experiment(spec, _boost(), FakePIController(kp=0.2))

    message = str(excinfo.value)
    assert "'buck'" in message
    assert "BuckPlant" in message and "BoostPlant" in message
    assert not (tmp_path / "runs").exists()

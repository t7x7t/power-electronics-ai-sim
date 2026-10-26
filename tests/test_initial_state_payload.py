"""Contract and run-level tests for the bounded initial-state payload.

The payload closes ``docs/60`` row A6: a pre-biased converter start must be
expressible in the experiment document, recorded in the manifest, and either
applied exactly by the plant or rejected before the first advance.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

import pytest

from pe_sim import BuckPlant, ExperimentSpec, FakePIController, Timebase
from pe_sim.artifacts import canonical_json
from pe_sim.contracts import InitialState, validate_experiment_document
from pe_sim.runtime import FakePlant, Runner, _verify_initial_state_echo
from pe_sim.whole_run_route import _build_request


def _buck() -> BuckPlant:
    return BuckPlant(
        input_voltage_v=12.0,
        inductance_h=100e-6,
        capacitance_f=470e-6,
        load_resistance_ohm=10.0,
        integration_step_s=1e-6,
    )


def _spec(
    tmp_path: Path,
    run_id: str,
    *,
    initial_state: InitialState | None = None,
    plant_id: str = "buck",
) -> ExperimentSpec:
    return ExperimentSpec(
        "initial-state",
        run_id,
        plant_id,
        "pi",
        Timebase(duration_s=4e-4, control_period_s=1e-5),
        initial_state=initial_state or InitialState(),
        input_schedule=({"time_s": 0.0, "vref": 8.0},),
        output_dir=str(tmp_path / "runs"),
        seed=17,
    )


def _document(tmp_path: Path, *, initial_state: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "0.1",
        "experiment_id": "initial-state",
        "run_id": "initial-state-run",
        "plant_id": "buck",
        "controller_id": "pi",
        "plant_config": {},
        "controller_config": {"kp": 0.2},
        "timebase": {"unit": "s", "control_period_s": 1e-5, "duration_s": 4e-4},
        "initial_state": dict(initial_state),
        "input_schedule": [{"time_s": 0.0, "vref": 8.0}],
        "seed": 0,
        "contracts": {
            "safety": {"id": "s", "hash": "0" * 64},
            "qualification": {"id": "q", "hash": "0" * 64},
        },
        "output": {"directory": str(tmp_path / "runs"), "retention": "keep"},
    }


class _ForgetfulPlant(FakePlant):
    """Fake plant that accepts nothing and reports an empty applied state."""

    def __init__(self) -> None:
        super().__init__()
        self.advance_calls = 0

    def reset(self, initial_state: Mapping[str, Any]) -> Mapping[str, Any]:
        super().reset(initial_state)
        return {}

    def advance(
        self,
        action: float,
        duration: float,
        external_input: Mapping[str, Any] | None = None,
    ):
        self.advance_calls += 1
        return super().advance(action, duration, external_input)


def test_prebiased_start_is_the_first_sample_and_converges_toward_vref(tmp_path):
    """A6: the requested start appears in the first observation and is not cold."""

    plant = _buck()
    spec = _spec(
        tmp_path,
        "a6-prebiased",
        initial_state=InitialState(
            mode="warm_start",
            state={"output_voltage_v": 5.0, "inductor_current_a": 0.0},
        ),
    )

    result = Runner().run(spec, plant, FakePIController(kp=0.2))

    assert result.status == "QUALIFIED"
    samples = json.loads((result.run_dir / "samples.json").read_text(encoding="utf-8"))
    assert samples
    assert samples[0]["vout"] == pytest.approx(5.0, abs=1e-9)
    assert samples[-1]["vout"] > samples[0]["vout"]
    manifest = json.loads((result.run_dir / "manifest.json").read_text(encoding="utf-8"))
    # The payload changes only the initial condition; the kernel still owns the
    # clock and the windows, so the route's evidence cap is unchanged.
    assert manifest["route"]["control_location"] == "kernel"


def test_manifest_records_the_payload_and_its_canonical_digest(tmp_path):
    payload = {"output_voltage_v": 5.0, "inductor_current_a": 0.25}
    spec = _spec(
        tmp_path,
        "manifest-payload",
        initial_state=InitialState(mode="warm_start", state=payload),
    )

    result = Runner().run(spec, _buck(), FakePIController(kp=0.2))

    manifest = json.loads((result.run_dir / "manifest.json").read_text(encoding="utf-8"))
    recorded = manifest["initial_state"]
    assert recorded["state"] == payload
    assert list(recorded["state"]) == ["inductor_current_a", "output_voltage_v"]
    expected = hashlib.sha256(
        canonical_json({"inductor_current_a": 0.25, "output_voltage_v": 5.0})
    ).hexdigest()
    assert recorded["state_sha256"] == expected


def test_manifest_adds_no_payload_keys_without_a_payload(tmp_path):
    spec = _spec(tmp_path, "manifest-no-payload")

    result = Runner().run(spec, _buck(), FakePIController(kp=0.2))

    manifest = json.loads((result.run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert set(manifest["initial_state"]) == {"mode", "snapshot_path", "snapshot_hash"}


def test_ignored_payload_key_fails_before_the_first_advance_and_writes_no_samples(
    tmp_path,
):
    plant = _ForgetfulPlant()
    spec = _spec(
        tmp_path,
        "echo-failure",
        initial_state=InitialState(state={"value": 2.5}),
        plant_id="fake",
    )

    applied = plant.reset(spec.initial_state.reset_arguments())
    with pytest.raises(ValueError) as caught:
        _verify_initial_state_echo(spec.initial_state, applied, plant)

    message = str(caught.value)
    assert "_ForgetfulPlant" in message
    assert "value" in message
    assert plant.advance_calls == 0
    assert not (tmp_path / "samples.json").exists()


def test_runner_records_an_ignored_payload_key_as_a_failed_run(tmp_path):
    spec = _spec(
        tmp_path,
        "echo-runner-failure",
        initial_state=InitialState(state={"unaccepted_state": 1.0}),
    )

    result = Runner().run(spec, _buck(), FakePIController(kp=0.2))

    assert result.status == "RUN_FAILED"
    manifest = json.loads((result.run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert "unaccepted_state" in manifest["error"]
    samples = json.loads((result.run_dir / "samples.json").read_text(encoding="utf-8"))
    assert samples == []


def test_whole_run_request_receives_the_flattened_payload(tmp_path):
    spec = _spec(
        tmp_path,
        "whole-run-payload",
        initial_state=InitialState(state={"output_voltage_v": 4.0}),
        plant_id="whole-run-buck",
    )

    request = _build_request(spec)

    assert request.initial_state["output_voltage_v"] == 4.0
    assert "state" not in request.initial_state


@pytest.mark.parametrize(
    "state",
    [
        {"output_voltage_v": float("nan")},
        {"output_voltage_v": float("inf")},
        {"output_voltage_v": True},
        {"output_voltage_v": "5.0"},
        {"": 1.0},
        {"../x": 1.0},
        {"./x": 1.0},
        {"bad key": 1.0},
        {"x" * 65: 1.0},
        {"output_voltage_v": 10**400},
        {f"key_{index}": float(index) for index in range(17)},
        [("output_voltage_v", 5.0)],
        {"mode": 1.0},
        {"snapshot_path": 1.0},
        {"snapshot_hash": 1.0},
    ],
)
def test_initial_state_rejects_invalid_payload_entries(state):
    with pytest.raises((TypeError, ValueError)):
        InitialState(mode="warm_start", state=state)


def test_qualified_snapshot_rejects_an_explicit_payload():
    with pytest.raises(ValueError, match="qualified_snapshot"):
        InitialState(
            "qualified_snapshot",
            "snapshot.json",
            "a" * 64,
            {"output_voltage_v": 5.0},
        )


def test_payload_is_sorted_frozen_and_order_independent():
    source = {"output_voltage_v": 5.0, "inductor_current_a": 0.25}
    first = InitialState(mode="warm_start", state=source)
    second = InitialState(
        mode="warm_start",
        state={"inductor_current_a": 0.25, "output_voltage_v": 5.0},
    )

    assert list(first.state) == ["inductor_current_a", "output_voltage_v"]
    assert first.to_dict() == second.to_dict()
    assert first.to_dict()["state"] == source
    source["output_voltage_v"] = 9.0
    assert first.state["output_voltage_v"] == 5.0


def test_to_dict_keeps_the_payload_nested_while_reset_arguments_flatten_it():
    state = InitialState(
        mode="warm_start", state={"output_voltage_v": 5.0, "inductor_current_a": 0.0}
    )

    assert state.to_dict() == {
        "mode": "warm_start",
        "snapshot_path": None,
        "snapshot_hash": None,
        "state": {"inductor_current_a": 0.0, "output_voltage_v": 5.0},
    }
    assert state.reset_arguments() == {
        "inductor_current_a": 0.0,
        "output_voltage_v": 5.0,
        "mode": "warm_start",
        "snapshot_path": None,
        "snapshot_hash": None,
    }


def test_absent_payload_keeps_the_legacy_document_shape():
    default = InitialState()

    assert default.to_dict() == {
        "mode": "cold_start",
        "snapshot_path": None,
        "snapshot_hash": None,
    }
    assert "state" not in default.to_dict()
    assert default.reset_arguments() == default.to_dict()
    assert json.dumps(default.to_dict(), sort_keys=True) == (
        '{"mode": "cold_start", "snapshot_hash": null, "snapshot_path": null}'
    )


def test_schema_accepts_a_bounded_payload(tmp_path):
    document = _document(
        tmp_path,
        initial_state={
            "mode": "warm_start",
            "state": {"output_voltage_v": 5.0, "inductor_current_a": 0.0},
        },
    )

    validate_experiment_document(document)


@pytest.mark.parametrize(
    "state",
    [
        {"output_voltage_v": True},
        {"bad key": 1.0},
        {"../x": 1.0},
        {f"key_{index}": float(index) for index in range(17)},
    ],
)
def test_schema_rejects_invalid_payloads(tmp_path, state):
    document = _document(
        tmp_path, initial_state={"mode": "warm_start", "state": state}
    )

    with pytest.raises(ValueError, match="schema validation"):
        validate_experiment_document(document)


def test_schema_rejects_a_payload_on_a_qualified_snapshot(tmp_path):
    document = _document(
        tmp_path,
        initial_state={
            "mode": "qualified_snapshot",
            "snapshot_path": "snapshot.json",
            "snapshot_hash": "a" * 64,
            "state": {"output_voltage_v": 5.0},
        },
    )

    with pytest.raises(ValueError, match="schema validation"):
        validate_experiment_document(document)


def test_from_dict_round_trips_the_payload(tmp_path):
    document = _document(
        tmp_path,
        initial_state={
            "mode": "warm_start",
            "state": {"output_voltage_v": 5.0, "inductor_current_a": 0.0},
        },
    )

    spec = ExperimentSpec.from_dict(document)

    assert spec.initial_state.state == {
        "inductor_current_a": 0.0,
        "output_voltage_v": 5.0,
    }
    assert list(spec.initial_state.state) == [
        "inductor_current_a",
        "output_voltage_v",
    ]
    assert spec.initial_state.reset_arguments()["output_voltage_v"] == 5.0
    assert spec.to_dict()["initial_state"] == {
        "mode": "warm_start",
        "snapshot_path": None,
        "snapshot_hash": None,
        "state": {"inductor_current_a": 0.0, "output_voltage_v": 5.0},
    }

"""B2: progress events and cooperative cancellation are first-class."""

from __future__ import annotations

import json

import pytest

from pe_sim.cancellation import CallbackCancelToken, FileCancelToken
from pe_sim.contracts import ExperimentSpec, Timebase
from pe_sim.run_events import (
    EVENT_RUN_FINISHED,
    EVENT_RUN_PROGRESS,
    EVENT_RUN_STARTED,
    RUN_EVENT_SCHEMA,
    ProgressPolicy,
    RunEvent,
    RunEventEmitter,
    progress_due,
)
from pe_sim.runner import RunOptions
from pe_sim.runtime import FakePIController, FakePlant, Runner


def _spec(tmp_path, run_id="events", *, duration=0.006, period=0.001):
    return ExperimentSpec(
        "stage-b2",
        run_id,
        "fake",
        "pi",
        Timebase(duration_s=duration, control_period_s=period),
        output_dir=str(tmp_path),
    )


def test_run_event_is_schema_tagged_and_json_safe():
    event = RunEvent(3, EVENT_RUN_PROGRESS, "run-1", {"time_s": float("nan"), "fraction": 0.5})
    payload = event.to_dict()

    assert payload["schema"] == RUN_EVENT_SCHEMA
    assert payload["sequence"] == 3
    assert payload["event"] == EVENT_RUN_PROGRESS
    assert payload["run_id"] == "run-1"
    assert payload["data"] == {"time_s": None, "fraction": 0.5}
    json.dumps(payload)  # must be serialisable without a custom encoder


def test_emitter_orders_deliveries_and_survives_a_broken_sink():
    delivered: list[dict] = []
    emitter = RunEventEmitter(delivered.append)

    assert emitter.emit(EVENT_RUN_STARTED, "run-1", {"plant_id": "fake"}) is True
    assert emitter.emit(EVENT_RUN_PROGRESS, "run-1", {"step_index": 0}) is True
    assert [item["sequence"] for item in delivered] == [0, 1]
    assert emitter.delivered == 2 and emitter.dropped == 0

    def broken(_event):
        raise RuntimeError("client went away")

    broken_emitter = RunEventEmitter(broken)
    assert broken_emitter.emit(EVENT_RUN_STARTED, "run-2", {}) is False
    assert broken_emitter.sequence == 1, "the attempt still consumes a sequence number"
    assert broken_emitter.delivered == 0
    assert broken_emitter.dropped == 1


def test_disabled_emitter_consumes_no_sequence():
    emitter = RunEventEmitter()

    assert emitter.emit(EVENT_RUN_STARTED, "run-1", {}) is False
    assert emitter.sequence == 0
    assert emitter.delivered == 0 and emitter.dropped == 0


def test_progress_due_is_a_pure_rate_limit_decision():
    assert not progress_due(sample_count=0, interval_steps=2)
    assert not progress_due(sample_count=1, interval_steps=2)
    assert progress_due(sample_count=2, interval_steps=2)
    assert progress_due(sample_count=6, interval_steps=2, interval_s=1.0)

    assert progress_due(
        sample_count=5, interval_s=0.5, wall_time_s=1.0, last_wall_time_s=0.4
    )
    assert not progress_due(
        sample_count=5, interval_s=0.5, wall_time_s=0.8, last_wall_time_s=0.4
    )
    assert progress_due(sample_count=1, interval_s=0.5, wall_time_s=0.0)
    assert not progress_due(sample_count=1), "no interval means no periodic event"


def test_progress_policy_rejects_invalid_intervals():
    with pytest.raises(TypeError):
        ProgressPolicy(sink=None)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        ProgressPolicy(sink=lambda _event: None, interval_steps=0)
    with pytest.raises(TypeError):
        ProgressPolicy(sink=lambda _event: None, interval_steps=True)
    with pytest.raises(ValueError):
        ProgressPolicy(sink=lambda _event: None, interval_s=0.0)


def test_run_streams_started_progress_finished_in_order(tmp_path):
    events: list[dict] = []
    policy = ProgressPolicy(sink=events.append, interval_steps=2, interval_s=None)

    result = Runner().run(
        _spec(tmp_path), FakePlant(), FakePIController(), options=RunOptions(progress=policy)
    )

    names = [event["event"] for event in events]
    assert result.status == "QUALIFIED"
    assert names[0] == EVENT_RUN_STARTED
    assert names[-1] == EVENT_RUN_FINISHED
    assert names.count(EVENT_RUN_PROGRESS) == 3  # six steps reported every second step
    progress = [event for event in events if event["event"] == EVENT_RUN_PROGRESS]
    assert [event["data"]["sample_count"] for event in progress] == [2, 4, 6]
    assert progress[-1]["data"]["fraction"] == 1.0
    assert all(event["schema"] == RUN_EVENT_SCHEMA for event in events)
    assert [event["sequence"] for event in events] == list(range(len(events)))

    finished = events[-1]["data"]
    assert finished["status"] == "QUALIFIED"
    assert finished["run_dir"] == str(result.run_dir)
    assert finished["evidence_level"] == result.evidence_level
    assert finished["sample_count"] == 6
    assert finished["failure_category"] is None


def test_progress_is_neither_written_nor_trusted_as_evidence(tmp_path):
    events: list[dict] = []
    policy = ProgressPolicy(sink=events.append, interval_steps=1, interval_s=None)

    result = Runner().run(
        _spec(tmp_path), FakePlant(), FakePIController(), options=RunOptions(progress=policy)
    )

    metrics = json.loads((result.run_dir / "metrics.json").read_text(encoding="utf-8"))
    # The terminal event is emitted after metrics.json is sealed, so the count
    # recorded in the package covers started + progress only.
    assert metrics["progress_events_emitted"] == len(events) - 1
    assert metrics["progress_events_dropped"] == 0

    manifest = json.loads((result.run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert not [name for name in manifest["artifacts"] if "progress" in name]
    assert manifest["status"] == "QUALIFIED"


def test_a_broken_progress_sink_cannot_change_the_run(tmp_path):
    def broken(_event):
        raise RuntimeError("client disconnected")

    result = Runner().run(
        _spec(tmp_path),
        FakePlant(),
        FakePIController(),
        options=RunOptions(
            progress=ProgressPolicy(sink=broken, interval_steps=1, interval_s=None)
        ),
    )

    assert result.status == "QUALIFIED"
    samples = json.loads((result.run_dir / "samples.json").read_text(encoding="utf-8"))
    assert len(samples) == 6
    metrics = json.loads((result.run_dir / "metrics.json").read_text(encoding="utf-8"))
    assert metrics["progress_events_dropped"] >= 1
    assert metrics["progress_events_emitted"] == 0


def test_cancellation_token_stops_at_the_next_window_boundary(tmp_path):
    calls = {"count": 0}

    def request_cancel():
        calls["count"] += 1
        return calls["count"] > 2

    events: list[dict] = []
    options = RunOptions(
        cancellation=CallbackCancelToken(request_cancel),
        progress=ProgressPolicy(sink=events.append, interval_steps=1, interval_s=None),
    )

    result = Runner().run(_spec(tmp_path), FakePlant(), FakePIController(), options=options)

    manifest = json.loads((result.run_dir / "manifest.json").read_text(encoding="utf-8"))
    samples = json.loads((result.run_dir / "samples.json").read_text(encoding="utf-8"))
    assert result.status == "INCOMPLETE"
    assert manifest["failure"]["category"] == "cancelled"
    assert manifest["failure"]["recoverable"] is True
    assert len(samples) == 2
    assert (result.run_dir / "checkpoint.json").exists()
    assert events[-1]["event"] == EVENT_RUN_FINISHED
    assert events[-1]["data"]["status"] == "INCOMPLETE"
    assert events[-1]["data"]["failure_category"] == "cancelled"
    assert events[-1]["data"]["sample_count"] == 2


def test_pre_cancelled_run_publishes_an_empty_recoverable_package(tmp_path):
    result = Runner().run(
        _spec(tmp_path),
        FakePlant(),
        FakePIController(),
        options=RunOptions(cancellation=CallbackCancelToken(lambda: True)),
    )

    manifest = json.loads((result.run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert result.status == "INCOMPLETE"
    assert manifest["failure"]["category"] == "cancelled"
    assert manifest["qualification"]["reasons"] == ["incomplete", "cancelled"]
    assert json.loads((result.run_dir / "samples.json").read_text(encoding="utf-8")) == []
    assert (result.run_dir / "checkpoint.json").exists()


def test_file_token_reads_presence_without_mutating_the_request(tmp_path):
    request = tmp_path / "cancel.request"
    token = FileCancelToken(request)

    assert token.cancelled() is False
    request.write_text("cancel\n", encoding="utf-8")
    assert token.cancelled() is True
    assert request.exists(), "the token must not consume the client's request"

    result = Runner().run(
        _spec(tmp_path, "file-token"),
        FakePlant(),
        FakePIController(),
        options=RunOptions(cancellation=token),
    )
    assert result.status == "INCOMPLETE"
    assert request.exists()


def test_run_options_reject_invalid_interaction_surfaces(tmp_path):
    spec = _spec(tmp_path)

    with pytest.raises(TypeError, match="ProgressPolicy"):
        RunOptions(progress="stream").validate(spec)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="cancelled"):
        RunOptions(cancellation=object()).validate(spec)  # type: ignore[arg-type]


def test_run_result_reports_the_level_recorded_in_the_manifest(tmp_path):
    result = Runner().run(_spec(tmp_path), FakePlant(), FakePIController())
    manifest = json.loads((result.run_dir / "manifest.json").read_text(encoding="utf-8"))

    assert result.evidence_level == manifest["evidence_level"]
    assert result.evidence_level in {"diagnostic_only", "functional", "limited", "comparable"}

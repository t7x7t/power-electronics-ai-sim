"""Contract tests for time ownership, trace cadence, and the whole-run boundary."""

from __future__ import annotations

import math

import pytest

from pe_sim.time_model import (
    RUNTIME_EVIDENCE_LEVELS,
    TIME_MODEL_STEPPED,
    TIME_MODEL_WHOLE_RUN,
    TIME_MODELS,
    TRACE_CONTRACT_SCHEMA,
    TraceContract,
    WholeRunPlant,
    WholeRunRequest,
    WholeRunResult,
    cap_evidence_level,
    evidence_cap,
    formal_comparison_allowed,
    route_limitations,
    validate_whole_run_result,
)

CADENCE_S = 1e-3
DURATION_S = 5e-3
SIGNAL = "output_voltage_v"


def _times(
    cadence_s: float = CADENCE_S, duration_s: float = DURATION_S
) -> list[float]:
    count = int(round(duration_s / cadence_s)) + 1
    return [index * cadence_s for index in range(count)]


def _result(
    times: list[float] | None = None,
    signals: dict[str, list[float]] | None = None,
) -> WholeRunResult:
    if times is None:
        times = _times()
    if signals is None:
        signals = {SIGNAL: [0.1 * index for index in range(len(times))]}
    return WholeRunResult(time_s=times, signals=signals)


def _problems(
    result: WholeRunResult, *, duration_s: float = DURATION_S
) -> tuple[str, ...]:
    return validate_whole_run_result(
        result, TraceContract(cadence_s=CADENCE_S), duration_s=duration_s
    )


def test_valid_trace_passes_validation_with_exact_coverage():
    result = _result()
    assert result.time_s[0] == 0.0
    assert result.time_s[-1] == pytest.approx(DURATION_S, abs=1e-12)
    assert _problems(result) == ()


def test_valid_trace_accepts_multiple_signals_and_final_state():
    times = _times()
    result = WholeRunResult(
        time_s=times,
        signals={
            SIGNAL: [1.0 for _ in times],
            "inductor_current_a": [0.5 for _ in times],
        },
        final_state={SIGNAL: 1.0},
        metadata={"note": "post-hoc"},
    )
    assert _problems(result) == ()


def test_non_monotonic_time_is_reported():
    times = _times()
    times[3] = times[2]
    problems = _problems(_result(times=times))
    assert any("strictly increasing" in problem for problem in problems)


def test_non_finite_time_is_reported():
    times = _times()
    times[2] = math.nan
    problems = _problems(_result(times=times))
    assert any("time_s[2]" in problem and "finite" in problem for problem in problems)


def test_non_finite_signal_is_reported():
    times = _times()
    samples = [0.5 for _ in times]
    samples[4] = math.inf
    problems = _problems(_result(times=times, signals={SIGNAL: samples}))
    assert any(SIGNAL in problem and "finite" in problem for problem in problems)


def test_length_mismatch_is_reported():
    times = _times()
    short = [0.0] * (len(times) - 1)
    problems = _problems(_result(times=times, signals={SIGNAL: short}))
    expected = f"has {len(times) - 1} samples but time_s has {len(times)}"
    assert any(expected in problem for problem in problems)


def test_late_start_is_reported():
    offset = 0.5 * CADENCE_S
    times = [value + offset for value in _times()]
    problems = _problems(_result(times=times), duration_s=DURATION_S + offset)
    assert any("late start" in problem for problem in problems)


def test_short_end_is_reported():
    times = _times()[:-1]
    problems = _problems(_result(times=times))
    assert any("short end" in problem for problem in problems)


def test_gap_larger_than_cadence_is_reported():
    times = [0.0, 0.001, 0.003, 0.004, 0.005]
    problems = _problems(_result(times=times))
    assert any("gap" in problem for problem in problems)


def test_mapping_is_not_accepted_as_a_sample_sequence():
    problems = _problems(
        WholeRunResult(time_s={0.0: 0.0, 0.001: 0.1}, signals={SIGNAL: {0: 0.0, 1: 0.1}})
    )
    assert any("time_s must be a sequence" in problem for problem in problems)


def test_oversized_integer_is_reported_not_raised():
    times = _times()
    times[2] = 10**400
    problems = _problems(_result(times=times))
    assert any("time_s[2]" in problem for problem in problems)


def test_object_without_a_working_length_is_reported_not_raised():
    class _NoLength:
        def __len__(self):
            raise TypeError("no length")

        def __getitem__(self, index):
            return 0.0

    problems = _problems(WholeRunResult(time_s=_NoLength(), signals={SIGNAL: [0.0]}))
    assert any("time_s must be a sequence" in problem for problem in problems)


def test_trace_contract_rejects_an_unknown_schema():
    with pytest.raises(ValueError, match="schema"):
        TraceContract(cadence_s=CADENCE_S, schema="pe-sim.trace-contract.v2")


def test_boolean_sample_is_rejected_as_not_a_real_number():
    times = _times()
    times[1] = True  # type: ignore[assignment]
    problems = _problems(_result(times=times))
    assert any("time_s[1]" in problem and "bool" in problem for problem in problems)


def test_empty_result_reports_missing_samples_and_signals():
    problems = _problems(WholeRunResult(time_s=[], signals={}))
    assert any("at least one sample" in problem for problem in problems)
    assert any("at least one signal" in problem for problem in problems)


def test_validation_rejects_invalid_arguments():
    contract = TraceContract(cadence_s=CADENCE_S)
    result = _result()
    for duration in (0.0, -1.0, math.nan):
        with pytest.raises(ValueError):
            validate_whole_run_result(result, contract, duration_s=duration)
    with pytest.raises(ValueError):
        validate_whole_run_result(object(), contract, duration_s=DURATION_S)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        validate_whole_run_result(result, object(), duration_s=DURATION_S)  # type: ignore[arg-type]


@pytest.mark.parametrize("cadence", [0.0, -1e-3, math.nan, math.inf, True])
def test_trace_contract_rejects_invalid_cadence(cadence):
    with pytest.raises(ValueError):
        TraceContract(cadence_s=cadence)  # type: ignore[arg-type]


@pytest.mark.parametrize("tolerance", [-1e-9, math.nan, math.inf, False])
def test_trace_contract_rejects_invalid_tolerance(tolerance):
    with pytest.raises(ValueError):
        TraceContract(cadence_s=CADENCE_S, coverage_tolerance_s=tolerance)  # type: ignore[arg-type]


def test_trace_contract_defaults_and_serialization():
    contract = TraceContract(cadence_s=CADENCE_S)
    assert contract.schema == TRACE_CONTRACT_SCHEMA
    assert contract.coverage_tolerance_s == 1e-9
    assert contract.to_dict() == {
        "schema": TRACE_CONTRACT_SCHEMA,
        "cadence_s": CADENCE_S,
        "coverage_tolerance_s": 1e-9,
    }


@pytest.mark.parametrize("duration", [0.0, -5e-3, math.nan, math.inf, True])
def test_whole_run_request_rejects_invalid_duration(duration):
    with pytest.raises(ValueError):
        WholeRunRequest(duration_s=duration)  # type: ignore[arg-type]


def test_whole_run_request_rejects_negative_seed():
    with pytest.raises(ValueError):
        WholeRunRequest(duration_s=DURATION_S, seed=-1)


def test_whole_run_request_defaults_and_serialization():
    request = WholeRunRequest(duration_s=DURATION_S)
    assert request.seed is None
    assert request.initial_state == {}
    assert request.metadata == {}
    assert request.to_dict() == {
        "duration_s": DURATION_S,
        "initial_state": {},
        "seed": None,
        "metadata": {},
    }

    seeded = WholeRunRequest(
        duration_s=DURATION_S,
        initial_state={SIGNAL: 1.0},
        seed=0,
        metadata={"tag": "x"},
    )
    assert seeded.seed == 0
    assert seeded.to_dict()["initial_state"] == {SIGNAL: 1.0}
    assert seeded.to_dict()["metadata"] == {"tag": "x"}


def test_constants_are_stable():
    assert TIME_MODELS == (TIME_MODEL_STEPPED, TIME_MODEL_WHOLE_RUN)
    assert RUNTIME_EVIDENCE_LEVELS == ("diagnostic_only", "functional", "limited", "comparable")
    assert TRACE_CONTRACT_SCHEMA == "pe-sim.trace-contract.v1"


def test_evidence_cap_for_each_route():
    assert evidence_cap(TIME_MODEL_STEPPED) == "comparable"
    assert evidence_cap(TIME_MODEL_WHOLE_RUN) == "limited"


@pytest.mark.parametrize("time_model", ["event_driven", "", None])
def test_evidence_cap_rejects_unknown_route(time_model):
    with pytest.raises(ValueError):
        evidence_cap(time_model)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("level", "time_model", "expected"),
    [
        ("comparable", TIME_MODEL_STEPPED, "comparable"),
        ("limited", TIME_MODEL_STEPPED, "limited"),
        ("functional", TIME_MODEL_STEPPED, "functional"),
        ("diagnostic_only", TIME_MODEL_STEPPED, "diagnostic_only"),
        ("comparable", TIME_MODEL_WHOLE_RUN, "limited"),
        ("limited", TIME_MODEL_WHOLE_RUN, "limited"),
        ("functional", TIME_MODEL_WHOLE_RUN, "functional"),
        ("diagnostic_only", TIME_MODEL_WHOLE_RUN, "diagnostic_only"),
    ],
)
def test_cap_evidence_level(level, time_model, expected):
    assert cap_evidence_level(level, time_model) == expected


def test_cap_evidence_level_rejects_unknown_values():
    with pytest.raises(ValueError):
        cap_evidence_level("mechanism", TIME_MODEL_STEPPED)
    with pytest.raises(ValueError):
        cap_evidence_level("comparable", "event_driven")


def test_formal_comparison_allowed_only_for_stepped():
    assert formal_comparison_allowed(TIME_MODEL_STEPPED) is True
    assert formal_comparison_allowed(TIME_MODEL_WHOLE_RUN) is False
    assert formal_comparison_allowed("event_driven") is False


def test_route_limitations():
    assert route_limitations(TIME_MODEL_STEPPED) == ()
    assert route_limitations(TIME_MODEL_WHOLE_RUN) == (
        "in_run_enforcement_unavailable",
        "post_hoc_validation_only",
    )


def test_route_limitations_rejects_unknown_route():
    with pytest.raises(ValueError):
        route_limitations("event_driven")


def test_cap_evidence_level_whole_run_never_exceeds_limited():
    assert cap_evidence_level("comparable", TIME_MODEL_WHOLE_RUN) == "limited"
    assert cap_evidence_level("functional", TIME_MODEL_WHOLE_RUN) == "functional"


def test_whole_run_protocol_is_structural():
    class _Stub:
        time_model = TIME_MODEL_WHOLE_RUN

        def trace_contract(self) -> TraceContract:
            return TraceContract(cadence_s=CADENCE_S)

        def run_whole(self, request: WholeRunRequest) -> WholeRunResult:
            return _result()

    plant: WholeRunPlant = _Stub()
    assert plant.time_model == TIME_MODEL_WHOLE_RUN
    assert plant.trace_contract().cadence_s == CADENCE_S
    assert plant.run_whole(WholeRunRequest(duration_s=DURATION_S)).time_s[0] == 0.0

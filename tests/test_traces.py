import json
import pytest

from pe_sim import (
    SignalDefinition,
    SignalRegistry,
    SamplingPolicy,
    TraceCollector,
    TraceDataset,
    TraceProvenance,
)


def test_registry_and_dataset_round_trip():
    registry = SignalRegistry()
    registry.register_signal("plant.vout", name="Output voltage", unit="V", source="measurement")
    registry.register_signal("controller.duty", name="Duty", unit="normalized", source="action")
    collector = TraceCollector(
        registry,
        provenance=TraceProvenance(run_id="run-1", manifest_sha256="a" * 64, package_sha256="b" * 64, source_commit="deadbeef"),
    )
    assert collector.record(0.0, {"plant.vout": 12.0, "controller.duty": 0.5})
    assert collector.record(0.1, {"plant.vout": 11.8})
    dataset = collector.dataset()
    restored = TraceDataset.from_json(dataset.to_json())
    assert restored.to_dict() == dataset.to_dict()
    assert restored.provenance.package_sha256 == "b" * 64


def test_unknown_nonfinite_and_nonmonotonic_inputs_fail_closed():
    registry = SignalRegistry([SignalDefinition("vout", "Vout", "V", source="measurement")])
    collector = TraceCollector(registry)
    with pytest.raises(ValueError, match="unregistered"):
        collector.record(0.0, {"unknown": 1.0})
    with pytest.raises(ValueError, match="finite"):
        collector.record(0.0, {"vout": float("nan")})
    collector.record(1.0, {"vout": 1.0})
    with pytest.raises(ValueError, match="monotonic"):
        collector.record(0.5, {"vout": 1.0})


def test_sampling_modes_are_explicit_and_drops_are_reported():
    registry = SignalRegistry([SignalDefinition("v", "V", "V")])
    fixed = TraceCollector(registry, sampling_policy=SamplingPolicy(mode="fixed_interval", interval_s=1.0, max_points=2))
    assert fixed.record(0.0, {"v": 0.0})
    assert not fixed.record(0.2, {"v": 0.2})
    assert fixed.record(1.0, {"v": 1.0})
    assert not fixed.record(2.0, {"v": 2.0})
    assert fixed.dataset().metadata["dropped_count"] == 1

    events = TraceCollector(registry, sampling_policy=SamplingPolicy(mode="event", event_types=("fault",)))
    assert not events.record(0.0, {"v": 0.0})
    assert events.record(0.5, {"v": 0.5}, event="fault")


def test_truth_is_opt_in_and_signal_units_are_declared():
    class Observation:
        time_s = 0.0
        measurement = {"vout": 2.0}
        truth = {"vout": 2.0, "secret": 9.0}

    registry = SignalRegistry([
        SignalDefinition("vout", "Output", "V", source="measurement"),
        SignalDefinition("secret", "Secret", "x", source="truth"),
    ])
    collector = TraceCollector(registry)
    collector.record_observation(Observation())
    assert collector.samples[0].values == {"vout": 2.0}
    collector.record_observation(Observation(), include_truth=True)
    assert collector.samples[1].values["secret"] == 9.0


def test_schema_rejects_future_version():
    with pytest.raises(ValueError, match="unsupported"):
        TraceDataset.from_dict({"schema_version": "2.0", "signals": [], "samples": []})


def test_record_to_sink_is_explicit_opt_in():
    registry = SignalRegistry([SignalDefinition("v", "Voltage", "V")])
    collector = TraceCollector(registry)
    received = []

    class Sink:
        def publish(self, dataset):
            received.append(dataset)

    assert collector.record_to_sink(0.0, {"v": 1.0})
    assert not received
    assert collector.record_to_sink(1.0, {"v": 2.0}, Sink())
    assert received[0].samples[-1].values == {"v": 2.0}

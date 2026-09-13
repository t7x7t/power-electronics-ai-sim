"""Joint acceptance checks for the Stage 11 renderer-neutral data core."""

from __future__ import annotations

import json

import pytest

from pe_sim import (
    BuckPlant,
    OutputCollector,
    OutputDefinition,
    OutputRegistry,
    SignalDefinition,
    SignalRegistry,
    TraceCollector,
    TraceProvenance,
    buck_topology_descriptor,
)


def _provenance() -> TraceProvenance:
    return TraceProvenance(
        run_id="buck-stage11",
        manifest_sha256="a" * 64,
        package_sha256="b" * 64,
        source_commit="stage11-test",
        producer="stage11.acceptance",
    )


def test_buck_reference_can_publish_all_three_data_planes_without_mutating_model():
    plant = BuckPlant(
        input_voltage_v=24.0,
        inductance_h=100e-6,
        capacitance_f=100e-6,
        load_resistance_ohm=4.0,
    )
    before = plant.manifest_identity()
    topology = buck_topology_descriptor(plant)

    signals = SignalRegistry(
        [
            SignalDefinition("plant.vout", "Output voltage", "V", source="measurement"),
            SignalDefinition("plant.inductor_current", "Inductor current", "A", source="measurement"),
        ]
    )
    trace = TraceCollector(signals, provenance=_provenance())
    plant.reset({})
    observation = plant.advance(0.5, 1e-5)
    assert trace.record(observation.time_s, {
        "plant.vout": observation.measurement["vout"],
        "plant.inductor_current": observation.measurement["inductor_current"],
    })

    outputs = OutputCollector(
        OutputRegistry(
            [OutputDefinition("vout.final", "Final output voltage", "scalar", source="postprocess", unit="V")]
        ),
        provenance=_provenance(),
    )
    outputs.publish("vout.final", observation.measurement["vout"])

    assert topology.fingerprint
    assert trace.dataset().provenance.run_id == "buck-stage11"
    assert outputs.dataset().get("vout.final").payload == pytest.approx(observation.measurement["vout"])
    assert plant.manifest_identity() == before


def test_output_provenance_tampering_is_fail_closed():
    collector = OutputCollector(
        OutputRegistry([OutputDefinition("metric", "Metric", "scalar", source="test", unit="1")]),
        provenance=_provenance(),
    )
    collector.publish("metric", 1.0)
    document = json.loads(collector.dataset().to_json())
    document["provenance"]["run_id"] = "other-run"
    with pytest.raises(ValueError, match="fingerprint"):
        type(collector.dataset()).from_dict(document)

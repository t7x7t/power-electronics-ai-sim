import copy

import pytest

from pe_sim.outputs import (
    OUTPUT_SCHEMA_VERSION,
    OutputCollector,
    OutputDataset,
    OutputDefinition,
    OutputFieldDefinition,
    OutputRegistry,
)
from pe_sim.traces import TraceProvenance


def _registry() -> OutputRegistry:
    return OutputRegistry(
        (
            OutputDefinition(
                "vout.final",
                "Final output voltage",
                "scalar",
                source="postprocess",
                unit="V",
            ),
            OutputDefinition(
                "vout.transient",
                "Output voltage transient",
                "series",
                source="plant",
                unit="V",
            ),
            OutputDefinition(
                "load_sweep",
                "Load sweep",
                "table",
                source="experiment",
                fields=(
                    OutputFieldDefinition("load_ohm", "Load", unit="ohm"),
                    OutputFieldDefinition("vout_v", "Output voltage", unit="V"),
                        OutputFieldDefinition("accepted", "Accepted", unit="1", dtype="bool"),
                ),
            ),
        )
    )


def test_output_dataset_round_trip_for_scalar_series_and_table():
    collector = OutputCollector(
        _registry(),
        provenance=TraceProvenance(
            run_id="buck-run-1",
            manifest_sha256="a" * 64,
            package_sha256="b" * 64,
            source_commit="deadbeef",
        ),
        metadata={"producer_version": "1"},
    )
    collector.publish("vout.final", 12.0)
    collector.publish(
        "vout.transient",
        {"time_s": [0.0, 1e-3, 2e-3], "values": [0.0, 11.7, 12.0]},
    )
    collector.publish(
        "load_sweep",
        {
            "rows": [
                {"load_ohm": 10.0, "vout_v": 12.0, "accepted": True},
                {"load_ohm": 20.0, "vout_v": 11.8, "accepted": False},
            ]
        },
    )

    dataset = collector.dataset()
    restored = OutputDataset.from_json(dataset.to_json())

    assert dataset.schema_version == OUTPUT_SCHEMA_VERSION
    assert restored.to_dict() == dataset.to_dict()
    assert restored.provenance.package_sha256 == "b" * 64
    assert restored.fingerprint == dataset.fingerprint
    assert restored.get("vout.final").payload == 12.0
    assert restored.get("load_sweep").payload["rows"][1]["accepted"] is False


def test_output_publication_requires_explicit_unique_registration():
    registry = _registry()
    with pytest.raises(ValueError, match="already registered"):
        registry.register(
            OutputDefinition(
                "vout.final",
                "Duplicate output",
                "scalar",
                source="postprocess",
                unit="V",
            )
        )

    collector = OutputCollector(registry)
    with pytest.raises(KeyError, match="unknown output_id"):
        collector.publish("plant.internal_state", 1.0)
    collector.publish("vout.final", 12.0)
    with pytest.raises(ValueError, match="already published"):
        collector.publish("vout.final", 11.9)


@pytest.mark.parametrize(
    ("output_id", "payload", "message"),
    [
        ("vout.final", float("nan"), "finite"),
        ("vout.final", "12.0", "finite numeric"),
        (
            "vout.transient",
            {"time_s": [0.0, 1.0, 0.5], "values": [1.0, 2.0, 3.0]},
            "monotonic",
        ),
        (
            "vout.transient",
            {"time_s": [0.0, 1.0], "values": [1.0]},
            "same length",
        ),
        (
            "load_sweep",
            {"rows": [{"load_ohm": 10.0, "vout_v": 12.0}]},
            "missing columns",
        ),
        (
            "load_sweep",
            {"rows": [{"load_ohm": 10.0, "vout_v": float("inf"), "accepted": True}]},
            "finite",
        ),
    ],
)
def test_invalid_payloads_fail_closed(output_id, payload, message):
    collector = OutputCollector(_registry())
    with pytest.raises(ValueError, match=message):
        collector.publish(output_id, payload)


def test_output_dataset_schema_and_shape_fail_closed():
    collector = OutputCollector(_registry())
    collector.publish("vout.final", 12.0)
    collector.publish("vout.transient", {"time_s": [0.0], "values": [12.0]})
    collector.publish("load_sweep", {"rows": []})
    document = collector.dataset().to_dict()

    future = copy.deepcopy(document)
    future["schema_version"] = "2.0"
    with pytest.raises(ValueError, match="unsupported"):
        OutputDataset.from_dict(future)

    missing_payload = copy.deepcopy(document)
    del missing_payload["outputs"][0]["payload"]
    with pytest.raises(ValueError, match="payload"):
        OutputDataset.from_dict(missing_payload)

    malformed_table = copy.deepcopy(document)
    malformed_table["outputs"][2]["payload"] = {"rows": [{"load_ohm": 10.0, "vout_v": 12.0, "accepted": "yes"}]}
    with pytest.raises(ValueError, match="boolean"):
        OutputDataset.from_dict(malformed_table)

    tampered = copy.deepcopy(document)
    tampered["outputs"][0]["payload"] = 11.8
    with pytest.raises(ValueError, match="fingerprint"):
        OutputDataset.from_dict(tampered)

    unexpected = copy.deepcopy(document)
    unexpected["unexpected"] = True
    with pytest.raises(ValueError, match="unexpected fields"):
        OutputDataset.from_dict(unexpected)


def test_dataset_defensively_freezes_nested_payload_metadata_and_provenance():
    provenance = TraceProvenance(
        run_id="run-immutable",
        manifest_sha256="a" * 64,
        metadata={"tags": ["reference"]},
    )
    source_rows = [{"load_ohm": 10.0, "vout_v": 12.0, "accepted": True}]
    source_metadata = {"nested": {"values": [1, 2]}}
    collector = OutputCollector(_registry(), provenance=provenance, metadata=source_metadata)
    collector.publish("load_sweep", {"rows": source_rows})
    dataset = collector.dataset()
    initial_fingerprint = dataset.fingerprint

    source_rows[0]["vout_v"] = 99.0
    source_metadata["nested"]["values"].append(3)
    with pytest.raises((TypeError, AttributeError)):
        provenance.metadata["tags"].append("mutated")

    assert dataset.get("load_sweep").payload["rows"][0]["vout_v"] == 12.0
    assert dataset.metadata["nested"]["values"] == (1, 2)
    assert dataset.provenance.metadata["tags"] == ("reference",)
    assert dataset.fingerprint == initial_fingerprint

    with pytest.raises(TypeError, match="read-only"):
        dataset.get("load_sweep").payload["rows"][0]["vout_v"] = 99.0
    with pytest.raises(TypeError, match="read-only"):
        dataset.metadata["new"] = True
    with pytest.raises(TypeError, match="read-only"):
        dataset.provenance.metadata["new"] = True


def test_serialization_returns_defensive_mutable_copy_and_stale_provenance_fails():
    collector = OutputCollector(
        _registry(),
        provenance=TraceProvenance(run_id="run-provenance", manifest_sha256="a" * 64),
    )
    collector.publish("vout.final", 12.0)
    dataset = collector.dataset()
    document = dataset.to_dict()
    document["outputs"][0]["payload"] = 99.0
    document["provenance"]["run_id"] = "other-run"
    assert dataset.get("vout.final").payload == 12.0
    assert dataset.provenance.run_id == "run-provenance"
    with pytest.raises(ValueError, match="fingerprint"):
        OutputDataset.from_dict(document)


def test_table_definition_rejects_missing_units_and_duplicate_fields():
    with pytest.raises(ValueError, match="unit"):
        OutputFieldDefinition("voltage", "Voltage", unit=None)
    with pytest.raises(ValueError, match="units belong"):
        OutputDefinition(
            "bad.table.unit",
            "Bad table unit",
            "table",
            source="postprocess",
            unit="V",
            fields=(OutputFieldDefinition("value", "Value", unit="V"),),
        )
    with pytest.raises(ValueError, match="unique"):
        OutputDefinition(
            "bad.table",
            "Bad table",
            "table",
            source="postprocess",
            fields=(
                OutputFieldDefinition("x", "X", unit="V"),
                OutputFieldDefinition("x", "X2", unit="A"),
            ),
        )

import copy

import pytest

from pe_sim.reference_plants import BoostPlant, BuckPlant
from pe_sim.topology import (
    TopologyComponent,
    TopologyDescriptor,
    TopologyNet,
    TopologyPort,
    boost_topology_descriptor,
    buck_topology_descriptor,
    export_topology_descriptor,
    topology_to_adjacency,
    topology_to_dot,
)


def _buck() -> BuckPlant:
    return BuckPlant(
        input_voltage_v=24.0,
        inductance_h=100e-6,
        capacitance_f=100e-6,
        load_resistance_ohm=4.0,
    )


def _boost() -> BoostPlant:
    return BoostPlant(
        input_voltage_v=12.0,
        inductance_h=100e-6,
        capacitance_f=100e-6,
        load_resistance_ohm=12.0,
    )


def test_buck_descriptor_is_versioned_connected_and_parameterized():
    descriptor = buck_topology_descriptor(_buck())

    payload = descriptor.to_dict()
    assert payload["schema"] == "topology-descriptor-v1"
    assert payload["topology_id"] == "buck"
    assert len(payload["fingerprint"]) == 64
    assert {item["component_id"] for item in payload["components"]} == {
        "vin_source", "switch", "inductor", "capacitor", "load"
    }
    inductor = next(item for item in payload["components"] if item["component_id"] == "inductor")
    assert inductor["parameters"]["inductance_h"] == pytest.approx(100e-6)
    assert "c:inductor" in topology_to_dot(descriptor)
    assert topology_to_adjacency(descriptor)["inductor"][0]["net_id"] == "sw"


def test_boost_exporter_and_plant_entrypoint_are_stable():
    plant = _boost()
    direct = boost_topology_descriptor(plant)
    via_plant = plant.topology_descriptor()
    assert direct.to_dict() == via_plant.to_dict()
    assert export_topology_descriptor(plant).fingerprint == direct.fingerprint
    assert "diode" in {component.component_id for component in direct.components}


def test_descriptor_round_trip_rejects_tampering_and_is_read_only():
    descriptor = buck_topology_descriptor(_buck())
    restored = TopologyDescriptor.from_dict(descriptor.to_dict())
    assert restored.to_dict() == descriptor.to_dict()

    document = copy.deepcopy(descriptor.to_dict())
    document["components"][2]["parameters"]["inductance_h"] = 1.0
    with pytest.raises(ValueError, match="fingerprint"):
        TopologyDescriptor.from_dict(document)

    with pytest.raises(TypeError, match="read-only"):
        descriptor.components[2].parameters["inductance_h"] = 1.0
    with pytest.raises(TypeError, match="read-only"):
        descriptor.metadata["new"] = True


def test_descriptor_rejects_unknown_nets_and_duplicate_component_ids():
    net = TopologyNet("gnd")
    bad_port = TopologyPort("p", "missing")
    component = TopologyComponent("x", "test", (bad_port,))
    with pytest.raises(ValueError, match="unknown net"):
        TopologyDescriptor("bad", (component,), (net,))

    valid_port = TopologyPort("p", "gnd")
    valid_component = TopologyComponent("x", "test", (valid_port,))
    with pytest.raises(ValueError, match="component ids"):
        TopologyDescriptor("bad", (valid_component, valid_component), (net,))


def test_unknown_plant_topology_fails_closed():
    class Unreviewed:
        topology = "llc"

    with pytest.raises(ValueError, match="no reviewed topology exporter"):
        export_topology_descriptor(Unreviewed())

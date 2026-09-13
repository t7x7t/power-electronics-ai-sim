from pe_sim.default_contracts import default_contract_ref, default_contract_refs
from pe_sim.provenance import collect_backend_provenance, collect_environment_provenance
from pe_sim.runtime import FakePIController
from pe_sim.reference_plants import BuckPlant


def test_default_contract_hashes_are_derived_from_reviewable_definitions():
    refs = default_contract_refs()
    assert refs["safety"] == {
        "id": "default-safety",
        "hash": "c8af0de8cfeec979bf5fd17dbf1c819df90d6fc5a95fc3a2f53d84271ba0ef58",
    }
    assert refs["qualification"] == {
        "id": "default-qualification",
        "hash": "a3ef375ff21b886d76977854178c77a5bd397e551170fe13e4db9e606f9f121e",
    }
    assert default_contract_ref("safety")["hash"] != "0" * 64


def test_reference_plant_declares_known_project_owned_backend_provenance():
    plant = BuckPlant(
        input_voltage_v=12,
        inductance_h=100e-6,
        capacitance_f=470e-6,
        load_resistance_ohm=10,
    )
    backend = collect_backend_provenance(plant)
    assert backend["status"] == "known"
    assert backend["name"] == "pe_sim.ideal_averaged_python"
    assert backend["solver_settings"]["integrator"] == "rk4"
    assert collect_environment_provenance((plant, FakePIController()))["status"] == "known"

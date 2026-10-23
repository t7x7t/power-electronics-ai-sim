"""The shipped solve path must match its checked-in review pin."""

from __future__ import annotations

import pytest

from pe_sim.identity import (
    REVIEW_IDENTITIES_SCHEMA,
    implementation_digest,
    implementation_sources,
    load_reviewed_identities,
    review_status,
)
from pe_sim.backends.ngspice_plants import NgspiceBuckPlant


def _plant(**overrides) -> NgspiceBuckPlant:
    values = {"solver_callable": lambda netlist: None, "control_period_s": 10e-6}
    values.update(overrides)
    return NgspiceBuckPlant(**values)


def test_solve_path_sources_are_declared_and_hashed():
    sources = implementation_sources("ngspice-buck")

    assert set(sources) == {
        "ngspice_plants.py",
        "netlist.py",
        "netlist_builder.py",
        "process.py",
        "settings.py",
        "wrdata.py",
    }
    assert all(len(digest) == 64 for digest in sources.values())
    assert implementation_digest("ngspice-buck") == implementation_digest("ngspice-buck")


def test_shipped_ngspice_adapter_matches_the_review_pin():
    status = review_status("ngspice-buck")

    assert status["status"] == "reviewed", status
    assert status["reviewed_implementation_sha256"] == implementation_digest("ngspice-buck")


def test_review_pin_document_has_the_expected_schema():
    document = load_reviewed_identities()

    assert document["schema"] == REVIEW_IDENTITIES_SCHEMA
    assert "ngspice-buck" in document["backends"]


def test_plant_identity_reports_the_same_review_status():
    implementation = _plant().manifest_identity()["implementation"]

    assert implementation["sha256"] == implementation_digest("ngspice-buck")
    assert implementation["review"]["status"] == "reviewed"


def test_control_period_must_be_an_integer_multiple_of_the_switching_period():
    with pytest.raises(ValueError, match="integer multiple"):
        _plant(control_period_s=25e-6, switching_frequency_hz=100e3)


def test_preview_netlist_does_not_invoke_the_solver():
    calls: list[str] = []
    plant = _plant(solver_callable=lambda netlist: calls.append(netlist))
    plant.reset({})

    text = plant.preview_netlist(duty=0.4)

    assert "V1 vin 0 DC" in text
    assert ".tran" in text
    assert calls == []


def test_unknown_backend_has_no_declared_solve_path():
    with pytest.raises(KeyError):
        implementation_digest("xyce")

"""A run id that can be written must also be attestable.

Regression for DEFECT-1: the write path and the attestation path applied
different run-id rules.  `CON`, `AUX`, `COM1`, `LPT1` and a trailing dot were
accepted when the run directory was created, so a formal run could reach
`comparable` while its package could never be attested (L1 impossible), and
`trailing.` landed in `runs/trailing` while the manifest recorded
`run_id = "trailing."`.

Both sides now call one rule, `pe_sim.artifacts.validate_run_id`.
"""

from __future__ import annotations

import pytest

from pe_sim import ExperimentSpec, Timebase
from pe_sim.artifacts import validate_run_location
from pe_sim.attestation import AttestationError, _validate_run_id
from pe_sim.reference_plants import BuckPlant
from pe_sim.runtime import FakePIController, Runner


REJECTED_IDS = (
    "CON",
    "con",
    "AUX",
    "NUL",
    "PRN",
    "COM1",
    "LPT9",
    "trailing.",
    "trailing ",
)

ACCEPTED_IDS = (
    "buck-demo",
    "run_1.2",
    "Run-2026_10_26",
    "console",
)


def _plant() -> BuckPlant:
    return BuckPlant(
        input_voltage_v=12.0,
        inductance_h=100e-6,
        capacitance_f=470e-6,
        load_resistance_ohm=10.0,
        integration_step_s=1e-6,
    )


def _spec(tmp_path, run_id: str) -> ExperimentSpec:
    return ExperimentSpec(
        "run-id-boundary",
        run_id,
        "buck",
        "pi",
        Timebase(duration_s=1e-4, control_period_s=1e-5),
        output_dir=str(tmp_path),
    )


@pytest.mark.parametrize("run_id", REJECTED_IDS)
def test_the_write_side_rejects_what_the_attestation_side_rejects(tmp_path, run_id):
    with pytest.raises(ValueError):
        validate_run_location(tmp_path, run_id)
    with pytest.raises(AttestationError):
        _validate_run_id(run_id)


@pytest.mark.parametrize("run_id", ACCEPTED_IDS)
def test_both_sides_accept_the_same_ids(tmp_path, run_id):
    assert validate_run_location(tmp_path, run_id) == (tmp_path / run_id).resolve()
    _validate_run_id(run_id)


def test_a_reserved_run_id_fails_before_a_directory_exists(tmp_path):
    with pytest.raises(ValueError, match="reserved"):
        Runner().run(_spec(tmp_path, "CON"), _plant(), FakePIController())

    assert list(tmp_path.iterdir()) == []


def test_a_trailing_dot_never_produces_a_renamed_directory(tmp_path):
    with pytest.raises(ValueError, match="dot or a space"):
        Runner().run(_spec(tmp_path, "trailing."), _plant(), FakePIController())

    assert list(tmp_path.iterdir()) == []

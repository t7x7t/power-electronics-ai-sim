"""A malformed parameter declaration must be visible, never inferred away.

Regression for DEFECT-2: `normalise_parameter_provenance` silently inferred a
status from the records.  An unregistered token such as `"trusted"` became
`declared` with `declaration_error=None`, and a NaN value was quietly replaced
by `None` while an explicit `"declared"` status survived, so the applicability
summary still said `within`.

`docs/40` §2.1 requires a malformed declaration to become
`declaration_invalid` with a bounded error, without crashing the run.
"""

from __future__ import annotations

import math

import pytest

from pe_sim import ExperimentSpec, Timebase
from pe_sim.applicability import ApplicabilityPolicy, ModelEnvelopePlugin
from pe_sim.checks import CheckContext
from pe_sim.model_claim import (
    ParameterRecord,
    collect_parameter_provenance,
    normalise_parameter_provenance,
)
from pe_sim.reference_plants import BuckPlant


def _declaration(status: str = "declared", **record_overrides):
    record = {
        "name": "inductance_h",
        "value": 100e-6,
        "unit": "H",
        "source": "datasheet",
        "review_status": "reviewed",
    }
    record.update(record_overrides)
    return {"status": status, "records": [record]}


def _plant() -> BuckPlant:
    return BuckPlant(
        input_voltage_v=12.0,
        inductance_h=100e-6,
        capacitance_f=470e-6,
        load_resistance_ohm=10.0,
        integration_step_s=1e-6,
    )


def test_an_unregistered_status_token_is_a_declaration_error():
    result = normalise_parameter_provenance(_declaration(status="trusted"))

    assert result.status == "declaration_invalid"
    assert result.declaration_error
    assert "trusted" in result.declaration_error


@pytest.mark.parametrize("bad_value", [float("nan"), float("inf"), float("-inf"), "100e-6", True])
def test_a_non_numeric_or_non_finite_value_is_a_declaration_error(bad_value):
    result = normalise_parameter_provenance(_declaration(value=bad_value))

    assert result.status == "declaration_invalid"
    assert result.declaration_error
    assert len(result.declaration_error) <= 200


def test_an_explicit_none_value_is_still_a_partial_declaration():
    """``None`` legitimately means "value not known"; it is not malformed."""

    declaration = _declaration(value=None, source="assumption", review_status="unreviewed")
    declaration["status"] = ""

    result = normalise_parameter_provenance(declaration)

    assert result.status == "partial"
    assert result.declaration_error is None


def test_direct_record_construction_rejects_a_non_finite_value():
    """The invalid state is unrepresentable rather than silently coerced."""

    with pytest.raises(ValueError):
        ParameterRecord(name="inductance_h", value=math.nan, unit="H", source="datasheet")


def test_an_adapter_declaration_error_does_not_crash_the_collection():
    class _Adapter:
        def parameter_provenance(self):
            return _declaration(status="trusted")

    result = collect_parameter_provenance(_Adapter())

    assert result.status == "declaration_invalid"


def test_the_invalid_declaration_is_visible_in_the_applicability_summary(tmp_path):
    plant = _plant()
    plugin = ModelEnvelopePlugin(
        plant.validity_envelope(),
        ApplicabilityPolicy(),
        provenance=_declaration(status="trusted"),
    )
    spec = ExperimentSpec(
        "provenance-boundary",
        "provenance-boundary",
        "buck",
        "pi",
        Timebase(duration_s=1e-4, control_period_s=1e-5),
        output_dir=str(tmp_path),
    )

    plugin.check_preflight(spec, CheckContext(step_index=0, current_time_s=0.0, phase="action"))
    summary = plugin.summary()

    assert summary.parameter_provenance_status == "declaration_invalid"
    assert summary.finding_count >= 1
    assert summary.status != "within"

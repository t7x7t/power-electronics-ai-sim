"""Applicability-envelope declarations, policy, and runtime integration."""

from __future__ import annotations

import json
import math

import pytest

from pe_sim import ExperimentSpec, Timebase
from pe_sim.applicability import (
    ApplicabilityError,
    ApplicabilityPolicy,
    ModelEnvelopePlugin,
)
from pe_sim.checks import CheckContext
from pe_sim.model_claim import (
    ModelClaim,
    NumericalLimit,
    OperatingRange,
    ParameterProvenance,
    ParameterRecord,
    collect_model_claim,
    collect_parameter_provenance,
)
from pe_sim.reference_plants import BuckPlant
from pe_sim.runner import RunOptions
from pe_sim.runtime import FakePIController, Runner


def _plant(**overrides):
    values = {
        "input_voltage_v": 12.0,
        "inductance_h": 100e-6,
        "capacitance_f": 470e-6,
        "load_resistance_ohm": 10.0,
        "integration_step_s": 1e-6,
    }
    values.update(overrides)
    return BuckPlant(**values)


def _spec(tmp_path, plant, run_id="applicability", *, schedule=()):
    return ExperimentSpec(
        "applicability-exp",
        run_id,
        plant.topology,
        "pi",
        Timebase(duration_s=1e-4, control_period_s=1e-5),
        input_schedule=schedule,
        output_dir=str(tmp_path),
        seed=7,
    )


def _context(phase="action", step_index=0, time_s=0.0):
    return CheckContext(step_index=step_index, current_time_s=time_s, phase=phase)


class _NarrowDutyBuck(BuckPlant):
    def validity_envelope(self) -> ModelClaim:
        claim = super().validity_envelope()
        ranges = tuple(
            OperatingRange(
                item.name,
                unit=item.unit,
                minimum=item.minimum,
                maximum=0.5 if item.name == "duty" else item.maximum,
                minimum_exclusive=item.minimum_exclusive,
                maximum_exclusive=item.maximum_exclusive,
                basis=item.basis,
                note=item.note,
            )
            for item in claim.operating_ranges
        )
        return ModelClaim(
            claim_level=claim.claim_level,
            topology=claim.topology,
            model_form=claim.model_form,
            assumptions=claim.assumptions,
            operating_ranges=ranges,
            numerical_limits=claim.numerical_limits,
            limitations=claim.limitations,
            uncheckable_rules=claim.uncheckable_rules,
            actuator=claim.actuator,
            static_parameters=claim.static_parameters,
        )


def test_absent_envelope_is_unknown_not_an_error(tmp_path):
    plant = _plant()
    claim = collect_model_claim(object())
    plugin = ModelEnvelopePlugin(claim, ApplicabilityPolicy(mode="record"))
    report = plugin.check_preflight(_spec(tmp_path, plant), CheckContext(phase="preflight"))

    assert claim.status == "unknown"
    assert report.passed is True
    assert plugin.summary().status == "unknown"
    finding = plugin.findings()[0]
    assert finding.severity == "info"
    assert finding.stop_requested is False


def test_out_of_range_duty_record_warn_enforce_modes():
    claim = ModelClaim(
        claim_level="test",
        topology="buck",
        operating_ranges=(OperatingRange("duty", minimum=0.0, maximum=1.0),),
    )
    record = ModelEnvelopePlugin(claim, ApplicabilityPolicy(mode="record"))
    warn = ModelEnvelopePlugin(claim, ApplicabilityPolicy(mode="warn"))
    enforce = ModelEnvelopePlugin(claim, ApplicabilityPolicy(mode="enforce"))

    record_report = record.check_action(1.5, _context())
    warn_report = warn.check_action(1.5, _context())
    enforce_report = enforce.check_action(1.5, _context())

    assert record_report.passed is True
    assert record.findings()[0].severity == "info"
    assert warn_report.passed is True
    assert warn.findings()[0].severity == "warning"
    assert enforce_report.passed is False
    assert enforce.findings()[0].stop_requested is True
    assert enforce.summary().status == "violated"


def test_preflight_enforce_blocks_before_a_run_directory_is_created(tmp_path):
    plant = _NarrowDutyBuck(
        input_voltage_v=12.0,
        inductance_h=100e-6,
        capacitance_f=470e-6,
        load_resistance_ohm=10.0,
        integration_step_s=1e-6,
    )
    spec = _spec(tmp_path, plant, "blocked")

    with pytest.raises(ApplicabilityError) as excinfo:
        Runner().run(
            spec,
            plant,
            FakePIController(kp=0.2),
            options=RunOptions(
                applicability=ApplicabilityPolicy(mode="enforce"),
                qualification_plugins=(),
            ),
        )

    assert excinfo.value.phase == "preflight"
    assert excinfo.value.report.findings
    assert not (tmp_path / "blocked").exists()


def test_preflight_record_is_non_blocking_and_recorded(tmp_path):
    plant = _NarrowDutyBuck(
        input_voltage_v=12.0,
        inductance_h=100e-6,
        capacitance_f=470e-6,
        load_resistance_ohm=10.0,
        integration_step_s=1e-6,
    )
    result = Runner().run(
        _spec(tmp_path, plant, "recorded"),
        plant,
        FakePIController(kp=0.2),
        options=RunOptions(
            applicability=ApplicabilityPolicy(mode="record"),
            qualification_plugins=(),
        ),
    )
    manifest = json.loads((result.run_dir / "manifest.json").read_text(encoding="utf-8"))

    assert result.status == "QUALIFIED"
    assert manifest["applicability"]["status"] == "limited"
    assert manifest["applicability"]["counts"].get("envelope.actuator.subset") == 1
    assert (result.run_dir / "preflight.json").is_file()
    preflight = json.loads((result.run_dir / "preflight.json").read_text(encoding="utf-8"))
    assert preflight["schema_version"] == "preflight-v1"
    assert preflight["gates"]


def test_schedule_voltage_outside_the_equation_domain_is_checked(tmp_path):
    claim = ModelClaim(
        claim_level="test",
        topology="buck",
        operating_ranges=(
            OperatingRange("input_voltage_v", minimum=0.0, minimum_exclusive=True),
        ),
    )
    spec = _spec(tmp_path, _plant(), "schedule", schedule=({"time_s": 0.0, "vin_v": -1.0},))
    record = ModelEnvelopePlugin(claim, ApplicabilityPolicy(mode="record"))
    enforce = ModelEnvelopePlugin(claim, ApplicabilityPolicy(mode="enforce"))

    record_report = record.check_preflight(spec, CheckContext(phase="preflight"))
    enforce_report = enforce.check_preflight(spec, CheckContext(phase="preflight"))

    assert record_report.passed is True
    assert any(finding.rule_id == "envelope.vin.domain" for finding in record.findings())
    assert enforce_report.passed is False


def test_external_input_change_rechecks_the_new_operating_point():
    claim = ModelClaim(
        claim_level="test",
        topology="buck",
        operating_ranges=(OperatingRange("load_resistance_ohm", minimum=5.0, maximum=20.0),),
    )
    plugin = ModelEnvelopePlugin(claim, ApplicabilityPolicy(mode="record"))

    class Observation:
        truth = {"load_resistance": 10.0}

    plugin.check_observation(Observation(), _context("observation", 0, 0.0))
    Observation.truth = {"load_resistance": 1.0}
    plugin.check_observation(Observation(), _context("observation", 1, 1e-5))

    rule_ids = [finding.rule_id for finding in plugin.findings()]
    assert "envelope.external_input.changed" in rule_ids
    assert rule_ids.count("envelope.load.domain") == 1
    changed = next(f for f in plugin.findings() if f.rule_id == "envelope.external_input.changed")
    assert changed.evidence["current"]["load_resistance_ohm"] == 1.0
    assert "duty" not in changed.evidence["previous"]


def test_findings_are_deduplicated_and_counted():
    claim = ModelClaim(
        claim_level="test",
        operating_ranges=(OperatingRange("duty", minimum=0.0, maximum=1.0),),
    )
    plugin = ModelEnvelopePlugin(claim, ApplicabilityPolicy(mode="record"))
    for index in range(100):
        plugin.check_action(1.5, _context(step_index=index))

    assert len(plugin.findings()) == 1
    assert plugin.counts()["envelope.duty.domain"] == 100
    assert plugin.summary().violation_count == 100


def test_envelope_hash_is_stable_and_not_part_of_the_plant_identity():
    plant = _plant()
    claim = collect_model_claim(plant)
    identity = plant.manifest_identity()

    assert claim.sha256() == collect_model_claim(_plant()).sha256()
    assert "validity_envelope" not in identity
    assert "applicability" not in identity
    assert identity["sha256"] == _plant().manifest_identity()["sha256"]


def test_default_buck_integration_step_passes_its_declared_envelope(tmp_path):
    plant = _plant()
    plugin = ModelEnvelopePlugin(collect_model_claim(plant), ApplicabilityPolicy(mode="record"))
    plugin.check_preflight(_spec(tmp_path, plant), CheckContext(phase="preflight"))

    assert not any(
        finding.rule_id == "envelope.timestep.dynamics" for finding in plugin.findings()
    )


def test_ngspice_declares_the_unapplied_solver_max_step(tmp_path):
    from pe_sim.backends.ngspice_plants import NgspiceBuckPlant

    plant = NgspiceBuckPlant(
        control_period_s=10e-6,
        switching_frequency_hz=100e3,
        solver_callable=lambda netlist: None,
    )
    envelope = plant.validity_envelope().to_dict()
    rules = {item["rule_id"]: item["reason"] for item in envelope["uncheckable_rules"]}

    assert rules["envelope.numerical.max_step"] == "solver_option_not_applied_by_netlist_template"
    assert envelope["claim_level"] == "L2-switching-reference"


def test_parameter_provenance_hash_is_order_independent_and_portable():
    first = ParameterRecord(
        name="inductance_h",
        value=100e-6,
        unit="H",
        source="datasheet",
        reference=r"D:\private\datasheet.pdf",
    )
    second = ParameterRecord(name="load_resistance_ohm", value=10.0, unit="ohm", source="assumption")
    left = ParameterProvenance(records=(first, second), status="declared")
    right = ParameterProvenance(records=(second, first), status="declared")

    assert first.reference is None
    assert len(first.reference_sha256) == 64
    assert left.sha256() == right.sha256()
    serialized = json.dumps(left.to_dict(), sort_keys=True, allow_nan=False)
    assert "private" not in serialized
    assert "D:" not in serialized


def test_policy_defaults_resolve_by_run_mode():
    assert ApplicabilityPolicy().resolve("exploratory").mode == "record"
    assert ApplicabilityPolicy().resolve("formal_comparison").mode == "enforce"
    assert ApplicabilityPolicy(mode="off").resolve("formal_comparison").mode == "off"


def test_third_party_adapter_that_raises_is_a_declaration_error(tmp_path):
    class Broken:
        def validity_envelope(self):
            raise RuntimeError("broken declaration")

    claim = collect_model_claim(Broken())
    plugin = ModelEnvelopePlugin(claim, ApplicabilityPolicy(mode="record"))
    report = plugin.check_preflight(_spec(tmp_path, _plant()), CheckContext(phase="preflight"))

    assert claim.status == "declaration_invalid"
    assert report.passed is True
    assert plugin.summary().status == "declaration_invalid"


def test_unknown_numerical_limit_is_informational_only():
    claim = ModelClaim(
        claim_level="test",
        numerical_limits=(NumericalLimit("custom.limit", 1.0, strength="advisory"),),
    )
    plugin = ModelEnvelopePlugin(claim, ApplicabilityPolicy(mode="record"))
    report = plugin.check_preflight(
        ExperimentSpec(
            "custom",
            "custom-run",
            "buck",
            "pi",
            Timebase(duration_s=1e-4, control_period_s=1e-5),
            output_dir=".",
            seed=1,
        ),
        CheckContext(phase="preflight"),
    )
    assert report.passed is True


def test_preflight_does_not_invoke_a_solver():
    from pe_sim.backends.ngspice_plants import NgspiceBuckPlant

    def boom(netlist):
        raise AssertionError("solver must not be called during preflight")

    plant = NgspiceBuckPlant(control_period_s=10e-6, solver_callable=boom)
    spec = ExperimentSpec(
        "ngspice-preview",
        "ngspice-preview",
        "buck",
        "pi",
        Timebase(duration_s=1e-4, control_period_s=1e-5),
        output_dir=".",
        seed=1,
    )
    plugin = ModelEnvelopePlugin(collect_model_claim(plant), ApplicabilityPolicy(mode="record"))
    report = plugin.check_preflight(spec, CheckContext(phase="preflight"))

    assert plant._solver_invocations == 0
    assert report.passed is True
    assert plugin.summary().claim_level == "L2-switching-reference"

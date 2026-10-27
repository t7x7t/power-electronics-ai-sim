"""Ideal averaged multiphase Buck reference.

The model represents an N-phase Buck converter as N parallel averaged phases
that share one duty command.  Only the total inductor current is an integrated
state.  The per-phase currents reported by :meth:`MultiphaseBuckPlant.observe`
are an equal-share declaration derived from the total current, not independent
phase currents and not the output of a current-sharing loop.

`per_phase_current_sharing_not_modelled`: no per-phase current loop, active
current-sharing loop, phase-shedding transient, ripple cancellation, device,
thermal, magnetic, or loss model is represented.
"""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any, Mapping

from .contracts import PlantObservation
from .identity import (
    implementation_digest,
    implementation_sources,
    register_solve_path,
    review_status,
)
from .model_claim import (
    ModelClaim,
    NumericalLimit,
    OperatingRange,
    ParameterProvenance,
    ParameterRecord,
)
from .reference_plants import BuckPlant, _positive
from .safety import BoundedActionPolicy


def _phase_count(phases: int) -> int:
    """Validate the declared phase count and return it as an ``int``."""

    if isinstance(phases, bool) or not isinstance(phases, int):
        raise ValueError("phases must be an integer in [2, 8]")
    if not 2 <= phases <= 8:
        raise ValueError("phases must be an integer in [2, 8]")
    return int(phases)


def phase_offsets_deg(phases: int) -> tuple[float, ...]:
    """Return the evenly spaced phase offsets for a multiphase converter.

    The offsets describe the declared phase relationship used by the averaged
    model.  They are not measured gate-drive timings and do not claim a
    particular PWM implementation.
    """

    count = _phase_count(phases)
    return tuple(360.0 * index / count for index in range(count))


def phase_currents(total_current_a: float, phases: int) -> tuple[float, ...]:
    """Split a total current equally across ``phases`` declared phases.

    This is an equal-share declaration used for observation and testing.  It
    is not a model of per-phase current regulation or current sharing.
    """

    count = _phase_count(phases)
    total = float(total_current_a)
    if not math.isfinite(total):
        raise ValueError("total_current_a must be finite")
    share = total / count
    return tuple(share for _ in range(count))


class MultiphaseBuckPlant(BuckPlant):
    """Ideal averaged N-phase Buck converter with one shared duty command.

    The integrated states are the **total** inductor current and the output
    capacitor voltage.  For ``N`` phases with per-phase inductance ``L`` the
    averaged equations are

    ``diL_total/dt = N * (duty * Vin - vout) / L``
    ``dvout/dt = (iL_total - vout / R) / C``

    which is the same as a single averaged Buck with effective inductance
    ``L / N``.  Phase offsets are managed internally; the caller supplies only
    the shared duty command.

    `per_phase_current_sharing_not_modelled`: per-phase current loops, active
    current-sharing loops, phase-shedding/adding transients, and ripple
    cancellation are not represented.
    """

    topology = "multiphase_buck"
    backend_id = "multiphase-buck"
    model_kind = "ideal_averaged_multiphase_reference"

    def __init__(
        self,
        *,
        input_voltage_v: float,
        inductance_h: float,
        capacitance_f: float,
        load_resistance_ohm: float,
        phases: int,
        switching_frequency_hz: float = 100e3,
        integration_step_s: float = 1e-6,
    ) -> None:
        count = _phase_count(phases)
        frequency = _positive("switching_frequency_hz", switching_frequency_hz)
        super().__init__(
            input_voltage_v=input_voltage_v,
            inductance_h=inductance_h,
            capacitance_f=capacitance_f,
            load_resistance_ohm=load_resistance_ohm,
            integration_step_s=integration_step_s,
        )
        self.phases = count
        self.switching_frequency_hz = frequency

    @property
    def effective_inductance_h(self) -> float:
        """Return the parallel effective inductance ``L / N``."""

        return self.inductance_h / self.phases

    def action_policy(self) -> BoundedActionPolicy:
        """Declare the shared duty-ratio actuator range."""

        return BoundedActionPolicy(0.0, 1.0, name="duty")

    def _derivatives(self, duty: float, current: float, voltage: float) -> tuple[float, float]:
        return (
            self.phases * (duty * self.input_voltage_v - voltage) / self.inductance_h,
            (current - voltage / self.load_resistance_ohm) / self.capacitance_f,
        )

    def _steady_state(self, duty: float) -> tuple[float, float]:
        voltage = duty * self.input_voltage_v
        return voltage, voltage / self.load_resistance_ohm

    def analytical_steady_state(self, duty: float) -> dict[str, float]:
        """Return the ideal CCM equilibrium and the declared per-phase share."""

        equilibrium = super().analytical_steady_state(duty)
        equilibrium["per_phase_current_a"] = (
            equilibrium["inductor_current_a"] / self.phases
        )
        return equilibrium

    def observe(self) -> PlantObservation:
        """Return the total measurement plus declared phase metadata.

        `per_phase_current_sharing_not_modelled`: the per-phase current fields
        are an equal split of the integrated total current.  They are not
        independently measured currents and cannot demonstrate current sharing.
        """

        base = super().observe()
        truth: dict[str, float] = dict(base.truth)
        truth["phase_count"] = float(self.phases)
        truth["effective_inductance_h"] = self.effective_inductance_h
        for index, (offset, current) in enumerate(
            zip(phase_offsets_deg(self.phases), phase_currents(base.measurement["inductor_current"], self.phases))
        ):
            truth[f"phase_offset_deg_{index}"] = offset
            truth[f"inductor_current_phase_{index}"] = current
        return PlantObservation(
            time_s=base.time_s,
            measurement=dict(base.measurement),
            truth=truth,
            age_steps=base.age_steps,
            measurement_units=dict(base.measurement_units),
        )

    def manifest_identity(self) -> dict[str, Any]:
        """Return a deterministic identity for the multiphase solve path.

        This deliberately does not reuse the single-phase Buck solve path or
        its review status.  The implementation digest covers
        ``multiphase_plants.py``; the parameter block records the per-phase
        inductance, the phase count, the declared switching frequency, and the
        effective parallel inductance ``L / N``.
        """

        implementation = type(self)
        parameters = {
            "input_voltage_v": self.input_voltage_v,
            "inductance_h": self.inductance_h,
            "capacitance_f": self.capacitance_f,
            "load_resistance_ohm": self.load_resistance_ohm,
            "phases": self.phases,
            "switching_frequency_hz": self.switching_frequency_hz,
            "effective_inductance_h": self.effective_inductance_h,
            "integration_step_s": self.integration_step_s,
        }
        identity = {
            "level": "L1",
            "kind": "ideal_averaged_multiphase_reference",
            "topology": self.topology,
            "parameters": parameters,
            "capabilities": sorted(self.capabilities()),
            "implementation": {
                "module": implementation.__module__,
                "class": implementation.__qualname__,
                "solve_path_modules": sorted(implementation_sources(self.backend_id)),
                "sha256": implementation_digest(self.backend_id),
                "review": review_status(self.backend_id),
            },
        }
        identity["sha256"] = hashlib.sha256(
            json.dumps(identity, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
        ).hexdigest()
        return identity

    def validity_envelope(self) -> ModelClaim:
        """Declare the structural limits of the shared-duty averaged model."""

        effective_inductance = self.effective_inductance_h
        tau_current = effective_inductance / self.load_resistance_ohm
        tau_voltage = self.load_resistance_ohm * self.capacitance_f
        tau_lc = 2.0 * math.pi * math.sqrt(effective_inductance * self.capacitance_f)
        max_step = min(tau_current, tau_voltage, tau_lc / 2.0) / 10.0
        assumptions = (
            "all phases share one duty command; no independent phase actuators exist",
            "averaged state-space model: switching ripple and ripple cancellation are not represented",
            "continuous conduction mode (CCM) is assumed, not detected",
            "ideal switches and diodes",
            "lossless phase inductors and output capacitor (no ESR, ESL, or saturation)",
            "per-phase current loops and active current-sharing loops are not modelled",
            "phase offsets are managed internally by the model",
            "no device, thermal, magnetic, or efficiency/loss model",
        )
        limitations = (
            "shared-duty averaged model: per-phase current regulation and current sharing are not modelled",
            "per-phase currents reported in truth are an equal-share declaration, not measured or simulated phase currents",
            "switching ripple and ripple cancellation are not represented",
            "phase shedding, phase addition, and phase-count transients are not represented",
            "no device, thermal, magnetic, or efficiency/loss model",
            "parameter values are constructor-declared, not measured or calibrated",
        )
        return ModelClaim(
            claim_level="L1-averaged-reference",
            topology=self.topology,
            model_form="state_space_average",
            assumptions=assumptions,
            operating_ranges=(
                OperatingRange(
                    "duty",
                    unit="1",
                    minimum=0.0,
                    maximum=1.0,
                    basis="equation_domain",
                ),
                OperatingRange(
                    "input_voltage_v",
                    unit="V",
                    minimum=0.0,
                    minimum_exclusive=True,
                    basis="equation_domain",
                ),
                OperatingRange(
                    "load_resistance_ohm",
                    unit="ohm",
                    minimum=0.0,
                    minimum_exclusive=True,
                    basis="equation_domain",
                ),
                OperatingRange(
                    "phases",
                    unit="1",
                    minimum=2.0,
                    maximum=8.0,
                    basis="constructor_domain",
                ),
            ),
            numerical_limits=(
                NumericalLimit(
                    "integration_step_s.max",
                    max_step,
                    "s",
                    strength="advisory",
                    basis="structural",
                    note="one tenth of min(L_eff/R, R*C, T_LC/2) using L_eff = L/N",
                ),
                NumericalLimit(
                    "control_period.min_integration_substeps",
                    10.0,
                    "1",
                    strength="advisory",
                    basis="structural",
                ),
            ),
            limitations=limitations,
            uncheckable_rules=(
                {
                    "rule_id": "envelope.per_phase_current_sharing",
                    "reason": "per_phase_current_sharing_not_modelled",
                },
                {
                    "rule_id": "envelope.ccm.boundary",
                    "reason": "conduction_mode_not_detected_in_averaged_multiphase_model",
                },
                {
                    "rule_id": "envelope.switching_ripple",
                    "reason": "switching_ripple_and_ripple_cancellation_not_represented",
                },
            ),
            actuator={"name": "duty", "unit": "normalized"},
            static_parameters={
                "input_voltage_v": self.input_voltage_v,
                "inductance_h": self.inductance_h,
                "capacitance_f": self.capacitance_f,
                "load_resistance_ohm": self.load_resistance_ohm,
                "phases": self.phases,
                "switching_frequency_hz": self.switching_frequency_hz,
                "effective_inductance_h": effective_inductance,
                "integration_step_s": self.integration_step_s,
            },
        )

    def parameter_provenance(self) -> ParameterProvenance:
        """Declare that multiphase parameters are constructor assumptions."""

        values = (
            ("input_voltage_v", self.input_voltage_v, "V"),
            ("inductance_h", self.inductance_h, "H"),
            ("capacitance_f", self.capacitance_f, "F"),
            ("load_resistance_ohm", self.load_resistance_ohm, "ohm"),
            ("phases", self.phases, "1"),
            ("switching_frequency_hz", self.switching_frequency_hz, "Hz"),
            ("effective_inductance_h", self.effective_inductance_h, "H"),
            ("integration_step_s", self.integration_step_s, "s"),
        )
        records = tuple(
            ParameterRecord(
                name=name,
                value=value,
                unit=unit,
                source="assumption",
                reference="multiphase_plants.py constructor/default",
                review_status="unreviewed",
                note="reference-model value; not a measured or calibrated parameter",
            )
            for name, value, unit in values
        )
        return ParameterProvenance(records=records, status="declared")

    def snapshot(self) -> Mapping[str, Any]:
        """Return the shared lifecycle snapshot with the phase declaration."""

        snapshot = dict(super().snapshot())
        snapshot["phases"] = self.phases
        snapshot["switching_frequency_hz"] = self.switching_frequency_hz
        return snapshot

    def restore(self, snapshot: Mapping[str, Any]) -> Mapping[str, Any]:
        """Restore the shared lifecycle state, refusing a phase mismatch."""

        if "phases" in snapshot:
            phases = snapshot["phases"]
            if isinstance(phases, bool) or not isinstance(phases, int) or phases != self.phases:
                raise ValueError("snapshot phases do not match plant")
        return super().restore(snapshot)


# The solve path is process-local: the checked-in review pin stays untouched so
# this new, unpinned implementation is reported as ``not_pinned`` rather than
# inheriting the reviewed Buck identity.
register_solve_path("multiphase-buck", ("multiphase_plants.py",))


__all__ = [
    "MultiphaseBuckPlant",
    "phase_currents",
    "phase_offsets_deg",
]

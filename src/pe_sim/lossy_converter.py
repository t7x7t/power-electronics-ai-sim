"""Declared-loss averaged CCM buck converter reference model.

This module is the library-level half of ``docs/60`` C6.  It adds a small,
explicit loss model to the averaged buck reference:

* conduction loss in the main switch (``I_L**2 * Rds_on`` during ``D``),
* conduction loss in the inductor winding (``I_L**2 * DCR``),
* low-side conduction loss from a duty-weighted diode drop,
* switch-node output-charge loss (``Qoss * Vin * fsw``),
* gate-drive loss (``Qg * Vg * fsw``).

The model is an **averaged CCM approximation**, not a device-level or thermal
model.  The diode drop is applied as ``(1 - D) * I_L * Vf`` rather than being
solved from a switching waveform; switching ripple, dead time, reverse
recovery, magnetic core loss, and temperature dependence are not represented.
The declared terms are therefore an accounting identity, not a claim that
every physical loss mechanism has been captured.

Only the Python standard library is used.  The class implements the existing
:class:`pe_sim.contracts.PlantAdapter` protocol (``reset`` / ``observe`` /
``advance`` / ``snapshot`` / ``restore``) plus ``capabilities``,
``manifest_identity``, ``backend_provenance``, ``analytical_steady_state``, and
``loss_model``.  It is intentionally **not** registered as a runner plant.

The loss provider is opt-in: importing this module does not register anything
with :mod:`pe_sim.model_capabilities`.  Call :func:`register_loss_provider`
explicitly; the function is idempotent and never overwrites an existing
registration.
"""

from __future__ import annotations

from typing import Any, Mapping
import hashlib
import json
import math

from .contracts import PlantObservation
from .identity import (
    implementation_digest,
    implementation_sources,
    register_solve_path,
    review_status,
)
from .model_capabilities import (
    CapabilityUnavailable,
    LossBreakdown,
    OperatingPoint,
    register_loss_model,
    registered_model_capabilities,
)
from .safety import BoundedActionPolicy

_BACKEND_ID = "lossy-buck"
_SOLVE_PATH_MODULES = ("lossy_converter.py",)
_VERIFIED_BY = (
    "tests/test_lossy_converter.py::test_loss_terms_sum_to_the_power_balance"
)

#: Loss terms are an arithmetic partition: every watt must appear exactly once.
_TERM_KEYS = (
    "conduction_switch_w",
    "conduction_inductor_w",
    "conduction_diode_w",
    "switching_qoss_w",
    "gate_drive_w",
)

_ASSUMPTIONS = (
    "ccm_average_model",
    "conduction_and_switching_losses_only",
    "no_magnetic_core_loss",
    "no_temperature_dependence",
    "diode_drop_applied_as_duty_weighted_average",
)

# Declare the solve path in this process.  This is the same reviewed-identity
# mechanism as the project-owned reference plants; it records a digest and an
# honest ``not_pinned`` review status rather than inventing an approval.
register_solve_path(_BACKEND_ID, _SOLVE_PATH_MODULES)


def _finite_number(name: str, value: Any) -> float:
    """Return ``value`` as a finite float, rejecting booleans and strings."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name} must be a finite number")
    return number


def _non_negative(name: str, value: Any) -> float:
    number = _finite_number(name, value)
    if number < 0.0:
        raise ValueError(f"{name} must be finite and non-negative")
    return number


def _positive(name: str, value: Any) -> float:
    number = _finite_number(name, value)
    if number <= 0.0:
        raise ValueError(f"{name} must be finite and positive")
    return number


class LossyBuckPlant:
    """Averaged CCM buck with declared conduction and switching losses.

    State variables are the inductor current ``iL`` and the output voltage
    ``vout``, exactly as in :class:`pe_sim.reference_plants.BuckPlant`.  The
    loss parameters are configuration, not state, so they are not part of a
    snapshot; a restored plant keeps the parameters it was constructed with.
    """

    topology = "buck"
    model_kind = "lossy_averaged_reference"
    backend_id = _BACKEND_ID

    def __init__(
        self,
        *,
        input_voltage_v: float,
        inductance_h: float,
        capacitance_f: float,
        load_resistance_ohm: float,
        switch_resistance_ohm: float = 0.0,
        inductor_resistance_ohm: float = 0.0,
        diode_forward_voltage_v: float = 0.0,
        switching_frequency_hz: float = 100e3,
        switch_output_charge_c: float = 0.0,
        gate_charge_c: float = 0.0,
        gate_drive_voltage_v: float = 0.0,
        integration_step_s: float = 1e-6,
    ) -> None:
        self.input_voltage_v = _positive("input_voltage_v", input_voltage_v)
        self.inductance_h = _positive("inductance_h", inductance_h)
        self.capacitance_f = _positive("capacitance_f", capacitance_f)
        self.load_resistance_ohm = _positive(
            "load_resistance_ohm", load_resistance_ohm
        )
        self.integration_step_s = _positive(
            "integration_step_s", integration_step_s
        )
        self.switch_resistance_ohm = _non_negative(
            "switch_resistance_ohm", switch_resistance_ohm
        )
        self.inductor_resistance_ohm = _non_negative(
            "inductor_resistance_ohm", inductor_resistance_ohm
        )
        self.diode_forward_voltage_v = _non_negative(
            "diode_forward_voltage_v", diode_forward_voltage_v
        )
        self.switching_frequency_hz = _positive(
            "switching_frequency_hz", switching_frequency_hz
        )
        self.switch_output_charge_c = _non_negative(
            "switch_output_charge_c", switch_output_charge_c
        )
        self.gate_charge_c = _non_negative("gate_charge_c", gate_charge_c)
        self.gate_drive_voltage_v = _non_negative(
            "gate_drive_voltage_v", gate_drive_voltage_v
        )
        self.time_s = 0.0
        self.inductor_current_a = 0.0
        self.output_voltage_v = 0.0
        self.last_duty = 0.0

    # -- protocol ---------------------------------------------------------

    def capabilities(self) -> set[str]:
        return {
            "continuous_time",
            "external_input",
            "snapshot",
            "loss_model",
        }

    def action_policy(self) -> BoundedActionPolicy:
        return BoundedActionPolicy(0.0, 1.0, name="duty")

    def backend_provenance(self) -> dict[str, Any]:
        """Identify the deterministic project-owned Python solve path.

        This is not an Ngspice, device, or thermal claim.  ``status: known``
        describes the deterministic environment, not a code-review approval;
        the review state is carried separately in ``manifest_identity``.
        """

        return {
            "status": "known",
            "name": "pe_sim.lossy_averaged_python",
            "version": "lossy-reference-v1",
            "solver_settings": {
                "integrator": "rk4",
                "substep_s": self.integration_step_s,
                "floating_point": "IEEE-754-binary64",
            },
            "limitations": [
                "averaged CCM model; switching ripple and peak current are not represented",
                "declared losses are conduction and switching accounting terms, not a device calibration",
                "project-owned deterministic Python reference; not Ngspice or a physical device solver",
            ],
        }

    def manifest_identity(self) -> dict[str, Any]:
        """Return a portable, deterministic identity for a lossy run."""

        implementation = type(self)
        parameters = {
            "input_voltage_v": self.input_voltage_v,
            "inductance_h": self.inductance_h,
            "capacitance_f": self.capacitance_f,
            "load_resistance_ohm": self.load_resistance_ohm,
            "integration_step_s": self.integration_step_s,
            "switch_resistance_ohm": self.switch_resistance_ohm,
            "inductor_resistance_ohm": self.inductor_resistance_ohm,
            "diode_forward_voltage_v": self.diode_forward_voltage_v,
            "switching_frequency_hz": self.switching_frequency_hz,
            "switch_output_charge_c": self.switch_output_charge_c,
            "gate_charge_c": self.gate_charge_c,
            "gate_drive_voltage_v": self.gate_drive_voltage_v,
        }
        identity = {
            "level": "L1",
            "kind": self.model_kind,
            "topology": self.topology,
            "parameters": parameters,
            "capabilities": sorted(self.capabilities()),
            "implementation": {
                "module": implementation.__module__,
                "class": implementation.__qualname__,
                "solve_path_modules": sorted(
                    implementation_sources(self.backend_id)
                ),
                "sha256": implementation_digest(self.backend_id),
                "review": review_status(self.backend_id),
            },
        }
        identity["sha256"] = hashlib.sha256(
            json.dumps(
                identity,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            ).encode("utf-8")
        ).hexdigest()
        return identity

    # -- state protocol ---------------------------------------------------

    def reset(self, initial_state: Mapping[str, Any]) -> Mapping[str, Any]:
        state = dict(initial_state or {})
        self.time_s = 0.0
        self.inductor_current_a = float(
            state.get("inductor_current_a", state.get("current_a", 0.0))
        )
        self.output_voltage_v = float(
            state.get("output_voltage_v", state.get("vout", 0.0))
        )
        self.last_duty = 0.0
        self._validate_state()
        return self._state_dict()

    def observe(self) -> PlantObservation:
        self._validate_state()
        return PlantObservation(
            time_s=self.time_s,
            measurement={
                "vout": self.output_voltage_v,
                "inductor_current": self.inductor_current_a,
            },
            truth={
                "vout": self.output_voltage_v,
                "inductor_current": self.inductor_current_a,
                "duty": self.last_duty,
                "input_voltage": self.input_voltage_v,
                "load_resistance": self.load_resistance_ohm,
            },
            age_steps=0,
            measurement_units={"vout": "V", "inductor_current": "A"},
        )

    def advance(
        self,
        action: float,
        duration: float,
        external_input: Mapping[str, Any] | None = None,
    ) -> PlantObservation:
        duty = _finite_number("action duty", action)
        duration = _finite_number("duration", duration)
        if not 0.0 <= duty <= 1.0:
            raise ValueError("action duty must be finite and in [0, 1]")
        if duration <= 0.0:
            raise ValueError("duration must be finite and positive")
        self._apply_external_input(external_input)
        self.last_duty = duty
        remaining = duration
        while remaining > 0.0:
            step = min(remaining, self.integration_step_s)
            self._rk4_step(duty, step)
            self.time_s += step
            remaining -= step
        self._validate_state()
        return self.observe()

    def snapshot(self) -> Mapping[str, Any]:
        self._validate_state()
        return {
            "topology": self.topology,
            "time_s": self.time_s,
            "inductor_current_a": self.inductor_current_a,
            "output_voltage_v": self.output_voltage_v,
            "input_voltage_v": self.input_voltage_v,
            "load_resistance_ohm": self.load_resistance_ohm,
            "last_duty": self.last_duty,
        }

    def restore(self, snapshot: Mapping[str, Any]) -> Mapping[str, Any]:
        if str(snapshot.get("topology", self.topology)) != self.topology:
            raise ValueError("snapshot topology does not match plant")
        self.time_s = _finite_number("time_s", snapshot["time_s"])
        if self.time_s < 0.0:
            raise ValueError("time_s must be non-negative")
        self.inductor_current_a = _finite_number(
            "inductor_current_a", snapshot["inductor_current_a"]
        )
        self.output_voltage_v = _finite_number(
            "output_voltage_v", snapshot["output_voltage_v"]
        )
        self.input_voltage_v = _positive(
            "input_voltage_v", snapshot.get("input_voltage_v", self.input_voltage_v)
        )
        self.load_resistance_ohm = _positive(
            "load_resistance_ohm",
            snapshot.get("load_resistance_ohm", self.load_resistance_ohm),
        )
        self.last_duty = _finite_number(
            "last_duty", snapshot.get("last_duty", 0.0)
        )
        self._validate_state()
        return self._state_dict()

    # -- analytical model -------------------------------------------------

    def analytical_steady_state(self, duty: float) -> dict[str, float]:
        """Solve the averaged CCM equilibrium for a constant duty ratio.

        ``vout = D*Vin - (D*Rds + Rl)*IL - (1-D)*Vf`` with ``IL = vout / R``.
        A non-positive solution means the declared losses consume the entire
        input voltage at this duty; that operating point is rejected rather
        than reported as a negative or zero voltage.
        """

        duty = _finite_number("duty", duty)
        if not 0.0 <= duty <= 1.0:
            raise ValueError("duty must be finite and in [0, 1]")
        voltage, current = self._steady_state(duty)
        if (
            not math.isfinite(voltage)
            or not math.isfinite(current)
            or voltage <= 0.0
            or current <= 0.0
        ):
            raise ValueError(
                "the declared losses leave no positive steady state at this duty"
            )
        return {
            "output_voltage_v": voltage,
            "inductor_current_a": current,
        }

    def loss_model(self, operating_point: OperatingPoint) -> LossBreakdown:
        """Return the declared loss breakdown at ``operating_point``.

        The operating point is evaluated with :meth:`analytical_steady_state`,
        so every term comes from one consistent equilibrium rather than from
        the transient state.  ``declared_lossless`` is only true when the
        model declares no loss mechanism at all; a lossy model whose terms
        round to zero raises instead of claiming lossless efficiency.
        """

        if not isinstance(operating_point, OperatingPoint):
            raise TypeError("operating_point must be an OperatingPoint")
        steady = self.analytical_steady_state(operating_point.duty)
        duty = operating_point.duty
        current = steady["inductor_current_a"]
        voltage = steady["output_voltage_v"]

        terms = {
            "conduction_switch_w": (
                duty
                * current
                * current
                * self.switch_resistance_ohm
            ),
            "conduction_inductor_w": (
                current * current * self.inductor_resistance_ohm
            ),
            "conduction_diode_w": (
                (1.0 - duty) * current * self.diode_forward_voltage_v
            ),
            "switching_qoss_w": (
                self.switch_output_charge_c
                * self.input_voltage_v
                * self.switching_frequency_hz
            ),
            "gate_drive_w": (
                self.gate_charge_c
                * self.gate_drive_voltage_v
                * self.switching_frequency_hz
            ),
        }
        # Plain ``sum`` over the same mapping the value object re-adds keeps
        # ``total_loss_w == sum(terms.values())`` exact, not merely close.
        total_loss_w = sum(terms.values())
        output_power_w = voltage * voltage / self.load_resistance_ohm
        input_power_w = output_power_w + total_loss_w
        efficiency = output_power_w / input_power_w
        declared_lossless = self._declares_no_loss()

        if not declared_lossless and efficiency >= 1.0 - 1e-12:
            raise ValueError(
                "declared loss parameters are too small to resolve a lossy "
                "power balance at double precision; increase them or remove "
                "the loss declarations"
            )

        return LossBreakdown(
            plant_id=self.backend_id,
            method="analytic",
            terms=terms,
            total_loss_w=total_loss_w,
            output_power_w=output_power_w,
            input_power_w=input_power_w,
            efficiency=efficiency,
            assumptions=_ASSUMPTIONS,
            verified_by=_VERIFIED_BY,
            declared_lossless=declared_lossless,
            operating_point={
                "duty": duty,
                "vout": voltage,
                "il": current,
            },
            origin="caller_registered",
            plant_verified_by=_VERIFIED_BY,
        )

    # -- internals --------------------------------------------------------

    def _declares_no_loss(self) -> bool:
        return (
            self.switch_resistance_ohm == 0.0
            and self.inductor_resistance_ohm == 0.0
            and self.diode_forward_voltage_v == 0.0
            and self.switch_output_charge_c == 0.0
            and self.gate_charge_c == 0.0
        )

    def _apply_external_input(
        self, external_input: Mapping[str, Any] | None
    ) -> None:
        values = dict(external_input or {})
        if "vin_v" in values or "input_voltage_v" in values:
            value = (
                values["vin_v"]
                if "vin_v" in values
                else values["input_voltage_v"]
            )
            self.input_voltage_v = _positive("input_voltage_v", value)
        if "load_ohm" in values or "load_resistance_ohm" in values:
            value = (
                values["load_ohm"]
                if "load_ohm" in values
                else values["load_resistance_ohm"]
            )
            self.load_resistance_ohm = _positive("load_resistance_ohm", value)

    def _state_dict(self) -> dict[str, float]:
        return {
            "time_s": self.time_s,
            "inductor_current_a": self.inductor_current_a,
            "output_voltage_v": self.output_voltage_v,
        }

    def _validate_state(self) -> None:
        for name in ("time_s", "inductor_current_a", "output_voltage_v"):
            if not math.isfinite(float(getattr(self, name))):
                raise ValueError(f"{name} must remain finite")
        if self.time_s < 0.0:
            raise ValueError("time_s must remain non-negative")

    def _derivatives(
        self, duty: float, current: float, voltage: float
    ) -> tuple[float, float]:
        switch_drop = current * self.switch_resistance_ohm
        inductor_drop = current * self.inductor_resistance_ohm
        diode_drop = (1.0 - duty) * self.diode_forward_voltage_v
        return (
            (
                duty * (self.input_voltage_v - switch_drop)
                - inductor_drop
                - diode_drop
                - voltage
            )
            / self.inductance_h,
            (current - voltage / self.load_resistance_ohm)
            / self.capacitance_f,
        )

    def _steady_state(self, duty: float) -> tuple[float, float]:
        series_drop = (
            duty * self.switch_resistance_ohm + self.inductor_resistance_ohm
        )
        numerator = (
            duty * self.input_voltage_v
            - (1.0 - duty) * self.diode_forward_voltage_v
        )
        voltage = numerator / (1.0 + series_drop / self.load_resistance_ohm)
        return voltage, voltage / self.load_resistance_ohm

    def _rk4_step(self, duty: float, step: float) -> None:
        i0, v0 = self.inductor_current_a, self.output_voltage_v
        k1i, k1v = self._derivatives(duty, i0, v0)
        k2i, k2v = self._derivatives(
            duty, i0 + 0.5 * step * k1i, v0 + 0.5 * step * k1v
        )
        k3i, k3v = self._derivatives(
            duty, i0 + 0.5 * step * k2i, v0 + 0.5 * step * k2v
        )
        k4i, k4v = self._derivatives(
            duty, i0 + step * k3i, v0 + step * k3v
        )
        self.inductor_current_a = i0 + step * (
            k1i + 2.0 * k2i + 2.0 * k3i + k4i
        ) / 6.0
        self.output_voltage_v = v0 + step * (
            k1v + 2.0 * k2v + 2.0 * k3v + k4v
        ) / 6.0


def _loss_factory(
    plant: Any, operating_point: OperatingPoint
) -> LossBreakdown:
    """Serve only :class:`LossyBuckPlant` under the ``lossy-buck`` key."""

    if type(plant).__name__ != "LossyBuckPlant":
        raise CapabilityUnavailable(
            "loss_model_not_declared",
            "the lossy-buck loss provider serves only LossyBuckPlant; "
            f"got {type(plant).__name__}",
        )
    return plant.loss_model(operating_point)


def register_loss_provider() -> None:
    """Register the ``lossy-buck`` loss factory, idempotently.

    An existing registration under this key is left untouched, including a
    caller-provided override.  Importing this module does not register it;
    the caller must opt in explicitly.
    """

    capabilities = registered_model_capabilities().get(_BACKEND_ID, ())
    if "loss_model" in capabilities:
        return
    register_loss_model(_BACKEND_ID, _loss_factory)


__all__ = ["LossyBuckPlant", "register_loss_provider"]

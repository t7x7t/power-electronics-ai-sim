"""Controller-side protection state machine: OVP, OCP, UVLO, hiccup or latch.

Why a wrapper instead of kernel work: ``docs/60`` row B9 (protection state
machine) and row A7 (short circuit / hiccup recovery) are both expressible
with the existing controller protocol.  The kernel still issues exactly one
``ActionRequest`` per control window; a wrapper can observe the same
measurement the inner controller sees and substitute a trip action.  No
kernel, plant, or adapter protocol is changed, so T1 (new behaviour is
composition, not a kernel branch) still holds.

Evidence ceiling: protection is evaluated at the control sampling instants
only.  A fault that occurs between two samples is therefore acted upon in the
**next control period**; this wrapper is not cycle-by-cycle comparator
hardware and makes no such claim.  The ceiling is published as
``ProtectedController.declared_limitations ==
("protection_latency_is_one_control_period",)`` and is the limit registered
for row A7 in ``docs/61_gap_closure_plan_v1.md`` §2.3.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Mapping

from .contracts import ActionRequest


#: Protection states are plain strings so they survive JSON checkpoints and
#: appear verbatim in ``samples``/manifest audits.
PROTECTION_NORMAL = "normal"
PROTECTION_TRIPPED = "tripped"
PROTECTION_LATCHED = "latched"

#: Closed state vocabulary.
PROTECTION_STATES: tuple[str, ...] = (
    PROTECTION_NORMAL,
    PROTECTION_TRIPPED,
    PROTECTION_LATCHED,
)

#: Capability advertised in addition to whatever the inner controller declares.
PROTECTION_CAPABILITY = "protection"


def _finite_number(value: Any, *, field: str) -> float:
    """Return ``value`` as a finite float, rejecting booleans and bad types."""

    if isinstance(value, bool):
        raise ValueError(f"{field} must be a finite number, not a boolean")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field} must be a finite number") from None
    if not math.isfinite(number):
        raise ValueError(f"{field} must be a finite number")
    return number


def _optional_positive(value: Any, *, field: str) -> float | None:
    """Validate an optional protection threshold (``None`` disables it)."""

    if value is None:
        return None
    number = _finite_number(value, field=field)
    if number <= 0.0:
        raise ValueError(f"{field} must be finite and positive")
    return number


@dataclass(frozen=True)
class ProtectionPolicy:
    """Declarative protection thresholds and recovery behaviour.

    Every threshold is optional: ``None`` disables that protection, so a
    policy may express any subset of OVP, OCP, and UVLO.  An enabled
    threshold must be finite and positive; ``trip_duty`` must be a duty ratio
    in ``[0, 1]``; ``hiccup_s`` must be finite and positive when enabled.

    ``hiccup_s is None`` means *latch*: after the first trip the controller
    never recovers.  A finite ``hiccup_s`` means *hiccup*: the trip action is
    held for at least that many seconds of control time, then the inner
    controller is retried once.

    ``uvlo_startup_mask`` keeps a cold start from tripping on its own initial
    zero output: UVLO is armed only after the measured voltage has first
    reached ``uvlo_v`` once.  Disable the mask to protect a pre-biased or
    externally supplied rail that is already below the threshold.

    ``voltage_key``/``current_key`` name the measurement channels to watch;
    the runner-provided measurement for the reference buck is ``vout`` and
    ``inductor_current``.
    """

    ovp_v: float | None = None
    uvlo_v: float | None = None
    ocp_a: float | None = None
    trip_duty: float = 0.0
    hiccup_s: float | None = None
    voltage_key: str = "vout"
    current_key: str = "inductor_current"
    uvlo_startup_mask: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "ovp_v", _optional_positive(self.ovp_v, field="ovp_v"))
        object.__setattr__(
            self, "uvlo_v", _optional_positive(self.uvlo_v, field="uvlo_v")
        )
        object.__setattr__(self, "ocp_a", _optional_positive(self.ocp_a, field="ocp_a"))
        duty = _finite_number(self.trip_duty, field="trip_duty")
        if not 0.0 <= duty <= 1.0:
            raise ValueError("trip_duty must be in [0, 1]")
        object.__setattr__(self, "trip_duty", duty)
        object.__setattr__(
            self, "hiccup_s", _optional_positive(self.hiccup_s, field="hiccup_s")
        )
        for name in ("voltage_key", "current_key"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"{name} must be a non-empty string")
        if not isinstance(self.uvlo_startup_mask, bool):
            raise ValueError("uvlo_startup_mask must be a boolean")

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe declaration of the policy (identity payload)."""

        return {
            "ovp_v": self.ovp_v,
            "uvlo_v": self.uvlo_v,
            "ocp_a": self.ocp_a,
            "trip_duty": self.trip_duty,
            "hiccup_s": self.hiccup_s,
            "voltage_key": self.voltage_key,
            "current_key": self.current_key,
            "uvlo_startup_mask": self.uvlo_startup_mask,
        }


class ProtectedController:
    """Compose OVP/OCP/UVLO protection around an inner controller.

    The wrapper implements the controller protocol
    (``observe``/``reset``) and additionally exposes ``snapshot``/``restore``
    so checkpoints keep the protection state, plus ``capabilities`` and a
    portable ``manifest_identity``.

    Behaviour per state:

    ``normal``
        The measurement is checked against OVP, then OCP, then UVLO.  On a
        fault the wrapper counts the trip and returns ``trip_duty``; otherwise
        the inner controller runs unchanged.
    ``tripped``
        The trip action is held.  ``hiccup_s`` accumulates one
        ``control_period_s`` per sample; when it elapses the wrapper returns
        to ``normal`` and delegates to the inner controller **on that same
        step**, which is the visible retry.
    ``latched``
        ``hiccup_s is None``.  The trip action is returned forever.

    Latency ceiling: protection is evaluated once per control sample, so the
    fault is executed in the next control window.  That is the A7 limitation
    ``protection_latency_is_one_control_period`` registered in
    ``docs/61_gap_closure_plan_v1.md`` §2.3 and published through
    ``declared_limitations``.  This is not a cycle-by-cycle comparator claim.

    The protection state is owned by the wrapper (not by the ``state``
    argument), and is mirrored into every ``ActionRequest.state_update`` so a
    run trajectory can be audited without instrumenting the wrapper.
    """

    NORMAL = PROTECTION_NORMAL
    TRIPPED = PROTECTION_TRIPPED
    LATCHED = PROTECTION_LATCHED

    #: Evidence ceiling, see the class docstring and docs/61 §2.3 (row A7).
    declared_limitations: tuple[str, ...] = (
        "protection_latency_is_one_control_period",
    )

    def __init__(
        self,
        inner: Any,
        policy: ProtectionPolicy,
        *,
        name: str = "protected-controller",
    ) -> None:
        if not isinstance(policy, ProtectionPolicy):
            raise TypeError("policy must be a ProtectionPolicy")
        if not isinstance(name, str) or not name:
            raise ValueError("name must be a non-empty string")
        for method in ("observe", "reset"):
            if not callable(getattr(inner, method, None)):
                raise TypeError(f"inner controller missing {method}()")
        self.inner = inner
        self.policy = policy
        self.name = name
        self._state = PROTECTION_NORMAL
        self._elapsed_s = 0.0
        self._trips = 0
        self._started = False

    # ------------------------------------------------------------------
    # Controller protocol
    # ------------------------------------------------------------------

    def observe(
        self,
        measurement: Mapping[str, float],
        command: Mapping[str, Any],
        timing: Mapping[str, Any],
        state: Mapping[str, Any],
    ) -> ActionRequest:
        """Check the sample, then either trip or delegate to the inner controller."""

        policy = self.policy
        control_period_s = _finite_number(
            timing.get("control_period_s"), field="timing.control_period_s"
        )
        if control_period_s <= 0.0:
            raise ValueError("timing.control_period_s must be positive")
        now = _finite_number(timing.get("sample_time_s"), field="timing.sample_time_s")
        vref = command.get("vref")
        if vref is not None:
            _finite_number(vref, field="command.vref")

        voltage = self._reading(
            measurement,
            policy.voltage_key,
            required=policy.ovp_v is not None or policy.uvlo_v is not None,
        )
        current = self._reading(
            measurement,
            policy.current_key,
            required=policy.ocp_a is not None,
        )

        if self._state == PROTECTION_LATCHED:
            return self._trip_request(now)

        if self._state == PROTECTION_TRIPPED:
            self._elapsed_s += control_period_s
            # A policy with ``hiccup_s=None`` latches instead of tripping, so a
            # tripped controller always has a finite retry delay.
            hiccup_s = policy.hiccup_s
            if hiccup_s is not None and self._elapsed_s >= hiccup_s:
                self._state = PROTECTION_NORMAL
                self._elapsed_s = 0.0
                return self._delegate(measurement, command, timing, state)
            return self._trip_request(now)

        if policy.uvlo_v is not None and voltage is not None:
            if voltage >= policy.uvlo_v:
                self._started = True

        if self._fault(voltage, current) is not None:
            self._trips += 1
            self._elapsed_s = 0.0
            self._state = (
                PROTECTION_LATCHED if policy.hiccup_s is None else PROTECTION_TRIPPED
            )
            return self._trip_request(now)

        return self._delegate(measurement, command, timing, state)

    def reset(
        self,
        controller_state: Mapping[str, Any] | None,
        seed: int,
    ) -> Mapping[str, Any]:
        """Restore protection state (missing values use initial state) and inner.

        ``controller_state`` may be ``None``, a mapping in this wrapper's
        snapshot shape, or a flat ``state_update`` mapping.  The inner
        controller receives its own slice when the nested shape is used.
        """

        state = dict(controller_state or {})
        policy_payload = state.get("policy")
        if policy_payload is not None:
            self._require_matching_policy(policy_payload)
        protection = self._protection_entries(state)
        if protection is None:
            self._reset_protection()
        else:
            self._restore_protection(protection)

        if "inner" in state:
            inner_state = state["inner"]
            if not isinstance(inner_state, Mapping):
                raise ValueError("controller_state inner must be a mapping")
        else:
            inner_state = state if state else None

        reset_fn = getattr(self.inner, "reset", None)
        inner_result = reset_fn(inner_state, seed) if callable(reset_fn) else None
        if inner_result is not None and not isinstance(inner_result, Mapping):
            raise TypeError("inner reset() must return a serializable mapping")
        return {
            "inner": dict(inner_result or {}),
            "protection": self._protection_snapshot(),
            "policy": self.policy.to_dict(),
        }

    # ------------------------------------------------------------------
    # Checkpointing and declaration
    # ------------------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        """Return ``{"inner", "protection", "policy"}`` for checkpointing.

        The inner controller must provide ``snapshot()``; an inner controller
        without one cannot be checkpointed and is rejected instead of being
        silently restored with default internal state.
        """

        snapshot_fn = getattr(self.inner, "snapshot", None)
        if not callable(snapshot_fn):
            raise TypeError(
                "inner controller must provide snapshot() for "
                "ProtectedController.snapshot()"
            )
        inner_snapshot = snapshot_fn()
        if not isinstance(inner_snapshot, Mapping):
            raise TypeError("inner snapshot() must return a mapping")
        return {
            "inner": dict(inner_snapshot),
            "protection": self._protection_snapshot(),
            "policy": self.policy.to_dict(),
        }

    def restore(self, snapshot: Mapping[str, Any]) -> Mapping[str, Any]:
        """Restore a snapshot produced by :meth:`snapshot`; reject mismatches."""

        state = dict(snapshot or {})
        unknown = set(state) - {"inner", "protection", "policy"}
        if unknown:
            raise ValueError(f"protection snapshot has unknown keys: {sorted(unknown)}")
        missing = {"inner", "protection", "policy"} - set(state)
        if missing:
            raise ValueError(f"protection snapshot is missing keys: {sorted(missing)}")

        inner_snapshot = state["inner"]
        if not isinstance(inner_snapshot, Mapping):
            raise ValueError("protection snapshot inner must be a mapping")

        self._require_matching_policy(state["policy"])
        self._restore_protection(state["protection"])

        restore_fn = getattr(self.inner, "restore", None)
        if callable(restore_fn):
            restore_fn(inner_snapshot)
        else:
            reset_fn = getattr(self.inner, "reset", None)
            if not callable(reset_fn):
                raise ValueError(
                    "inner controller provides neither restore() nor reset()"
                )
            reset_fn(dict(inner_snapshot), 0)

        return {
            "inner": dict(inner_snapshot),
            "protection": self._protection_snapshot(),
            "policy": self.policy.to_dict(),
        }

    def capabilities(self) -> set[str]:
        """Return the inner capabilities plus ``"protection"``."""

        inner_capabilities = getattr(self.inner, "capabilities", None)
        declared = set(inner_capabilities()) if callable(inner_capabilities) else set()
        return {str(item) for item in declared} | {PROTECTION_CAPABILITY}

    def manifest_identity(self) -> dict[str, Any]:
        """Return a JSON-safe identity: the policy plus the inner controller."""

        inner_identity_fn = getattr(self.inner, "manifest_identity", None)
        if callable(inner_identity_fn):
            inner_identity = dict(inner_identity_fn())
        else:
            inner_identity = {
                "kind": "runtime_component",
                "module": type(self.inner).__module__,
                "class": type(self.inner).__qualname__,
            }
        return {
            "kind": "protection_wrapper",
            "module": type(self).__module__,
            "class": type(self).__qualname__,
            "name": self.name,
            "parameters": self.policy.to_dict(),
            "inner": inner_identity,
            "capabilities": sorted(self.capabilities()),
            "declared_limitations": list(self.declared_limitations),
        }

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _reading(
        measurement: Mapping[str, float],
        key: str,
        *,
        required: bool,
    ) -> float | None:
        if key not in measurement:
            if required:
                raise ValueError(f"measurement does not contain {key!r}")
            return None
        return _finite_number(measurement[key], field=f"measurement[{key!r}]")

    def _fault(self, voltage: float | None, current: float | None) -> str | None:
        policy = self.policy
        if policy.ovp_v is not None and voltage is not None:
            if voltage >= policy.ovp_v:
                return "ovp"
        if policy.ocp_a is not None and current is not None:
            if current >= policy.ocp_a:
                return "ocp"
        if policy.uvlo_v is not None and voltage is not None:
            if (self._started or not policy.uvlo_startup_mask) and (
                voltage <= policy.uvlo_v
            ):
                return "uvlo"
        return None

    def _delegate(
        self,
        measurement: Mapping[str, float],
        command: Mapping[str, Any],
        timing: Mapping[str, Any],
        state: Mapping[str, Any],
    ) -> ActionRequest:
        """Run the inner controller and attach the protection audit keys."""

        request = self.inner.observe(measurement, command, timing, state)
        if not isinstance(request, ActionRequest):
            raise TypeError("inner controller must return ActionRequest")
        update = dict(request.state_update or {})
        update.update(self._state_update())
        return ActionRequest(
            value=request.value,
            unit=request.unit,
            produced_time_s=request.produced_time_s,
            target_time_s=request.target_time_s,
            diagnostics=dict(request.diagnostics),
            state_update=update,
        )

    def _trip_request(self, now: float) -> ActionRequest:
        return ActionRequest(
            value=self.policy.trip_duty,
            produced_time_s=now,
            target_time_s=now,
            state_update=self._state_update(),
        )

    def _state_update(self) -> dict[str, Any]:
        return {
            "protection_state": self._state,
            "protection_trips": self._trips,
            "protection_elapsed_s": self._elapsed_s,
            "protection_started": self._started,
        }

    def _protection_snapshot(self) -> dict[str, Any]:
        return {
            "state": self._state,
            "elapsed_s": self._elapsed_s,
            "trips": self._trips,
            "started": self._started,
        }

    def _reset_protection(self) -> None:
        self._state = PROTECTION_NORMAL
        self._elapsed_s = 0.0
        self._trips = 0
        self._started = False

    def _require_matching_policy(self, payload: Any) -> None:
        if not isinstance(payload, Mapping):
            raise ValueError("protection snapshot policy must be a mapping")
        if dict(payload) != self.policy.to_dict():
            raise ValueError(
                "protection snapshot policy does not match this controller"
            )

    @staticmethod
    def _protection_entries(state: Mapping[str, Any]) -> Mapping[str, Any] | None:
        """Find a nested or flat protection payload, or ``None`` for defaults."""

        nested = state.get("protection")
        if isinstance(nested, Mapping):
            return nested
        if "protection_state" in state:
            return {
                "state": state["protection_state"],
                "elapsed_s": state.get("protection_elapsed_s", 0.0),
                "trips": state.get("protection_trips", 0),
                "started": state.get("protection_started", False),
            }
        return None

    def _restore_protection(self, values: Mapping[str, Any]) -> None:
        if not isinstance(values, Mapping):
            raise ValueError("protection snapshot must be a mapping")
        unknown = set(values) - {"state", "elapsed_s", "trips", "started"}
        if unknown:
            raise ValueError(
                f"protection snapshot has unknown keys: {sorted(unknown)}"
            )
        if "state" not in values:
            raise ValueError("protection snapshot is missing state")
        state = values["state"]
        if state not in PROTECTION_STATES:
            raise ValueError(f"unsupported protection state: {state!r}")
        elapsed_s = _finite_number(
            values.get("elapsed_s", 0.0), field="protection elapsed_s"
        )
        if elapsed_s < 0.0:
            raise ValueError("protection elapsed_s must be non-negative")
        trips = values.get("trips", 0)
        if isinstance(trips, bool) or not isinstance(trips, int) or trips < 0:
            raise ValueError("protection trips must be a non-negative integer")
        started = values.get("started", False)
        if not isinstance(started, bool):
            raise ValueError("protection started must be a boolean")
        self._state = state
        self._elapsed_s = elapsed_s
        self._trips = trips
        self._started = started

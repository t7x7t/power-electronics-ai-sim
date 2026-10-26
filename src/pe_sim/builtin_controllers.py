"""Project-owned composition root for controllers.

The registry is data; this module supplies the data for the controllers the
project ships.  Importing it is cheap: it registers factories and does not
construct a controller, start a run, or read a configuration file.
"""

from __future__ import annotations

from .controller_registry import (
    ControllerRegistration,
    register_controller,
    registered_controllers,
)
from .controllers import (
    BIQUAD_CONFIG_KEYS,
    TYPE2_CONFIG_KEYS,
    TYPE3_CONFIG_KEYS,
    BiquadController,
    Type2Controller,
    Type3Controller,
)
from .runtime import FakePIController


_PI_CONFIG_KEYS = frozenset(
    {"kp", "ki", "output_min", "output_max", "anti_windup", "measurement_key"}
)
_PID_CONFIG_KEYS = _PI_CONFIG_KEYS | {"kd", "kd_filter_tau_s", "kff_v"}


def _build_pi(config, context):
    del context
    return FakePIController.from_config(config)


def _build_pid(config, context):
    del context
    return FakePIController.from_config(config)


def _build_type2(config, context):
    return Type2Controller.from_config(
        config, control_period_s=context.control_period_s
    )


def _build_type3(config, context):
    return Type3Controller.from_config(
        config, control_period_s=context.control_period_s
    )


def _build_biquad(config, context):
    return BiquadController.from_config(
        config, control_period_s=context.control_period_s
    )


_BUILTIN_CONTROLLER_REGISTRATIONS: tuple[ControllerRegistration, ...] = (
    ControllerRegistration(
        controller_id="pi",
        factory=_build_pi,
        config_keys=_PI_CONFIG_KEYS,
        class_name="FakePIController",
    ),
    ControllerRegistration(
        controller_id="pid",
        factory=_build_pid,
        config_keys=_PID_CONFIG_KEYS,
        class_name="FakePIController",
    ),
    ControllerRegistration(
        controller_id="type2",
        factory=_build_type2,
        config_keys=TYPE2_CONFIG_KEYS,
        class_name="Type2Controller",
    ),
    ControllerRegistration(
        controller_id="type3",
        factory=_build_type3,
        config_keys=TYPE3_CONFIG_KEYS,
        class_name="Type3Controller",
    ),
    ControllerRegistration(
        controller_id="biquad",
        factory=_build_biquad,
        config_keys=BIQUAD_CONFIG_KEYS,
        class_name="BiquadController",
    ),
)


def builtin_controller_registrations() -> tuple[ControllerRegistration, ...]:
    """Return the project-owned registrations without touching the registry."""

    return _BUILTIN_CONTROLLER_REGISTRATIONS


def load_builtin_controllers() -> None:
    """Register the shipped controllers.  Calling it twice is a no-op.

    The whole set is checked before anything is registered, so a conflict
    cannot leave the registry holding half of the built-in controllers.
    """

    existing = registered_controllers()
    pending: list[ControllerRegistration] = []
    for registration in _BUILTIN_CONTROLLER_REGISTRATIONS:
        current = existing.get(registration.controller_id)
        if current == registration:
            continue
        if current is not None:
            raise ValueError(
                f"controller_id {registration.controller_id!r} is already "
                f"registered by class {current.class_name!r}; unregister it "
                "before loading the built-in controllers"
            )
        pending.append(registration)
    for registration in pending:
        register_controller(registration)


__all__ = [
    "builtin_controller_registrations",
    "load_builtin_controllers",
]

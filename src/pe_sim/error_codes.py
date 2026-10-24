"""Stable codes for rejected requests and kernel-produced failure categories.

``REJECTION_SCHEMA`` tags the machine-readable envelope the CLI prints when a
request is rejected before a run package exists.  ``REQUEST_REJECTION_CODES``
is the closed set a client may branch on for that envelope.

``KERNEL_FAILURE_CODES`` documents the ``failure.category`` values the kernel
itself can produce: the fallback classifier in ``runtime.py``, the hard-coded
``cancelled``/``interrupted`` branches, the ``category`` attributes of the
exceptions declared in ``contracts.py``, ``checks.py`` and ``applicability.py``,
and the finding categories emitted by the kernel's built-in observation
validity and model-envelope checks (a raised ``CheckFailureError`` takes the
category of the finding that stopped the run).

It is **not** a whitelist.  An adapter or plugin may declare its own exception
or finding ``category``, and that value passes through to the manifest
unchanged.  Clients must therefore treat any category outside this set as
*unverified*: recorded as given, but not classified by the kernel.

This module is stdlib-only so a client can import it without loading the
simulation kernel.
"""

from __future__ import annotations

from typing import Any, Mapping


#: Schema tag for the envelope printed when a CLI request is rejected.
REJECTION_SCHEMA = "pe-sim.rejection.v1"

#: The closed vocabulary of rejection codes.  A new code is a contract event.
REQUEST_REJECTION_CODES = frozenset(
    {
        "invalid_request",
        "unknown_plant",
        "backend_mismatch",
        "unknown_backend",
        "formal_comparison_rejected",
        "run_inspection_rejected",
    }
)

#: Categories the kernel itself can write to ``manifest["failure"]["category"]``.
KERNEL_FAILURE_CODES = frozenset(
    {
        # Runtime classifier and exception-declared categories.
        "action_invalid",
        "backend_process_failure",
        "backend_timeout",
        "cancelled",
        "capability_configuration_mismatch",
        "capability_mismatch",
        "capability_runtime_unavailable",
        "check_failed",
        "data_invalid",
        "interrupted",
        "invalid_observation_age",
        "missing_capability",
        "model_numerical_error",
        "nonfinite_observation",
        "numerical_nonconvergence",
        "out_of_envelope",
        "plant_advance_failure",
        "plant_execution_failure",
        "qualification_failure",
        "runtime_error",
        "safety_violation",
        "simulation_execution_error",
        "timestamp_violation",
        "timing_violation",
        "unit_mismatch",
        "unsupported_capability",
        # Built-in observation-validity findings.
        "future_observation",
        "invalid_measurement",
        "measurement_out_of_range",
        "missing_measurement",
        "stale_observation",
        # Plugin boundary: a check plugin that raised instead of returning.
        "checker_error",
        # Built-in model-envelope (applicability) findings.
        "actuator_outside_envelope",
        "actuator_undeclared",
        "ccm_boundary_violated",
        "control_period_too_short",
        "envelope_declaration_invalid",
        "envelope_undeclared",
        "external_input_changed",
        "insufficient_integration_substeps",
        "insufficient_sample_resolution",
        "integration_step_too_large",
        "invalid_schedule_value",
        "load_too_close_to_switch_off_resistance",
        "parameter_provenance_invalid",
        "parameter_provenance_unknown",
        "parameter_unreviewed",
        "pulse_width_below_template_resolution",
    }
)


class RequestRejected(ValueError):
    """A rejected request carrying a stable code a client can branch on."""

    def __init__(
        self,
        code: str,
        message: str,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        if code not in REQUEST_REJECTION_CODES:
            raise ValueError(f"unknown request rejection code: {code!r}")
        self.code = code
        self.details = details
        super().__init__(message)


def rejection_document(
    status: str,
    code: str,
    error: str,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the versioned rejection envelope for one exit-2 outcome.

    Values in ``extra`` must already be JSON-safe; this helper does not
    serialize or coerce them.
    """

    if not isinstance(status, str) or not status:
        raise ValueError("status must be a non-empty string")
    if code not in REQUEST_REJECTION_CODES:
        raise ValueError(f"unknown request rejection code: {code!r}")
    if not isinstance(error, str):
        raise ValueError("error must be a string")
    document: dict[str, Any] = dict(extra or {})
    # The contract keys win over a colliding caller key.
    document.update(
        {
            "schema": REJECTION_SCHEMA,
            "status": status,
            "error_code": code,
            "error": error,
        }
    )
    return document


__all__ = [
    "KERNEL_FAILURE_CODES",
    "REJECTION_SCHEMA",
    "REQUEST_REJECTION_CODES",
    "RequestRejected",
    "rejection_document",
]

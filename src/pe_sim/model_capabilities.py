"""Optional model capabilities below the plant: small-signal and loss models.

This module is the first slice of Seam 2 in ``docs/adr/0005`` and closes the
model-capability half of ``docs/60`` C4.  It answers questions that
``PlantAdapter.advance()`` cannot answer without changing the runtime: "what is
the control-to-output transfer function at this operating point?" and, in a
later slice, "where does the loss go?".

Design rules implemented here
-----------------------------

* **Absence is reported, never inferred.**  A plant without a registered
  provider raises :class:`CapabilityUnavailable` with a machine-readable
  ``reason``.  There is no ``limited`` result and no silent approximation.
  An ideal lossless converter therefore has **no** loss model: reporting
  ``efficiency = 1.0`` would be a misleading claim about a model that has no
  loss terms at all.
* **Registration is explicit.**  Built-ins are installed only by
  :func:`load_builtin_model_capabilities`; importing this module has no side
  effect.  The loader never overwrites an existing key, so repeated calls are
  idempotent and a caller-provided override survives a reload.
* **Lookup order.**  For ``small_signal_model_for`` and ``loss_model_for`` the
  key is resolved as follows:

  1. ``getattr(plant, "backend_id", None)`` when it is a non-empty string;
  2. ``type(plant).__name__``;
  3. if neither key is registered, :class:`CapabilityUnavailable` is raised.
     The message names both attempted keys.

  The order is intentional: a plant's ``backend_id`` is its declared solve-path
  identity (for the reference plants these are ``"buck"`` and ``"boost"``),
  while the class name is only a fallback for duck-typed providers.
* **The state-space shape is the existing one.**  ``LinearStateSpace`` is
  imported from ``pe_sim.backends.whole_run_engine``; no second state-space
  type is introduced.
* **No evidence levels, and no evidence-level words.**  Capability results
  self-report ``method``, ``assumptions``, ``verified_by``, and (for
  small-signal models) ``origin``.  They do not carry ``diagnostic_only`` /
  ``functional`` / ``limited`` / ``comparable``; those words are rejected as
  tokens in ``method`` and ``verified_by`` and as whole assumption entries, so
  a capability string cannot be mistaken for a run qualification.  Token
  comparison is deliberate: ``unlimited_...`` does not contain the evidence
  level ``limited``.
* **Origin is explicit.**  ``origin`` is ``"builtin"`` only for providers
  installed by :func:`load_builtin_model_capabilities`; every provider that a
  caller registers defaults to ``"caller_registered"``.  AC results record the
  origin and add the assumption
  ``capability_provider_is_caller_registered`` in that case.
* **The provider must answer for the key it was found under.**  A provider
  registered under a lookup key that returns a model with a different
  ``plant_id`` raises :class:`CapabilityUnavailable` with reason
  ``model_plant_id_mismatch``; a provider cannot silently answer for another
  plant.

Reason codes produced by this module
------------------------------------

``small_signal_model_not_declared``
    No small-signal provider is registered for either lookup key.
``loss_model_not_declared``
    No loss provider is registered for either lookup key.
``operating_point_not_declared``
    The plant has no usable ``analytical_steady_state(duty)`` and the supplied
    :class:`OperatingPoint` does not declare the voltage/load needed to compute
    the equilibrium, or the equilibrium is singular (boost at ``duty == 1``).
``small_signal_parameters_not_declared``
    A registered analytic provider needs positive ``inductance_h`` and
    ``capacitance_f`` on the plant and they are absent or invalid.  This is a
    malformed provider/plant pairing, not a reason to approximate.
``model_plant_id_mismatch``
    The provider found under a lookup key returned a model whose ``plant_id``
    is different from that key.  The message names both values.

Only the Python standard library is used.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Callable, Mapping

from .backends.whole_run_engine import LinearStateSpace
from .time_model import RUNTIME_EVIDENCE_LEVELS


CAPABILITY_SCHEMA = "pe-sim.model-capability.v1"
LINEARIZATION_METHODS = ("analytic", "identified")
CAPABILITY_ORIGINS = ("builtin", "caller_registered")

_VERIFIED_BY_PATTERN = re.compile(
    r"^tests/[A-Za-z0-9_./-]+\.py::[A-Za-z0-9_\[\].-]+$"
)
_ASSUMPTION_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
_WORD_PATTERN = re.compile(r"[A-Za-z0-9]+")
_EVIDENCE_LEVELS = frozenset(RUNTIME_EVIDENCE_LEVELS)
_RELATIVE_TOLERANCE = 1e-9

#: Capability names reported by :func:`registered_model_capabilities`.
_SMALL_SIGNAL_CAPABILITY = "small_signal_model"
_LOSS_CAPABILITY = "loss_model"


class CapabilityUnavailable(Exception):
    """A plant does not declare a capability that was asked for.

    ``reason`` is the machine-readable code; ``str(exc)`` is the human-readable
    message.  Callers must branch on ``reason`` and must not treat absence as a
    degraded result.
    """

    def __init__(self, reason: str, message: str) -> None:
        if not isinstance(reason, str) or not reason:
            raise ValueError("reason must be a non-empty string")
        if not isinstance(message, str) or not message:
            raise ValueError("message must be a non-empty string")
        super().__init__(message)
        self.reason = reason
        self.message = message


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


def _finite_float(value: Any, label: str) -> float:
    """Return ``value`` as a finite float or raise ``ValueError``.

    ``bool`` is rejected: a flag is not a measurement and must not silently
    become ``0.0``/``1.0``.  Numeric strings are rejected for the same reason.
    """

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a finite number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{label} must be a finite number")
    return number


def _positive_float(value: Any, label: str) -> float:
    number = _finite_float(value, label)
    if number <= 0.0:
        raise ValueError(f"{label} must be finite and positive")
    return number


def _string_tuple(values: Any, label: str) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, (tuple, list)):
        raise TypeError(f"{label} must be a sequence of strings")
    items = tuple(values)
    if not items:
        raise ValueError(f"{label} must not be empty")
    if any(not isinstance(item, str) or not item for item in items):
        raise ValueError(f"{label} must contain only non-empty strings")
    return items


def _evidence_tokens(value: str) -> list[str]:
    """Return evidence-level words that occur as identifier tokens.

    Matching whole tokens instead of substrings keeps legitimate words such as
    ``unlimited_authority`` from being mistaken for the evidence level
    ``limited``.  Adjacent words are also checked as an underscore-joined pair
    so the multi-word level ``diagnostic_only`` is still caught when it appears
    inside an identifier such as ``test_diagnostic_only_case``.
    """

    words = _WORD_PATTERN.findall(value)
    found = {word for word in words if word in _EVIDENCE_LEVELS}
    for left, right in zip(words, words[1:]):
        pair = f"{left}_{right}"
        if pair in _EVIDENCE_LEVELS:
            found.add(pair)
    return sorted(found)


def _validate_method(value: Any, label: str = "method") -> str:
    if not isinstance(value, str) or value not in LINEARIZATION_METHODS:
        raise ValueError(
            f"{label} must be one of {LINEARIZATION_METHODS}, got {value!r}"
        )
    banned = _evidence_tokens(value)
    if banned:
        raise ValueError(
            f"{label} must not contain evidence-level tokens {banned}; "
            "a linearization method is not an evidence level"
        )
    return value


def _validate_verified_by(value: Any, label: str = "verified_by") -> str:
    if not isinstance(value, str) or not _VERIFIED_BY_PATTERN.match(value):
        raise ValueError(
            f"{label} must match 'tests/<file>.py::<test_name>' "
            "(for example "
            "'tests/test_ac_analysis.py::test_loop_gain_provenance_is_self_contained'), "
            f"got {value!r}"
        )
    if ".." in value:
        # A provenance string is a reference, but a consumer may still resolve
        # it as a path.  Reject parent segments rather than hand out something
        # that escapes the tests directory when joined.
        raise ValueError(
            f"{label} must not contain '..' path segments, got {value!r}"
        )
    banned = _evidence_tokens(value)
    if banned:
        raise ValueError(
            f"{label} must not contain evidence-level tokens {banned}; "
            "capability provenance is not an evidence level"
        )
    return value


def _validate_assumptions(values: Any, label: str = "assumptions") -> tuple[str, ...]:
    items = _string_tuple(values, label)
    for item in items:
        if not _ASSUMPTION_PATTERN.match(item):
            raise ValueError(
                f"{label} entries must be snake_case identifiers matching "
                f"^[a-z][a-z0-9_]*$, got {item!r}"
            )
        if item in _EVIDENCE_LEVELS:
            raise ValueError(
                f"{label} entries must not be evidence-level words "
                f"{sorted(_EVIDENCE_LEVELS)}; got {item!r}"
            )
    return items


def _validate_signal_names(
    values: Any, label: str, *, expected_length: int
) -> tuple[str, ...]:
    """Validate one ordered list of input or output port names."""

    items = _string_tuple(values, label)
    if len(items) != expected_length:
        raise ValueError(
            f"{label} must contain exactly {expected_length} name(s), "
            f"got {len(items)}"
        )
    seen: set[str] = set()
    for item in items:
        if not _ASSUMPTION_PATTERN.match(item):
            raise ValueError(
                f"{label} entries must be snake_case identifiers matching "
                f"^[a-z][a-z0-9_]*$, got {item!r}"
            )
        if item in seen:
            raise ValueError(f"{label} entries must be unique; duplicated {item!r}")
        seen.add(item)
    return items


def _require_consistent(actual: float, expected: float, label: str) -> None:
    if not math.isclose(
        actual, expected, rel_tol=_RELATIVE_TOLERANCE, abs_tol=0.0
    ):
        raise ValueError(
            f"{label} (relative tolerance {_RELATIVE_TOLERANCE}): "
            f"got {actual!r}, expected {expected!r}"
        )


def _freeze_float_mapping(
    values: Any,
    label: str,
    *,
    required_keys: tuple[str, ...] = (),
) -> Mapping[str, float]:
    """Validate a string-to-finite-float mapping and freeze it."""

    if not isinstance(values, Mapping):
        raise TypeError(f"{label} must be a mapping of string keys to floats")
    frozen: dict[str, float] = {}
    for key, value in values.items():
        if not isinstance(key, str) or not key:
            raise ValueError(f"{label} keys must be non-empty strings")
        frozen[key] = _finite_float(value, f"{label}[{key!r}]")
    missing = [key for key in required_keys if key not in frozen]
    if missing:
        raise ValueError(f"{label} must contain {sorted(missing)}")
    return MappingProxyType(frozen)


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OperatingPoint:
    """The linearization point requested from a capability provider.

    ``duty`` is always required and must be finite in ``[0, 1]``.
    ``input_voltage_v`` and ``load_resistance_ohm`` are optional declarations
    used only when the plant itself cannot evaluate
    ``analytical_steady_state(duty)``.  ``None`` means "not declared"; it must
    not be read as zero.
    """

    duty: float
    input_voltage_v: float | None = None
    load_resistance_ohm: float | None = None

    def __post_init__(self) -> None:
        duty = _finite_float(self.duty, "duty")
        if not 0.0 <= duty <= 1.0:
            raise ValueError("duty must be finite and in [0, 1]")
        object.__setattr__(self, "duty", duty)

        voltage = self.input_voltage_v
        if voltage is not None:
            voltage = _finite_float(voltage, "input_voltage_v")
            object.__setattr__(self, "input_voltage_v", voltage)

        resistance = self.load_resistance_ohm
        if resistance is not None:
            resistance = _positive_float(resistance, "load_resistance_ohm")
            object.__setattr__(self, "load_resistance_ohm", resistance)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe copy; ``None`` means "not declared"."""

        return {
            "duty": self.duty,
            "input_voltage_v": self.input_voltage_v,
            "load_resistance_ohm": self.load_resistance_ohm,
        }


def _solve_complex(matrix: list[list[complex]], rhs: list[complex]) -> list[complex]:
    """Solve ``matrix @ x = rhs`` by Gauss-Jordan elimination.

    The matrices here are tiny (normally 2x2), so a straightforward
    partial-pivoting elimination is both sufficient and dependency-free.
    """

    size = len(matrix)
    augmented = [list(row) + [rhs[index]] for index, row in enumerate(matrix)]
    for column in range(size):
        pivot_row = max(range(column, size), key=lambda row: abs(augmented[row][column]))
        if augmented[pivot_row][column] == 0.0:
            raise ValueError(
                "state matrix is singular at this frequency; "
                "the transfer function is undefined"
            )
        if pivot_row != column:
            augmented[column], augmented[pivot_row] = augmented[pivot_row], augmented[column]
        pivot = augmented[column][column]
        for row in range(column + 1, size):
            factor = augmented[row][column] / pivot
            if factor != 0.0:
                for offset in range(column, size + 1):
                    augmented[row][offset] -= factor * augmented[column][offset]

    solution = [0j] * size
    for row in range(size - 1, -1, -1):
        accumulated = augmented[row][size]
        for column in range(row + 1, size):
            accumulated -= augmented[row][column] * solution[column]
        solution[row] = accumulated / augmented[row][row]
    return solution


@dataclass(frozen=True)
class SmallSignalModel:
    """A linearized plant ``dx/dt = A x + B u``, ``y = C x + D u``.

    ``u`` and ``y`` are **named port vectors**.  ``input_names`` labels the
    columns of ``input_matrix`` and ``output_names`` labels the rows of
    ``output_matrix``; both default to the legacy single-port view
    (``("duty",)`` and ``("vout",)``).  ``transfer_function`` therefore keeps
    meaning "first output over first input"; use ``transfer_function_for`` or
    ``transfer_matrix`` when the model declares more than one port.

    ``feedthrough`` is the scalar ``D[0][0]`` of the legacy view.  ``D`` itself
    is ``feedthrough_matrix``; when it is omitted it is built from the scalar.
    Declaring both with different ``[0][0]`` values is rejected so the model
    never has two contradictory DC paths.

    The ``operating_point`` mapping must contain ``duty``, ``vout``, and
    ``il``.  Providers may add keys, but must not omit those three.

    ``method`` distinguishes an analytic derivation from identification.  The
    value is echoed by every AC result; it is not an evidence level.

    ``origin`` distinguishes a model installed by
    :func:`load_builtin_model_capabilities` (``"builtin"``) from one a caller
    registered (``"caller_registered"``, the default).
    """

    plant_id: str
    method: str
    state_space: LinearStateSpace
    output_matrix: tuple[tuple[float, ...], ...]
    feedthrough: float
    operating_point: Mapping[str, float]
    parameters: Mapping[str, float]
    assumptions: tuple[str, ...]
    verified_by: str
    origin: str = "caller_registered"
    input_names: tuple[str, ...] = ("duty",)
    output_names: tuple[str, ...] = ("vout",)
    feedthrough_matrix: tuple[tuple[float, ...], ...] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.plant_id, str) or not self.plant_id:
            raise ValueError("plant_id must be a non-empty string")
        _validate_method(self.method)
        if not isinstance(self.state_space, LinearStateSpace):
            raise TypeError("state_space must be a LinearStateSpace")
        input_names = _validate_signal_names(
            self.input_names,
            "input_names",
            expected_length=self.state_space.input_size,
        )

        if isinstance(self.output_matrix, (str, bytes)):
            raise TypeError("output_matrix must be a sequence of rows")
        rows = tuple(
            tuple(_finite_float(value, "output_matrix") for value in row)
            for row in self.output_matrix
        )
        if not rows:
            raise ValueError("output_matrix must contain at least one row")
        output_names = _validate_signal_names(
            self.output_names, "output_names", expected_length=len(rows)
        )
        if len(rows[0]) != self.state_space.state_size:
            raise ValueError(
                "output_matrix row length must equal state_space.state_size"
            )

        feedthrough = _finite_float(self.feedthrough, "feedthrough")
        declared_matrix = self.feedthrough_matrix
        if declared_matrix is None:
            feedthrough_matrix = tuple(
                tuple(
                    feedthrough if (row == 0 and column == 0) else 0.0
                    for column in range(len(input_names))
                )
                for row in range(len(rows))
            )
        else:
            if isinstance(declared_matrix, (str, bytes)):
                raise TypeError("feedthrough_matrix must be a sequence of rows")
            feedthrough_matrix = tuple(
                tuple(_finite_float(value, "feedthrough_matrix") for value in row)
                for row in declared_matrix
            )
            if len(feedthrough_matrix) != len(rows):
                raise ValueError(
                    "feedthrough_matrix must have one row per output name"
                )
            if any(len(row) != len(input_names) for row in feedthrough_matrix):
                raise ValueError(
                    "feedthrough_matrix must have one column per input name"
                )
            if feedthrough_matrix[0][0] != feedthrough:
                raise ValueError(
                    "feedthrough must equal feedthrough_matrix[0][0]; declare "
                    "the scalar or the matrix, not two different values"
                )
        operating_point = _freeze_float_mapping(
            self.operating_point,
            "operating_point",
            required_keys=("duty", "vout", "il"),
        )
        if not 0.0 <= operating_point["duty"] <= 1.0:
            raise ValueError("operating_point['duty'] must be finite and in [0, 1]")
        parameters = _freeze_float_mapping(self.parameters, "parameters")
        assumptions = _validate_assumptions(self.assumptions)
        verified_by = _validate_verified_by(self.verified_by)
        if self.origin not in CAPABILITY_ORIGINS:
            raise ValueError(
                f"origin must be one of {CAPABILITY_ORIGINS}, got {self.origin!r}"
            )

        object.__setattr__(self, "output_matrix", rows)
        object.__setattr__(self, "feedthrough", feedthrough)
        object.__setattr__(self, "feedthrough_matrix", feedthrough_matrix)
        object.__setattr__(self, "input_names", input_names)
        object.__setattr__(self, "output_names", output_names)
        object.__setattr__(self, "operating_point", operating_point)
        object.__setattr__(self, "parameters", parameters)
        object.__setattr__(self, "assumptions", assumptions)
        object.__setattr__(self, "verified_by", verified_by)

    def _transfer_value(
        self, frequency_hz: float, *, output_index: int, input_index: int
    ) -> complex:
        """Evaluate ``C[i] (sI - A)^-1 B[:, j] + D[i][j]`` at ``s = j 2 pi f``."""

        frequency = _finite_float(frequency_hz, "frequency_hz")
        if frequency < 0.0:
            raise ValueError("frequency_hz must be finite and non-negative")

        s = complex(0.0, 2.0 * math.pi * frequency)
        size = self.state_space.state_size
        matrix: list[list[complex]] = []
        for row in range(size):
            matrix_row: list[complex] = []
            for column in range(size):
                diagonal = s if row == column else 0j
                matrix_row.append(diagonal - self.state_space.matrix_a[row][column])
            matrix.append(matrix_row)
        rhs = [
            complex(self.state_space.input_matrix[row][input_index])
            for row in range(size)
        ]
        solution = _solve_complex(matrix, rhs)

        output_row = self.output_matrix[output_index]
        value = sum(
            output_row[column] * solution[column] for column in range(size)
        ) + self.feedthrough_matrix[output_index][input_index]
        if not math.isfinite(value.real) or not math.isfinite(value.imag):
            raise ValueError("transfer function is not finite at this frequency")
        return value

    def transfer_function(self, frequency_hz: float) -> complex:
        """Return ``C (sI - A)^-1 B + D`` at ``s = j 2 pi f``.

        This is the legacy single-port view: the first output over the first
        input.  ``frequency_hz`` must be finite and non-negative.  At
        ``f == 0`` this is the DC control-to-output gain.  A singular
        ``sI - A`` raises ``ValueError`` rather than returning infinity or a
        guessed value.
        """

        return self._transfer_value(
            frequency_hz, output_index=0, input_index=0
        )

    def transfer_function_for(
        self, frequency_hz: float, *, output: str, input: str
    ) -> complex:
        """Return the transfer function from one named input to one named output."""

        if output not in self.output_names:
            raise KeyError(
                f"unknown output {output!r}; declared outputs are "
                f"{list(self.output_names)}"
            )
        if input not in self.input_names:
            raise KeyError(
                f"unknown input {input!r}; declared inputs are "
                f"{list(self.input_names)}"
            )
        return self._transfer_value(
            frequency_hz,
            output_index=self.output_names.index(output),
            input_index=self.input_names.index(input),
        )

    def transfer_matrix(self, frequency_hz: float) -> dict[str, dict[str, complex]]:
        """Return ``result[output][input]`` for every declared port pair."""

        return {
            output: {
                input_name: self._transfer_value(
                    frequency_hz,
                    output_index=output_index,
                    input_index=input_index,
                )
                for input_index, input_name in enumerate(self.input_names)
            }
            for output_index, output in enumerate(self.output_names)
        }

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe, self-describing projection of the model."""

        return {
            "schema": CAPABILITY_SCHEMA,
            "kind": "small_signal_model",
            "plant_id": self.plant_id,
            "method": self.method,
            "state_space": {
                "matrix_a": [list(row) for row in self.state_space.matrix_a],
                "input_matrix": [list(row) for row in self.state_space.input_matrix],
            },
            "output_matrix": [list(row) for row in self.output_matrix],
            "feedthrough": self.feedthrough,
            "feedthrough_matrix": [list(row) for row in self.feedthrough_matrix],
            "input_names": list(self.input_names),
            "output_names": list(self.output_names),
            "state_equation": "dx/dt = A x + B u",
            "output_equation": "y = C x + D u",
            "input": "duty_deviation",
            "operating_point": dict(self.operating_point),
            "parameters": dict(self.parameters),
            "assumptions": list(self.assumptions),
            "verified_by": self.verified_by,
            "origin": self.origin,
        }


@dataclass(frozen=True)
class LossBreakdown:
    """Provider-declared loss terms at one operating point.

    The value object enforces the arithmetic it projects: every term is finite
    and non-negative, ``total_loss_w`` equals their sum, input power equals
    output power plus total loss, and ``efficiency`` equals the power ratio
    (all within ``1e-9`` relative tolerance).  A breakdown at or above
    ``1 - 1e-12`` efficiency must declare ``declared_lossless=True`` and at
    least one assumption; a lossless claim is never inferred.  No built-in
    provider is registered: an ideal averaged converter has no loss terms, so
    reporting an efficiency of ``1.0`` would be a fabricated claim rather than
    a measurement.
    """

    plant_id: str
    method: str
    terms: Mapping[str, float]
    total_loss_w: float
    output_power_w: float
    input_power_w: float
    efficiency: float
    assumptions: tuple[str, ...]
    verified_by: str
    declared_lossless: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.plant_id, str) or not self.plant_id:
            raise ValueError("plant_id must be a non-empty string")
        _validate_method(self.method)
        terms = _freeze_float_mapping(self.terms, "terms")
        negative_terms = sorted(key for key, value in terms.items() if value < 0.0)
        if negative_terms:
            raise ValueError(
                "terms values must be finite and non-negative; got negative "
                f"values for {negative_terms}"
            )
        total_loss_w = _finite_float(self.total_loss_w, "total_loss_w")
        output_power_w = _finite_float(self.output_power_w, "output_power_w")
        input_power_w = _finite_float(self.input_power_w, "input_power_w")
        efficiency = _finite_float(self.efficiency, "efficiency")
        assumptions = _validate_assumptions(self.assumptions)
        verified_by = _validate_verified_by(self.verified_by)
        declared_lossless = self.declared_lossless
        if not isinstance(declared_lossless, bool):
            raise ValueError("declared_lossless must be a bool")

        if input_power_w <= 0.0:
            raise ValueError(
                "input_power_w must be finite and positive so efficiency is defined"
            )
        _require_consistent(
            total_loss_w,
            sum(terms.values()),
            "total_loss_w must equal sum(terms.values())",
        )
        _require_consistent(
            input_power_w,
            output_power_w + total_loss_w,
            "input_power_w must equal output_power_w + total_loss_w",
        )
        _require_consistent(
            efficiency,
            output_power_w / input_power_w,
            "efficiency must equal output_power_w / input_power_w",
        )
        if efficiency >= 1.0 - 1e-12:
            if not declared_lossless:
                raise ValueError(
                    "efficiency >= 1 - 1e-12 requires declared_lossless=True; "
                    "a lossless breakdown must be declared, not inferred"
                )
            if not assumptions:
                raise ValueError(
                    "a declared_lossless breakdown must declare at least one "
                    "assumption"
                )

        object.__setattr__(self, "terms", terms)
        object.__setattr__(self, "total_loss_w", total_loss_w)
        object.__setattr__(self, "output_power_w", output_power_w)
        object.__setattr__(self, "input_power_w", input_power_w)
        object.__setattr__(self, "efficiency", efficiency)
        object.__setattr__(self, "assumptions", assumptions)
        object.__setattr__(self, "verified_by", verified_by)
        object.__setattr__(self, "declared_lossless", declared_lossless)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe projection of the validated breakdown."""

        return {
            "schema": CAPABILITY_SCHEMA,
            "kind": "loss_model",
            "plant_id": self.plant_id,
            "method": self.method,
            "terms": dict(self.terms),
            "total_loss_w": self.total_loss_w,
            "output_power_w": self.output_power_w,
            "input_power_w": self.input_power_w,
            "efficiency": self.efficiency,
            "declared_lossless": self.declared_lossless,
            "assumptions": list(self.assumptions),
            "verified_by": self.verified_by,
        }


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

SmallSignalFactory = Callable[[Any, OperatingPoint], SmallSignalModel]
LossFactory = Callable[[Any, OperatingPoint], LossBreakdown]

_SMALL_SIGNAL_REGISTRY: dict[str, SmallSignalFactory] = {}
_LOSS_REGISTRY: dict[str, LossFactory] = {}


def _register(
    registry: dict[str, Any],
    plant_id: str,
    factory: Any,
    *,
    replace: bool,
    capability: str,
) -> None:
    if not isinstance(plant_id, str) or not plant_id.strip():
        raise ValueError("plant_id must be a non-empty string")
    if not callable(factory):
        raise TypeError("factory must be callable")
    if plant_id in registry and not replace:
        raise ValueError(
            f"a {capability} is already registered for plant_id {plant_id!r}; "
            "pass replace=True to override it"
        )
    registry[plant_id] = factory


def register_small_signal_model(
    plant_id: str, factory: SmallSignalFactory, *, replace: bool = False
) -> None:
    """Register ``factory(plant, operating_point) -> SmallSignalModel``.

    Duplicate registration raises ``ValueError`` unless ``replace=True``.
    A factory result of the wrong type is rejected by
    :func:`small_signal_model_for` with ``TypeError``.
    """

    _register(
        _SMALL_SIGNAL_REGISTRY,
        plant_id,
        factory,
        replace=replace,
        capability=_SMALL_SIGNAL_CAPABILITY,
    )


def register_loss_model(
    plant_id: str, factory: LossFactory, *, replace: bool = False
) -> None:
    """Register ``factory(plant, operating_point) -> LossBreakdown``.

    Duplicate registration raises ``ValueError`` unless ``replace=True``.
    A factory result of the wrong type is rejected by :func:`loss_model_for`
    with ``TypeError``.
    """

    _register(
        _LOSS_REGISTRY,
        plant_id,
        factory,
        replace=replace,
        capability=_LOSS_CAPABILITY,
    )


def _lookup_keys(plant: Any) -> tuple[str, ...]:
    """Return the lookup keys in contract order: backend_id, then class name."""

    keys: list[str] = []
    backend_id = getattr(plant, "backend_id", None)
    if isinstance(backend_id, str) and backend_id:
        keys.append(backend_id)
    class_name = type(plant).__name__
    if class_name not in keys:
        keys.append(class_name)
    return tuple(keys)


def _lookup_factory(
    registry: dict[str, Any],
    plant: Any,
    *,
    capability: str,
    reason: str,
) -> tuple[Any, str]:
    keys = _lookup_keys(plant)
    for key in keys:
        factory = registry.get(key)
        if factory is not None:
            return factory, key
    raise CapabilityUnavailable(
        reason,
        f"no {capability} is registered for plant {type(plant).__name__}; "
        f"looked up keys {list(keys)}",
    )


def _require_plant_id_matches(
    declared_plant_id: str, lookup_key: str, capability: str
) -> None:
    if declared_plant_id == lookup_key:
        return
    raise CapabilityUnavailable(
        "model_plant_id_mismatch",
        f"{capability} registered for lookup key {lookup_key!r} returned "
        f"plant_id {declared_plant_id!r}; the requested key and the "
        "provider's declared plant_id must agree",
    )


def small_signal_model_for(
    plant: Any, operating_point: OperatingPoint
) -> SmallSignalModel:
    """Return the registered small-signal model at ``operating_point``.

    Lookup order (also documented at module level): ``plant.backend_id`` when
    it is a non-empty string, then ``type(plant).__name__``.  If neither key is
    registered, :class:`CapabilityUnavailable` with reason
    ``"small_signal_model_not_declared"`` is raised.  If the provider found
    under a key returns a model whose ``plant_id`` is a different key, the
    reason is ``"model_plant_id_mismatch"``.  There is no fallback,
    approximation, or ``limited`` result.
    """

    if not isinstance(operating_point, OperatingPoint):
        raise TypeError("operating_point must be an OperatingPoint")
    factory, lookup_key = _lookup_factory(
        _SMALL_SIGNAL_REGISTRY,
        plant,
        capability="small-signal model",
        reason="small_signal_model_not_declared",
    )
    model = factory(plant, operating_point)
    if not isinstance(model, SmallSignalModel):
        raise TypeError(
            "small-signal factory returned "
            f"{type(model).__name__}, expected SmallSignalModel"
        )
    _require_plant_id_matches(model.plant_id, lookup_key, "small-signal model")
    return model


def loss_model_for(plant: Any, operating_point: OperatingPoint) -> LossBreakdown:
    """Return the registered loss breakdown at ``operating_point``.

    Lookup order matches :func:`small_signal_model_for`.  If neither key is
    registered, :class:`CapabilityUnavailable` with reason
    ``"loss_model_not_declared"`` is raised; a ``plant_id`` mismatch raises
    ``"model_plant_id_mismatch"``.  An ideal lossless model must not be
    reported as an efficiency of ``1.0`` without ``declared_lossless=True``.
    """

    if not isinstance(operating_point, OperatingPoint):
        raise TypeError("operating_point must be an OperatingPoint")
    factory, lookup_key = _lookup_factory(
        _LOSS_REGISTRY,
        plant,
        capability="loss model",
        reason="loss_model_not_declared",
    )
    breakdown = factory(plant, operating_point)
    if not isinstance(breakdown, LossBreakdown):
        raise TypeError(
            "loss factory returned "
            f"{type(breakdown).__name__}, expected LossBreakdown"
        )
    _require_plant_id_matches(breakdown.plant_id, lookup_key, "loss model")
    return breakdown


def capability_provenance(
    model: SmallSignalModel,
    *,
    sweep: Mapping[str, Any] | None,
    verified_by: Sequence[str],
    controller: Mapping[str, Any] | None = None,
    assumptions: Sequence[str] = (),
) -> dict[str, Any]:
    """Return the self-contained provenance block analysis results must carry.

    ``verified_by`` is the list of tests that verify *this analysis*, while
    ``plant_verified_by`` is the model's own declaration.  Keeping them apart
    is what stops a loop-gain result from borrowing the plant model's test as
    if it had verified the controller mapping.
    """

    return {
        "plant_id": model.plant_id,
        "linearization_point": dict(model.operating_point),
        "method": model.method,
        "capability_origin": model.origin,
        "assumptions": list(assumptions),
        "plant_verified_by": model.verified_by,
        "controller": dict(controller) if controller is not None else None,
        "sweep": dict(sweep) if sweep is not None else None,
        "verified_by": list(verified_by),
    }


def registered_model_capabilities() -> dict[str, tuple[str, ...]]:
    """Return registered plant keys and the capabilities each declares.

    Capability names are a closed pair, in this order:
    ``"small_signal_model"``, ``"loss_model"``.  Keys are sorted so the result
    is deterministic and JSON-safe.
    """

    keys = sorted(set(_SMALL_SIGNAL_REGISTRY) | set(_LOSS_REGISTRY))
    capabilities: dict[str, tuple[str, ...]] = {}
    for key in keys:
        names: list[str] = []
        if key in _SMALL_SIGNAL_REGISTRY:
            names.append(_SMALL_SIGNAL_CAPABILITY)
        if key in _LOSS_REGISTRY:
            names.append(_LOSS_CAPABILITY)
        capabilities[key] = tuple(names)
    return capabilities


def clear_model_capabilities() -> None:
    """Remove every registered small-signal and loss provider."""

    _SMALL_SIGNAL_REGISTRY.clear()
    _LOSS_REGISTRY.clear()


# ---------------------------------------------------------------------------
# Built-in analytic providers
# ---------------------------------------------------------------------------

_BUCK_VERIFIED_BY = (
    "tests/test_model_capabilities.py::"
    "test_buck_small_signal_model_dc_gain_and_resonance"
)
_BOOST_VERIFIED_BY = (
    "tests/test_model_capabilities.py::test_boost_small_signal_model_dc_gain"
)

_ANALYTIC_ASSUMPTIONS = (
    "ccm_average_model",
    "ideal_components_no_loss",
    "small_signal_linearization_at_declared_operating_point",
    "constant_input_voltage_and_load_resistance",
)


def _plant_number(plant: Any, name: str) -> float | None:
    value = getattr(plant, name, None)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _mapping_number(values: Mapping[str, Any], names: tuple[str, ...]) -> float | None:
    for name in names:
        if name in values:
            value = values[name]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return None
            number = float(value)
            return number if math.isfinite(number) else None
    return None


def _require_positive(value: float | None, message: str, reason: str) -> float:
    if value is None or not math.isfinite(value) or value <= 0.0:
        raise CapabilityUnavailable(reason, message)
    return float(value)


def _analytic_converter_model(
    plant: Any, operating_point: OperatingPoint, *, topology: str
) -> SmallSignalModel:
    """Build the ideal CCM averaged control-to-output model."""

    duty = operating_point.duty
    if topology == "boost" and duty >= 1.0:
        raise CapabilityUnavailable(
            "operating_point_not_declared",
            "the boost equilibrium is singular at duty=1; declare duty < 1",
        )

    plant_name = type(plant).__name__
    inductance = _require_positive(
        _plant_number(plant, "inductance_h"),
        f"plant {plant_name} must declare a finite positive inductance_h",
        "small_signal_parameters_not_declared",
    )
    capacitance = _require_positive(
        _plant_number(plant, "capacitance_f"),
        f"plant {plant_name} must declare a finite positive capacitance_f",
        "small_signal_parameters_not_declared",
    )

    steady_state_method = getattr(plant, "analytical_steady_state", None)
    if callable(steady_state_method):
        try:
            steady_state = steady_state_method(duty)
        except Exception as exc:
            raise CapabilityUnavailable(
                "operating_point_not_declared",
                f"plant {plant_name}.analytical_steady_state({duty!r}) raised "
                f"{type(exc).__name__}: {exc}",
            ) from exc
        if not isinstance(steady_state, Mapping):
            raise CapabilityUnavailable(
                "operating_point_not_declared",
                f"plant {plant_name}.analytical_steady_state({duty!r}) "
                "did not return a mapping",
            )
        output_voltage = _mapping_number(
            steady_state, ("output_voltage_v", "vout")
        )
        inductor_current = _mapping_number(
            steady_state, ("inductor_current_a", "inductor_current", "current_a", "il")
        )
        if output_voltage is None or inductor_current is None:
            raise CapabilityUnavailable(
                "operating_point_not_declared",
                f"plant {plant_name}.analytical_steady_state({duty!r}) must "
                "declare vout and il",
            )
        input_voltage = _plant_number(plant, "input_voltage_v")
        if input_voltage is None:
            input_voltage = operating_point.input_voltage_v
        load_resistance = _plant_number(plant, "load_resistance_ohm")
        if load_resistance is None:
            load_resistance = operating_point.load_resistance_ohm
        input_voltage = _require_positive(
            input_voltage,
            f"plant {plant_name} must declare input_voltage_v or the "
            "OperatingPoint must provide it",
            "operating_point_not_declared",
        )
        load_resistance = _require_positive(
            load_resistance,
            f"plant {plant_name} must declare load_resistance_ohm or the "
            "OperatingPoint must provide it",
            "operating_point_not_declared",
        )
    else:
        if (
            operating_point.input_voltage_v is None
            or operating_point.load_resistance_ohm is None
        ):
            raise CapabilityUnavailable(
                "operating_point_not_declared",
                f"plant {plant_name} has no analytical_steady_state; "
                "OperatingPoint must declare input_voltage_v and "
                "load_resistance_ohm",
            )
        input_voltage = _require_positive(
            operating_point.input_voltage_v,
            "input_voltage_v must be finite and positive",
            "operating_point_not_declared",
        )
        load_resistance = _require_positive(
            operating_point.load_resistance_ohm,
            "load_resistance_ohm must be finite and positive",
            "operating_point_not_declared",
        )
        if topology == "buck":
            output_voltage = duty * input_voltage
            inductor_current = output_voltage / load_resistance
        else:
            conduction = 1.0 - duty
            output_voltage = input_voltage / conduction
            inductor_current = output_voltage / (conduction * load_resistance)

    if not math.isfinite(output_voltage) or not math.isfinite(inductor_current):
        raise CapabilityUnavailable(
            "operating_point_not_declared",
            f"plant {plant_name} produced a non-finite equilibrium at duty={duty!r}",
        )

    if topology == "buck":
        matrix_a = (
            (0.0, -1.0 / inductance),
            (1.0 / capacitance, -1.0 / (load_resistance * capacitance)),
        )
        # Inputs: duty deviation, input-voltage deviation, injected load current.
        input_matrix = (
            (input_voltage / inductance, duty / inductance, 0.0),
            (0.0, 0.0, 1.0 / capacitance),
        )
        # Outputs: output voltage; input current (d * iL -> I_L * dd + D * diL).
        # The input-current row multiplies the inductor-current state, not the
        # output voltage: i_in = D * iL.
        output_matrix = ((0.0, 1.0), (duty, 0.0))
        feedthrough_matrix = (
            (0.0, 0.0, 0.0),
            (inductor_current, 0.0, 0.0),
        )
        current_assumption = "input_current_is_d_times_inductor_current"
    else:
        conduction = 1.0 - duty
        matrix_a = (
            (0.0, -conduction / inductance),
            (conduction / capacitance, -1.0 / (load_resistance * capacitance)),
        )
        input_matrix = (
            (output_voltage / inductance, 1.0 / inductance, 0.0),
            (-inductor_current / capacitance, 0.0, 1.0 / capacitance),
        )
        output_matrix = ((0.0, 1.0), (1.0, 0.0))
        feedthrough_matrix = (
            (0.0, 0.0, 0.0),
            (0.0, 0.0, 0.0),
        )
        current_assumption = "input_current_is_inductor_current"

    assumptions = _ANALYTIC_ASSUMPTIONS + (
        "load_current_is_injected_at_the_output_node",
        current_assumption,
    )

    return SmallSignalModel(
        plant_id=topology,
        method="analytic",
        state_space=LinearStateSpace(matrix_a, input_matrix),
        output_matrix=output_matrix,
        feedthrough=feedthrough_matrix[0][0],
        feedthrough_matrix=feedthrough_matrix,
        input_names=("duty", "vin", "load_current"),
        output_names=("vout", "input_current"),
        operating_point={
            "duty": duty,
            "vout": output_voltage,
            "il": inductor_current,
            "vin": input_voltage,
            "load_resistance_ohm": load_resistance,
        },
        parameters={
            "input_voltage_v": input_voltage,
            "inductance_h": inductance,
            "capacitance_f": capacitance,
            "load_resistance_ohm": load_resistance,
        },
        assumptions=assumptions,
        verified_by=_BUCK_VERIFIED_BY if topology == "buck" else _BOOST_VERIFIED_BY,
        origin="builtin",
    )


def _buck_small_signal_model(
    plant: Any, operating_point: OperatingPoint
) -> SmallSignalModel:
    return _analytic_converter_model(plant, operating_point, topology="buck")


def _boost_small_signal_model(
    plant: Any, operating_point: OperatingPoint
) -> SmallSignalModel:
    return _analytic_converter_model(plant, operating_point, topology="boost")


_BUILTIN_SMALL_SIGNAL_MODELS: tuple[tuple[str, SmallSignalFactory], ...] = (
    ("buck", _buck_small_signal_model),
    ("boost", _boost_small_signal_model),
)


def load_builtin_model_capabilities() -> None:
    """Install the built-in analytic buck and boost small-signal models.

    Idempotent: an existing registration under the same key is left untouched,
    so calling this twice never raises and never clobbers a caller-provided
    ``replace=True`` override.  No loss provider is installed: the built-in
    models are ideal and lossless, so ``efficiency = 1.0`` would be a
    fabricated claim rather than a declared loss breakdown.
    """

    for plant_id, factory in _BUILTIN_SMALL_SIGNAL_MODELS:
        if plant_id not in _SMALL_SIGNAL_REGISTRY:
            _SMALL_SIGNAL_REGISTRY[plant_id] = factory


__all__ = [
    "CAPABILITY_ORIGINS",
    "CAPABILITY_SCHEMA",
    "LINEARIZATION_METHODS",
    "CapabilityUnavailable",
    "LossBreakdown",
    "OperatingPoint",
    "SmallSignalModel",
    "clear_model_capabilities",
    "load_builtin_model_capabilities",
    "loss_model_for",
    "register_loss_model",
    "register_small_signal_model",
    "registered_model_capabilities",
    "small_signal_model_for",
]

"""User-facing capability analysis: AC, impedance, and loss at one point.

The study layer answers "run this experiment many times"; this module answers
"ask this declared model a frequency- or loss-domain question once".  It is a
separate document type (``pe-sim.capability-analysis.v1``) because reusing a
study's ``base_experiment`` would force every analysis to pretend to be a run.

The document names a registered plant (or the built-in ``lossy-buck`` adapter),
one explicit operating point, and the analysis-specific inputs.  Results are
returned as a single JSON report; every nested result is the same object the
library functions publish, including its own ``provenance`` block.  No result
carries an evidence level: a capability analysis is not a run.
"""

from __future__ import annotations

import json
import math
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .ac_analysis import frequency_response, loop_gain_response, stability_margins
from .artifacts import canonical_json, sha256_bytes
from .builtin_controllers import load_builtin_controllers
from .builtin_plants import PlantBuildContext, load_builtin_plants
from .controller_registry import ControllerBuildContext, resolve_controller
from .frequency_domain import validate_sweep
from .impedance_analysis import IMPEDANCE_KINDS, impedance_response, middlebrook_ratio
from .lossy_converter import LossyBuckPlant, register_loss_provider
from .model_capabilities import (
    OperatingPoint,
    load_builtin_model_capabilities,
    loss_model_for,
    small_signal_model_for,
)
from .plant_registry import resolve


CAPABILITY_ANALYSIS_SCHEMA = "pe-sim.capability-analysis.v1"
CAPABILITY_REPORT_SCHEMA = "pe-sim.capability-report.v1"
CAPABILITY_KINDS = ("ac", "impedance", "loss")
CAPABILITY_ANALYSIS_REASONS = frozenset(
    {
        "invalid_document",
        "unknown_plant",
        "invalid_plan",
        "output_error",
    }
)

_TOP_LEVEL_KEYS = frozenset(
    {
        "schema",
        "analysis_id",
        "kind",
        "plant",
        "operating_point",
        "sweep",
        "controller",
        "impedance",
        "output",
    }
)
_PLANT_KEYS = frozenset({"id", "config"})
_OPERATING_POINT_KEYS = frozenset(
    {"duty", "input_voltage_v", "load_resistance_ohm"}
)
_SWEEP_KEYS = frozenset({"start_hz", "stop_hz", "points"})
_CONTROLLER_KEYS = frozenset({"id", "config", "control_period_s"})
_IMPEDANCE_KEYS = frozenset({"kind", "external"})
_OUTPUT_KEYS = frozenset({"directory"})
_ANALYSIS_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")
_REPORT_FILENAME = "capability-report.json"


class CapabilityAnalysisError(ValueError):
    """A capability analysis document or output location was refused.

    ``reason`` is the machine-readable code; ``str(exc)`` is the message.  The
    CLI maps ``unknown_plant`` to the existing rejection code of the same name
    and every other reason to ``invalid_request``; this module never invents a
    new rejection vocabulary.
    """

    def __init__(self, reason: str, message: str) -> None:
        if reason not in CAPABILITY_ANALYSIS_REASONS:
            raise ValueError(
                f"reason must be one of {sorted(CAPABILITY_ANALYSIS_REASONS)}, "
                f"got {reason!r}"
            )
        super().__init__(message)
        self.reason = reason
        self.message = message


@dataclass(frozen=True)
class SweepSpec:
    start_hz: float
    stop_hz: float
    points: int


@dataclass(frozen=True)
class ControllerSpec:
    controller_id: str
    config: Mapping[str, Any]
    control_period_s: float


@dataclass(frozen=True)
class ImpedanceSpec:
    kind: str
    external: Mapping[str, Any] | None


@dataclass(frozen=True)
class CapabilityAnalysisPlan:
    document: Mapping[str, Any]
    analysis_id: str
    kind: str
    plant_id: str
    plant_config: Mapping[str, Any]
    operating_point: OperatingPoint
    sweep: SweepSpec | None = None
    controller: ControllerSpec | None = None
    impedance: ImpedanceSpec | None = None
    output_dir: str | None = None


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise CapabilityAnalysisError(
            "invalid_document", f"{label} must be an object"
        )
    return value


def _reject_unknown(
    value: Mapping[str, Any], allowed: frozenset[str], label: str
) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise CapabilityAnalysisError(
            "invalid_document",
            f"{label} contains unknown key(s) {unknown}; "
            f"allowed keys: {sorted(allowed)}",
        )


def _nonempty_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise CapabilityAnalysisError(
            "invalid_document", f"{label} must be a non-empty string"
        )
    return value


def _finite(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CapabilityAnalysisError(
            "invalid_document", f"{label} must be a finite number"
        )
    number = float(value)
    if not math.isfinite(number):
        raise CapabilityAnalysisError(
            "invalid_document", f"{label} must be a finite number"
        )
    return number


def _parse_operating_point(value: Any) -> OperatingPoint:
    block = _mapping(value, "operating_point")
    _reject_unknown(block, _OPERATING_POINT_KEYS, "operating_point")
    missing = sorted(_OPERATING_POINT_KEYS - set(block))
    if missing:
        raise CapabilityAnalysisError(
            "invalid_document",
            f"operating_point is missing required key(s) {missing}",
        )
    duty = _finite(block["duty"], "operating_point.duty")
    input_voltage = _finite(
        block["input_voltage_v"], "operating_point.input_voltage_v"
    )
    load = _finite(
        block["load_resistance_ohm"], "operating_point.load_resistance_ohm"
    )
    try:
        return OperatingPoint(
            duty=duty,
            input_voltage_v=input_voltage,
            load_resistance_ohm=load,
        )
    except (TypeError, ValueError) as exc:
        raise CapabilityAnalysisError(
            "invalid_document", f"operating_point is invalid: {exc}"
        ) from exc


def _parse_sweep(value: Any) -> SweepSpec:
    block = _mapping(value, "sweep")
    _reject_unknown(block, _SWEEP_KEYS, "sweep")
    missing = sorted(_SWEEP_KEYS - set(block))
    if missing:
        raise CapabilityAnalysisError(
            "invalid_document", f"sweep is missing required key(s) {missing}"
        )
    try:
        start, stop, points = validate_sweep(
            block["start_hz"], block["stop_hz"], block["points"]
        )
    except ValueError as exc:
        raise CapabilityAnalysisError(
            "invalid_plan", f"sweep is invalid: {exc}"
        ) from exc
    return SweepSpec(start_hz=start, stop_hz=stop, points=points)


def _parse_controller(value: Any) -> ControllerSpec:
    block = _mapping(value, "controller")
    _reject_unknown(block, _CONTROLLER_KEYS, "controller")
    controller_id = _nonempty_string(block.get("id"), "controller.id")
    config = block.get("config", {})
    if not isinstance(config, Mapping):
        raise CapabilityAnalysisError(
            "invalid_document", "controller.config must be an object"
        )
    control_period = _finite(
        block.get("control_period_s"), "controller.control_period_s"
    )
    if control_period <= 0.0:
        raise CapabilityAnalysisError(
            "invalid_plan", "controller.control_period_s must be positive"
        )
    return ControllerSpec(
        controller_id=controller_id,
        config=dict(config),
        control_period_s=control_period,
    )


def _parse_impedance(value: Any) -> ImpedanceSpec:
    block = _mapping(value, "impedance")
    _reject_unknown(block, _IMPEDANCE_KEYS, "impedance")
    kind = _nonempty_string(block.get("kind"), "impedance.kind")
    if kind not in IMPEDANCE_KINDS:
        raise CapabilityAnalysisError(
            "invalid_plan",
            f"impedance.kind must be one of {IMPEDANCE_KINDS}, got {kind!r}",
        )
    external = block.get("external")
    if external is not None and not isinstance(external, Mapping):
        raise CapabilityAnalysisError(
            "invalid_document", "impedance.external must be an object"
        )
    return ImpedanceSpec(kind=kind, external=dict(external) if external else None)


def parse_capability_document(document: Any) -> CapabilityAnalysisPlan:
    """Validate one ``pe-sim.capability-analysis.v1`` document."""

    value = _mapping(document, "document")
    _reject_unknown(value, _TOP_LEVEL_KEYS, "document")
    if value.get("schema") != CAPABILITY_ANALYSIS_SCHEMA:
        raise CapabilityAnalysisError(
            "invalid_document",
            f"schema must equal {CAPABILITY_ANALYSIS_SCHEMA!r}, "
            f"got {value.get('schema')!r}",
        )

    analysis_id = _nonempty_string(value.get("analysis_id"), "analysis_id")
    if not _ANALYSIS_ID_PATTERN.match(analysis_id):
        raise CapabilityAnalysisError(
            "invalid_plan",
            "analysis_id must contain only letters, digits, '.', '_' and '-'",
        )

    kind = _nonempty_string(value.get("kind"), "kind")
    if kind not in CAPABILITY_KINDS:
        raise CapabilityAnalysisError(
            "invalid_plan",
            f"kind must be one of {CAPABILITY_KINDS}, got {kind!r}",
        )

    plant = _mapping(value.get("plant"), "plant")
    _reject_unknown(plant, _PLANT_KEYS, "plant")
    plant_id = _nonempty_string(plant.get("id"), "plant.id")
    plant_config = plant.get("config", {})
    if not isinstance(plant_config, Mapping):
        raise CapabilityAnalysisError(
            "invalid_document", "plant.config must be an object"
        )

    operating_point = _parse_operating_point(value.get("operating_point"))

    sweep: SweepSpec | None = None
    controller: ControllerSpec | None = None
    impedance: ImpedanceSpec | None = None

    if kind in {"ac", "impedance"}:
        if "sweep" not in value:
            raise CapabilityAnalysisError(
                "invalid_document", f"kind {kind!r} requires a sweep block"
            )
        sweep = _parse_sweep(value["sweep"])
    elif "sweep" in value:
        raise CapabilityAnalysisError(
            "invalid_document", "kind 'loss' must not declare a sweep block"
        )

    if kind == "ac":
        if "controller" in value:
            controller = _parse_controller(value["controller"])
    elif "controller" in value:
        raise CapabilityAnalysisError(
            "invalid_document",
            f"kind {kind!r} must not declare a controller block",
        )

    if kind == "impedance":
        if "impedance" not in value:
            raise CapabilityAnalysisError(
                "invalid_document", "kind 'impedance' requires an impedance block"
            )
        impedance = _parse_impedance(value["impedance"])
    elif "impedance" in value:
        raise CapabilityAnalysisError(
            "invalid_document",
            f"kind {kind!r} must not declare an impedance block",
        )

    output_dir: str | None = None
    if "output" in value:
        output = _mapping(value["output"], "output")
        _reject_unknown(output, _OUTPUT_KEYS, "output")
        output_dir = _nonempty_string(
            output.get("directory"), "output.directory"
        )

    return CapabilityAnalysisPlan(
        document=dict(value),
        analysis_id=analysis_id,
        kind=kind,
        plant_id=plant_id,
        plant_config=dict(plant_config),
        operating_point=operating_point,
        sweep=sweep,
        controller=controller,
        impedance=impedance,
        output_dir=output_dir,
    )


def _build_plant(plan: CapabilityAnalysisPlan) -> Any:
    """Build the declared plant without creating a run or a run directory."""

    if plan.plant_id == "lossy-buck":
        try:
            return LossyBuckPlant(**dict(plan.plant_config))
        except (TypeError, ValueError) as exc:
            raise CapabilityAnalysisError(
                "invalid_plan",
                f"lossy-buck plant_config is invalid: {exc}",
            ) from exc

    load_builtin_plants()
    try:
        registration = resolve(plan.plant_id)
    except (KeyError, ValueError) as exc:
        raise CapabilityAnalysisError(
            "unknown_plant",
            f"plant_id {plan.plant_id!r} is not registered: {exc}",
        ) from exc
    try:
        return registration.factory(
            dict(plan.plant_config), PlantBuildContext()
        )
    except (TypeError, ValueError) as exc:
        raise CapabilityAnalysisError(
            "invalid_plan",
            f"plant_config for {plan.plant_id!r} is invalid: {exc}",
        ) from exc


def _build_controller(spec: ControllerSpec) -> Any:
    load_builtin_controllers()
    try:
        return resolve_controller(
            spec.controller_id,
            dict(spec.config),
            ControllerBuildContext(control_period_s=spec.control_period_s),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise CapabilityAnalysisError(
            "invalid_plan",
            f"controller {spec.controller_id!r} cannot be built: {exc}",
        ) from exc


def run_capability_analysis(plan: CapabilityAnalysisPlan) -> dict[str, Any]:
    """Execute one capability analysis and return the JSON report object."""

    if not isinstance(plan, CapabilityAnalysisPlan):
        raise TypeError("plan must be a CapabilityAnalysisPlan")

    # The capability registries are opt-in in the library; the CLI entry point
    # is the composition root for this document and installs them once.
    load_builtin_model_capabilities()
    plant = _build_plant(plan)
    results: dict[str, Any] = {}

    if plan.kind == "ac":
        model = small_signal_model_for(plant, plan.operating_point)
        if plan.sweep is None:
            raise CapabilityAnalysisError(
                "invalid_plan", "kind 'ac' requires a sweep block"
            )
        results["frequency_response"] = frequency_response(
            model,
            start_hz=plan.sweep.start_hz,
            stop_hz=plan.sweep.stop_hz,
            points=plan.sweep.points,
        )
        if plan.controller is not None:
            controller = _build_controller(plan.controller)
            loop = loop_gain_response(
                model,
                controller,
                start_hz=plan.sweep.start_hz,
                stop_hz=plan.sweep.stop_hz,
                points=plan.sweep.points,
            )
            results["loop_gain"] = loop
            results["stability_margins"] = stability_margins(loop)

    elif plan.kind == "impedance":
        model = small_signal_model_for(plant, plan.operating_point)
        if plan.sweep is None:
            raise CapabilityAnalysisError(
                "invalid_plan", "kind 'impedance' requires a sweep block"
            )
        if plan.impedance is None:
            raise CapabilityAnalysisError(
                "invalid_plan",
                "kind 'impedance' requires an impedance block",
            )
        impedance = impedance_response(
            model,
            kind=plan.impedance.kind,
            start_hz=plan.sweep.start_hz,
            stop_hz=plan.sweep.stop_hz,
            points=plan.sweep.points,
        )
        results["impedance"] = impedance
        if plan.impedance.external is not None:
            results["middlebrook"] = middlebrook_ratio(
                impedance, plan.impedance.external, kind=plan.impedance.kind
            )

    else:  # loss
        if plan.plant_id == "lossy-buck":
            register_loss_provider()
        results["loss_model"] = loss_model_for(
            plant, plan.operating_point
        ).to_dict()

    return {
        "schema": CAPABILITY_REPORT_SCHEMA,
        "analysis_id": plan.analysis_id,
        "kind": plan.kind,
        "status": "available",
        "plant": {"id": plan.plant_id, "class": type(plant).__name__},
        "operating_point": {
            "duty": plan.operating_point.duty,
            "input_voltage_v": plan.operating_point.input_voltage_v,
            "load_resistance_ohm": plan.operating_point.load_resistance_ohm,
        },
        "input_sha256": sha256_bytes(canonical_json(plan.document)),
        "results": results,
    }


def write_capability_report(
    report: Mapping[str, Any], directory: str | Path
) -> Path:
    """Atomically write ``capability-report.json`` under ``directory``."""

    if not isinstance(report, Mapping):
        raise CapabilityAnalysisError(
            "output_error", "report must be an object"
        )
    try:
        text = (
            json.dumps(report, sort_keys=True, indent=2, allow_nan=False)
            + "\n"
        )
    except (TypeError, ValueError) as exc:
        raise CapabilityAnalysisError(
            "output_error", f"report is not JSON-safe: {exc}"
        ) from exc
    target_dir = Path(directory)
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise CapabilityAnalysisError(
            "output_error", f"cannot create {target_dir}: {exc}"
        ) from exc
    target = target_dir / _REPORT_FILENAME
    temporary = target_dir / f"{_REPORT_FILENAME}.{os.getpid()}.tmp"
    try:
        try:
            with open(temporary, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        except OSError as exc:
            # mkdir succeeded but the write or the atomic rename failed.  The
            # entry point promises a branchable ``output_error`` reason for
            # every output failure, so do not let this escape as a bare OSError.
            raise CapabilityAnalysisError(
                "output_error", f"cannot write {target}: {exc}"
            ) from exc
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            # Best-effort cleanup; never mask the primary write failure.
            pass
    return target


__all__ = [
    "CAPABILITY_ANALYSIS_REASONS",
    "CAPABILITY_ANALYSIS_SCHEMA",
    "CAPABILITY_KINDS",
    "CAPABILITY_REPORT_SCHEMA",
    "CapabilityAnalysisError",
    "CapabilityAnalysisPlan",
    "ControllerSpec",
    "ImpedanceSpec",
    "SweepSpec",
    "parse_capability_document",
    "run_capability_analysis",
    "write_capability_report",
]

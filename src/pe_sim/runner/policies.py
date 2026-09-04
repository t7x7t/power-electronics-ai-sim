"""Low-level policy objects for the simple and advanced runner APIs."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping


@dataclass(frozen=True)
class TimingPolicy:
    """Timing behavior selected by an advanced caller.

    The initial policy surface is deliberately narrow.  Timing values remain
    authoritative in ``ExperimentSpec.timebase``; this object selects the
    supported fixed-window engine and rejects unsupported combinations early.
    """

    engine: str = "fixed_window"
    allow_sample_offset: bool = True
    allow_target_time: bool = True

    def validate(self, spec: Any) -> None:
        if self.engine != "fixed_window":
            raise ValueError("unsupported timing engine; only 'fixed_window' is available")
        if not self.allow_sample_offset and float(spec.timebase.sample_offset_s) != 0.0:
            raise ValueError("timing policy disallows the configured sample offset")
        # Individual ActionRequest target times are validated by the runner
        # once a Controller has produced them.


@dataclass(frozen=True)
class RecoveryPolicy:
    """Checkpoint and resume behavior for advanced callers."""

    checkpoint_interval_steps: int | None = None
    resume_from: str | Path | None = None
    interrupt_after_steps: int | None = None

    def validate(self) -> None:
        for name, value in (
            ("checkpoint_interval_steps", self.checkpoint_interval_steps),
            ("interrupt_after_steps", self.interrupt_after_steps),
        ):
            if value is not None and (isinstance(value, bool) or int(value) <= 0):
                raise ValueError(f"{name} must be a positive integer")


@dataclass(frozen=True)
class AuditPolicy:
    """Audit behavior.  Formal runs cannot disable audit evidence."""

    enabled: bool = True

    def validate(self, mode: str) -> None:
        if mode == "formal_comparison" and not self.enabled:
            raise ValueError("formal_comparison requires audit evidence")


@dataclass(frozen=True)
class RunOptions:
    """Optional advanced runner policies.

    The default is intentionally empty: ``run_experiment(spec, plant,
    controller)`` uses the standard deterministic, audited fixed-window run.
    """

    mode: str = "exploratory"
    timing: TimingPolicy = TimingPolicy()
    recovery: RecoveryPolicy = RecoveryPolicy()
    audit: AuditPolicy = AuditPolicy()
    measurement_key: str | None = None
    safety_checker: Callable[[Mapping[str, float]], Any] | None = None
    qualification_checker: Callable[[list[Mapping[str, Any]]], tuple[bool, list[str]]] | None = None

    def validate(self, spec: Any) -> None:
        if self.mode not in {"exploratory", "formal_comparison"}:
            raise ValueError("mode must be 'exploratory' or 'formal_comparison'")
        if self.measurement_key is not None and not str(self.measurement_key).strip():
            raise ValueError("measurement_key must be non-empty when provided")
        self.timing.validate(spec)
        self.recovery.validate()
        self.audit.validate(self.mode)

"""Explicit, fail-closed run lifecycle state machine.

The matrix is intentionally public.  Callers that publish or recover runs can
inspect the same transition rules as the runner instead of inferring status
semantics from strings in a manifest.
"""

from __future__ import annotations

from typing import Any, Mapping


class LifecycleTransitionError(ValueError):
    """Raised when a run attempts an illegal state transition."""


class LifecycleStateMachine:
    _ALLOWED: Mapping[str, frozenset[str]] = {
        "CREATED": frozenset({"RUNNING", "RUN_FAILED", "INCOMPLETE"}),
        "RUNNING": frozenset({"RUN_OK", "RUN_FAILED", "INCOMPLETE"}),
        "RUN_OK": frozenset({"QUALIFIED", "DISQUALIFIED"}),
        "QUALIFIED": frozenset({"COMPARABLE", "LEARNING_ELIGIBLE"}),
        "LEARNING_ELIGIBLE": frozenset({"LEARNING_UPDATED", "LEARNING_REJECTED"}),
        "RUN_FAILED": frozenset(),
        "INCOMPLETE": frozenset(),
        "DISQUALIFIED": frozenset(),
        "COMPARABLE": frozenset(),
        "LEARNING_UPDATED": frozenset(),
        "LEARNING_REJECTED": frozenset(),
    }
    TERMINAL_STATES = frozenset({
        "RUN_FAILED", "INCOMPLETE", "DISQUALIFIED", "COMPARABLE",
        "LEARNING_UPDATED", "LEARNING_REJECTED",
    })
    RULE_VERSION = "runner-state-v2"

    def __init__(self) -> None:
        self.status = "CREATED"
        self.transitions: list[dict[str, Any]] = []

    @classmethod
    def allowed_transitions(cls) -> dict[str, tuple[str, ...]]:
        """Return a stable, JSON-friendly copy of the transition matrix."""

        return {state: tuple(sorted(targets)) for state, targets in cls._ALLOWED.items()}

    @property
    def is_terminal(self) -> bool:
        return self.status in self.TERMINAL_STATES

    def can_transition(self, target: str) -> bool:
        return str(target) in self._ALLOWED.get(self.status, frozenset())

    def transition(
        self,
        target: str,
        reason: str,
        *,
        step_index: int | None = None,
        time_s: float | None = None,
    ) -> None:
        target = str(target)
        if not self.can_transition(target):
            raise LifecycleTransitionError(f"invalid run state transition {self.status} -> {target}")
        self.transitions.append(
            {
                "sequence": len(self.transitions),
                "from": self.status,
                "to": target,
                "reason": reason,
                "step_index": step_index,
                "time_s": time_s,
                "rule_version": self.RULE_VERSION,
            }
        )
        self.status = target

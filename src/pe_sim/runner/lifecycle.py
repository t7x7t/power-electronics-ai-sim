"""Explicit run lifecycle state machine."""

from __future__ import annotations

from typing import Any


class LifecycleStateMachine:
    _ALLOWED = {
        "CREATED": {"RUNNING", "RUN_FAILED", "INCOMPLETE"},
        "RUNNING": {"RUN_OK", "RUN_FAILED", "INCOMPLETE"},
    }

    def __init__(self) -> None:
        self.status = "CREATED"
        self.transitions: list[dict[str, Any]] = []

    def transition(
        self,
        target: str,
        reason: str,
        *,
        step_index: int | None = None,
        time_s: float | None = None,
    ) -> None:
        if target not in self._ALLOWED.get(self.status, set()):
            raise ValueError(f"invalid run state transition {self.status} -> {target}")
        self.transitions.append(
            {
                "from": self.status,
                "to": target,
                "reason": reason,
                "step_index": step_index,
                "time_s": time_s,
                "rule_version": "runner-state-v1",
            }
        )
        self.status = target

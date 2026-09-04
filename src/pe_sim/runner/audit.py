"""Call auditing service used by the runner coordinator."""

from __future__ import annotations

from typing import Any, Callable


class AuditRecorder:
    """Record ordered component calls while preserving exception behavior."""

    def __init__(self, enabled: bool = True) -> None:
        self.calls: list[dict[str, Any]] = []
        self._call_id = 0
        self.enabled = bool(enabled)

    def call(
        self,
        component: str,
        method: str,
        fn: Callable[[], Any],
        *,
        step_index: int | None = None,
        time_s: float | None = None,
    ) -> Any:
        if not self.enabled:
            return fn()
        self._call_id += 1
        record: dict[str, Any] = {
            "call_id": self._call_id,
            "component": component,
            "method": method,
            "step_index": step_index,
            "time_s": time_s,
        }
        try:
            result = fn()
        except BaseException as exc:
            record.update({"ok": False, "exception_type": type(exc).__name__, "error": str(exc)})
            self.calls.append(record)
            raise
        record["ok"] = True
        self.calls.append(record)
        return result

    def to_dict(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for row in self.calls:
            key = f"{row['component']}.{row['method']}"
            counts[key] = counts.get(key, 0) + 1
        return {
            "calls": self.calls,
            "call_counts": counts,
            "call_order": [f"{row['component']}.{row['method']}" for row in self.calls],
        }

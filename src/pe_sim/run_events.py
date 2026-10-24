"""Versioned run-interaction events: progress is observability, never evidence.

A client that starts a long run needs to know three things without reading the
run directory: that the run started, how far it has advanced, and how it
ended.  This module defines that stream as data.

Two properties are load-bearing and deliberately different from the evidence
chain:

1. **Nothing here is evidence.**  Events are not written into the run package,
   are not hashed, and cannot change a status, a qualification, or an evidence
   level.  A client that needs proof reads the published package, not the
   stream.
2. **A broken sink must not break the run.**  ``RunEventEmitter`` counts a
   failed delivery and continues.  Progress is a courtesy extended to the
   caller; evidence is the run itself.

The stream is rate-limited by ``ProgressPolicy`` so that observing a run
cannot cost more than the run.  Rate limiting is intentionally coarse: exact
event timing is machine-dependent and therefore never part of a contract.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Callable, Mapping

from .jsonutil import json_safe


#: Schema tag carried by every progress event.
RUN_EVENT_SCHEMA = "pe-sim.run-event.v1"

#: Schema tag carried by the terminal result printed by the CLI.
RUN_RESULT_SCHEMA = "pe-sim.run-result.v1"

EVENT_RUN_STARTED = "run.started"
EVENT_RUN_PROGRESS = "run.progress"
EVENT_RUN_FINISHED = "run.finished"


@dataclass(frozen=True)
class RunEvent:
    """One ordered observation about a run in progress."""

    sequence: int
    event: str
    run_id: str
    data: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": RUN_EVENT_SCHEMA,
            "sequence": self.sequence,
            "event": self.event,
            "run_id": self.run_id,
            "data": json_safe(dict(self.data)),
        }


class RunEventEmitter:
    """Best-effort, strictly ordered delivery to an optional sink.

    ``sequence`` increases for every delivery *attempt*, so a client can see
    that it missed an event after a sink failure rather than silently seeing a
    shorter run.  ``dropped`` exposes the same fact to the run itself.
    """

    def __init__(self, sink: Callable[[Mapping[str, Any]], None] | None = None):
        if sink is not None and not callable(sink):
            raise TypeError("event sink must be callable")
        self._sink = sink
        self._sequence = 0
        self._delivered = 0
        self._dropped = 0

    @property
    def enabled(self) -> bool:
        return self._sink is not None

    @property
    def sequence(self) -> int:
        """Number of delivery attempts so far."""

        return self._sequence

    @property
    def delivered(self) -> int:
        return self._delivered

    @property
    def dropped(self) -> int:
        return self._dropped

    def emit(self, event: str, run_id: str, data: Mapping[str, Any]) -> bool:
        """Deliver one event.  Returns True when the sink accepted it."""

        if self._sink is None:
            return False
        if not isinstance(event, str) or not event:
            raise ValueError("event name must be a non-empty string")
        payload = RunEvent(self._sequence, event, str(run_id), data).to_dict()
        self._sequence += 1
        try:
            self._sink(payload)
        except Exception:
            self._dropped += 1
            return False
        self._delivered += 1
        return True


@dataclass(frozen=True)
class ProgressPolicy:
    """Where progress events go and how often the runner may emit them.

    ``interval_steps`` emits when the completed sample count is a multiple of
    the given step count.  ``interval_s`` emits when at least this much wall
    time has passed since the previous emission.  Both may be set; neither
    makes an event's timing part of the contract.
    """

    sink: Callable[[Mapping[str, Any]], None]
    interval_steps: int | None = None
    interval_s: float | None = 1.0

    def __post_init__(self) -> None:
        if not callable(self.sink):
            raise TypeError("progress sink must be callable")
        if self.interval_steps is not None:
            if isinstance(self.interval_steps, bool) or not isinstance(self.interval_steps, int):
                raise TypeError("interval_steps must be an integer or None")
            if self.interval_steps <= 0:
                raise ValueError("interval_steps must be positive")
        if self.interval_s is not None:
            if isinstance(self.interval_s, bool) or not isinstance(self.interval_s, (int, float)):
                raise TypeError("interval_s must be a number or None")
            if not math.isfinite(float(self.interval_s)) or float(self.interval_s) <= 0.0:
                raise ValueError("interval_s must be finite and positive")


def progress_due(
    *,
    sample_count: int,
    interval_steps: int | None = None,
    interval_s: float | None = None,
    wall_time_s: float | None = None,
    last_wall_time_s: float | None = None,
) -> bool:
    """Pure rate-limit decision used by the runner.

    ``wall_time_s`` and ``last_wall_time_s`` are monotonic-clock readings; the
    function never reads the clock itself so the decision is testable.
    """

    if sample_count <= 0:
        return False
    if interval_steps is not None and sample_count % interval_steps == 0:
        return True
    if interval_s is not None:
        if wall_time_s is None:
            return False
        if last_wall_time_s is None:
            return True
        return wall_time_s - last_wall_time_s >= interval_s
    return False


__all__ = [
    "EVENT_RUN_FINISHED",
    "EVENT_RUN_PROGRESS",
    "EVENT_RUN_STARTED",
    "RUN_EVENT_SCHEMA",
    "RUN_RESULT_SCHEMA",
    "ProgressPolicy",
    "RunEvent",
    "RunEventEmitter",
    "progress_due",
]

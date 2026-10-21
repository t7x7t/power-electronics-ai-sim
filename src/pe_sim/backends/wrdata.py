"""Fail-closed parser for ngspice ``wrdata`` ASCII output.

The reviewed backend deliberately requests a flat ASCII table instead of a
binary rawfile.  Parsing is therefore small, dependency-free, and testable
without an ngspice installation.  Malformed, truncated, or non-finite output is
rejected rather than silently converted into simulation evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Mapping, Sequence


class WrdataParseError(ValueError):
    """Raised when wrdata output cannot be interpreted safely."""


def _is_number(token: str) -> bool:
    try:
        float(token)
    except (TypeError, ValueError):
        return False
    return True


def _normalise_name(name: str) -> str:
    return name.strip().lower()


@dataclass(frozen=True)
class TraceTable:
    """Monotonic time series published by one ngspice window solve."""

    time_s: tuple[float, ...]
    signals: Mapping[str, tuple[float, ...]]

    def __post_init__(self) -> None:
        times = tuple(float(value) for value in self.time_s)
        if len(times) < 2:
            raise WrdataParseError("trace table requires at least two time points")
        if any(not math.isfinite(value) for value in times):
            raise WrdataParseError("trace timestamps must be finite")
        if times[0] < 0:
            raise WrdataParseError("trace timestamps must be non-negative")
        if any(right < left for left, right in zip(times, times[1:])):
            raise WrdataParseError("trace timestamps must be monotonic")
        signals = {_normalise_name(name): tuple(float(value) for value in values) for name, values in self.signals.items()}
        if len(signals) != len(self.signals):
            raise WrdataParseError("trace signal names must be unique after normalisation")
        if not signals:
            raise WrdataParseError("trace table requires at least one signal")
        for name, values in signals.items():
            if not name:
                raise WrdataParseError("trace signal names must be non-empty")
            if len(values) != len(times):
                raise WrdataParseError(f"signal {name!r} length does not match the time column")
            if any(not math.isfinite(value) for value in values):
                raise WrdataParseError(f"signal {name!r} contains a non-finite value")
        object.__setattr__(self, "time_s", times)
        object.__setattr__(self, "signals", signals)

    @property
    def point_count(self) -> int:
        return len(self.time_s)

    @property
    def start_time_s(self) -> float:
        return self.time_s[0]

    @property
    def end_time_s(self) -> float:
        return self.time_s[-1]

    @property
    def signal_names(self) -> tuple[str, ...]:
        return tuple(self.signals)

    def value_at(self, signal: str, time_s: float) -> float:
        """Linearly interpolate one signal at an absolute time inside the table.

        The adapter only requests interpolation inside the solved window; a
        caller asking outside the table gets a fail-closed error instead of an
        invented extrapolation.
        """

        name = _normalise_name(signal)
        values = self.signals.get(name)
        if values is None:
            raise WrdataParseError(f"unknown trace signal: {signal!r}")
        target = float(time_s)
        if not math.isfinite(target):
            raise WrdataParseError("interpolation time must be finite")
        tolerance = 1e-15 * max(1.0, abs(target))
        if target < self.time_s[0] - tolerance or target > self.time_s[-1] + tolerance:
            raise WrdataParseError("interpolation time lies outside the solved window")
        if target <= self.time_s[0]:
            return values[0]
        if target >= self.time_s[-1]:
            return values[-1]
        # bisect_right keeps the left edge of a duplicate-time segment.
        low, high = 0, len(self.time_s) - 1
        while low + 1 < high:
            middle = (low + high) // 2
            if self.time_s[middle] <= target:
                low = middle
            else:
                high = middle
        left_time, right_time = self.time_s[low], self.time_s[high]
        left_value, right_value = values[low], values[high]
        if right_time == left_time:
            return right_value
        ratio = (target - left_time) / (right_time - left_time)
        return left_value + (right_value - left_value) * ratio

    def to_dict(self) -> dict[str, Any]:
        return {
            "point_count": self.point_count,
            "start_time_s": self.start_time_s,
            "end_time_s": self.end_time_s,
            "signals": list(self.signals),
        }


def parse_wrdata(text: str, *, expected_columns: int | None = None) -> TraceTable:
    """Parse a ``wrdata`` ASCII table into a validated ``TraceTable``.

    The parser accepts both common header layouts produced by ngspice: a
    header that names every column (including the scale), and a header that
    names only the data vectors (the scale column is implicit).  A file with no
    header is accepted only when ``expected_columns`` is supplied.
    """

    if not isinstance(text, str):
        raise WrdataParseError("wrdata content must be text")
    raw_lines = [line.strip() for line in text.splitlines()]
    lines = [line for line in raw_lines if line and not line.startswith(("*", "#", ";"))]
    if not lines:
        raise WrdataParseError("wrdata output is empty")

    header: list[str] | None = None
    first_tokens = lines[0].split()
    if first_tokens and not all(_is_number(token) for token in first_tokens):
        header = [_normalise_name(token) for token in first_tokens]
        rows = lines[1:]
    else:
        rows = lines

    if not rows:
        raise WrdataParseError("wrdata output contains a header but no samples")

    columns = [line.split() for line in rows]
    width = len(columns[0])
    if width < 2:
        raise WrdataParseError("wrdata output requires at least two columns")
    for index, tokens in enumerate(columns):
        if len(tokens) != width:
            raise WrdataParseError(f"wrdata row {index} has {len(tokens)} columns, expected {width}")

    if expected_columns is not None and width != expected_columns:
        raise WrdataParseError(f"wrdata output has {width} columns, expected {expected_columns}")

    if header is None:
        if expected_columns is None:
            raise WrdataParseError("wrdata output has no header; expected_columns is required")
        names = ["time"] + [f"signal_{index}" for index in range(1, width)]
    elif len(header) == width:
        names = header
    elif len(header) == width - 1:
        names = ["time"] + header
    else:
        raise WrdataParseError(f"wrdata header names {len(header)} columns, data has {width}")

    if not names[0]:
        names[0] = "time"
    if len(set(names)) != len(names):
        raise WrdataParseError("wrdata header contains duplicate column names")

    values: list[list[float]] = []
    for index, tokens in enumerate(columns):
        try:
            values.append([float(token) for token in tokens])
        except ValueError as exc:
            raise WrdataParseError(f"wrdata row {index} contains a non-numeric value") from exc

    times = tuple(row[0] for row in values)
    signals = {names[column]: tuple(row[column] for row in values) for column in range(1, width)}
    return TraceTable(times, signals)


__all__ = ["TraceTable", "WrdataParseError", "parse_wrdata"]

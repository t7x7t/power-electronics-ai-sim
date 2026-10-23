"""Bounded classification of solver logs.

A successful solver exit does not guarantee a clean numerical history: ngspice
may recover from a singular matrix, gmin stepping, or a reduced timestep and
still produce a complete waveform.  This module classifies such lines without
retaining the full log.  Fatal categories always take precedence over warning
categories on the same line.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import re
from typing import Any, Mapping, Sequence


SCHEMA_VERSION = "numerical-health-v1"

# (category, pattern).  Evaluated in order; the first match wins.
_FATAL_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("timestep_too_small", re.compile(r"(?:time\s*step|timestep)\s+too\s+small", re.I)),
    ("no_convergence", re.compile(r"doanalyses\s*:.*no convergence|^\s*no convergence\b", re.I)),
    ("convergence_failed", re.compile(r"convergence failed", re.I)),
    ("gmin_stepping_failed", re.compile(r"gmin stepping failed", re.I)),
    ("source_stepping_failed", re.compile(r"source stepping failed", re.I)),
    ("iteration_limit", re.compile(r"iteration limit reached", re.I)),
    ("fatal_error", re.compile(r"^\s*(?:fatal error|error\s*:)", re.I)),
)

_WARNING_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("singular_matrix", re.compile(r"singular matrix", re.I)),
    ("gmin_stepping", re.compile(r"gmin stepping", re.I)),
    ("source_stepping", re.compile(r"source stepping", re.I)),
    ("timestep_reduction", re.compile(r"(?:reducing|reduced|resetting)\s+timestep", re.I)),
    ("convergence_difficulty", re.compile(r"(?:trying|difficult|slow)\s+convergence", re.I)),
    ("floating_node", re.compile(r"floating node|no dc path to ground", re.I)),
    ("solver_warning", re.compile(r"^\s*warning\s*:", re.I)),
)

_ECHO_START = re.compile(r"^\s*Circuit\s*:", re.I)
_ECHO_END = re.compile(r"^\s*(?:Doing analysis|No\.\s*of Data Rows)", re.I)
_FATAL_ESCALATION = re.compile(r"doanalyses|fatal|abort|no convergence", re.I)

_MAX_CATEGORIES = 32


@dataclass(frozen=True)
class NumericalHealth:
    schema: str = SCHEMA_VERSION
    severity: str = "clean"  # clean | warning | fatal | unknown
    categories: Mapping[str, int] = field(default_factory=dict)
    warning_count: int = 0
    error_count: int = 0
    windows: int = 0
    excerpts: tuple[Mapping[str, Any], ...] = ()
    truncated: bool = False
    log_sha256: str | None = None

    def __post_init__(self) -> None:
        if self.severity not in {"clean", "warning", "fatal", "unknown"}:
            raise ValueError(f"unsupported numerical health severity: {self.severity!r}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "severity": self.severity,
            "categories": dict(sorted(self.categories.items())),
            "warning_count": int(self.warning_count),
            "error_count": int(self.error_count),
            "windows": int(self.windows),
            "excerpts": [dict(item) for item in self.excerpts],
            "truncated": bool(self.truncated),
            "log_sha256": self.log_sha256,
        }

    def merged(self, other: "NumericalHealth") -> "NumericalHealth":
        severity_order = {"clean": 0, "unknown": 1, "warning": 2, "fatal": 3}
        severity = self.severity
        if severity_order.get(other.severity, 0) > severity_order.get(severity, 0):
            severity = other.severity
        categories: dict[str, int] = dict(self.categories)
        for key, value in other.categories.items():
            categories[str(key)] = categories.get(str(key), 0) + int(value)
        if len(categories) > _MAX_CATEGORIES:
            overflow = sum(
                value
                for key, value in categories.items()
                if key not in dict(sorted(categories.items())[: _MAX_CATEGORIES])
            )
            categories = dict(sorted(categories.items())[: _MAX_CATEGORIES])
            categories["other"] = categories.get("other", 0) + overflow
        excerpts = tuple(self.excerpts) + tuple(other.excerpts)
        truncated = self.truncated or other.truncated or len(excerpts) > 3
        return NumericalHealth(
            severity=severity,
            categories=categories,
            warning_count=self.warning_count + other.warning_count,
            error_count=self.error_count + other.error_count,
            windows=self.windows + other.windows,
            excerpts=excerpts[:3],
            truncated=truncated,
            log_sha256=other.log_sha256 or self.log_sha256,
        )


def _excerpt(line: str, workdir: str | None) -> str:
    text = line.strip()
    if workdir:
        text = text.replace(workdir, "<workdir>")
        text = text.replace(workdir.replace("\\", "/"), "<workdir>")
    if len(text) > 240:
        text = text[:237] + "..."
    return text


def parse_log(
    text: str,
    *,
    workdir: str | None = None,
    max_excerpts: int = 3,
) -> NumericalHealth:
    """Classify one solver log without retaining its full text."""

    if not isinstance(text, str):
        return NumericalHealth(severity="unknown")
    digest = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()
    categories: dict[str, int] = {}
    warning_count = 0
    error_count = 0
    excerpts: list[dict[str, Any]] = []
    in_echo = False
    for line_number, line in enumerate(text.splitlines(), start=1):
        if _ECHO_START.search(line):
            in_echo = True
            continue
        if in_echo:
            if _ECHO_END.search(line):
                in_echo = False
            continue
        if not line.strip():
            continue
        fatal_category = next(
            (category for category, pattern in _FATAL_PATTERNS if pattern.search(line)),
            None,
        )
        if fatal_category is None and re.search(r"singular matrix", line, re.I) and _FATAL_ESCALATION.search(line):
            fatal_category = "singular_matrix_fatal"
        if fatal_category is not None:
            error_count += 1
            categories[fatal_category] = categories.get(fatal_category, 0) + 1
            if len(excerpts) < max_excerpts:
                excerpts.append(
                    {
                        "category": fatal_category,
                        "line": line_number,
                        "excerpt": _excerpt(line, workdir),
                        "log_sha256": digest,
                    }
                )
            continue
        warning_category = next(
            (category for category, pattern in _WARNING_PATTERNS if pattern.search(line)),
            None,
        )
        if warning_category is not None:
            warning_count += 1
            categories[warning_category] = categories.get(warning_category, 0) + 1
            if len(excerpts) < max_excerpts:
                excerpts.append(
                    {
                        "category": warning_category,
                        "line": line_number,
                        "excerpt": _excerpt(line, workdir),
                        "log_sha256": digest,
                    }
                )
    if error_count:
        severity = "fatal"
    elif warning_count:
        severity = "warning"
    else:
        severity = "clean"
    return NumericalHealth(
        severity=severity,
        categories=categories,
        warning_count=warning_count,
        error_count=error_count,
        windows=1,
        excerpts=tuple(excerpts),
        truncated=len(excerpts) >= max_excerpts,
        log_sha256=digest,
    )


__all__ = ["NumericalHealth", "SCHEMA_VERSION", "parse_log"]

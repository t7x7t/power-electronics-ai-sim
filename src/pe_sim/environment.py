"""Machine-readable checks for the repository's recommended environment.

The check deliberately has two independent expectations:

* ``requirements-tested.txt`` is the locally verified pin set.
* ``pyproject.toml`` declares the supported Python and dependency ranges.

This module only inspects the running Python environment.  It does not find,
install, or make claims about Ngspice or another external simulator.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import importlib.metadata
import re
import sys
import tomllib
from typing import Any, Mapping


_REQ_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9_.-]*)(?:\[[^]]+\])?\s*(.*)$")
_COMPARATOR_RE = re.compile(r"^(===|==|!=|~=|>=|<=|>|<)\s*([0-9][^,;\s]*)")


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _normalise_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", str(name)).lower()


def _parse_requirement(line: str) -> tuple[str, str] | None:
    line = line.split("#", 1)[0].strip()
    if not line or line.startswith("-"):
        return None
    # Markers are not needed for this small, platform-neutral test set.
    line = line.split(";", 1)[0].strip()
    match = _REQ_RE.match(line)
    if not match:
        return None
    return _normalise_name(match.group(1)), match.group(2).strip()


def _read_verified(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    result: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        item = _parse_requirement(line)
        if item is None:
            continue
        name, spec = item
        match = re.match(r"^==\s*([0-9][^\s,;]*)$", spec)
        if match:
            result[name] = match.group(1)
    return result


def _read_supported(path: Path) -> tuple[str | None, dict[str, str]]:
    if not path.exists():
        return None, {}
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    project = data.get("project", {})
    if not isinstance(project, Mapping):
        return None, {}
    python_spec = project.get("requires-python")
    supported: dict[str, str] = {}
    optional = project.get("optional-dependencies", {})
    if isinstance(optional, Mapping):
        for values in optional.values():
            if not isinstance(values, list):
                continue
            for value in values:
                item = _parse_requirement(str(value))
                if item is None:
                    continue
                name, spec = item
                # Keep the most informative declaration.  In this project the
                # test extra has one declaration per package.
                supported[name] = spec or "*"
    return (str(python_spec) if python_spec is not None else None), supported


def _version_tuple(value: str) -> tuple[int, ...]:
    match = re.match(r"^([0-9]+(?:\.[0-9]+)*)(?:[-+].*)?$", str(value).strip())
    if not match:
        raise ValueError(f"unsupported version format: {value!r}")
    return tuple(int(part) for part in match.group(1).split("."))


def _compare(actual: str, expected: str) -> int:
    left, right = _version_tuple(actual), _version_tuple(expected)
    width = max(len(left), len(right))
    return (left + (0,) * (width - len(left)) > right + (0,) * (width - len(right))) - (left + (0,) * (width - len(left)) < right + (0,) * (width - len(right)))


def _satisfies(actual: str, specification: str | None) -> bool | None:
    if not specification or specification == "*":
        return True
    try:
        for raw in str(specification).split(","):
            raw = raw.strip()
            match = _COMPARATOR_RE.match(raw)
            if not match:
                return None
            relation, expected = match.groups()
            cmp = _compare(actual, expected)
            if relation in {"==", "==="} and cmp != 0:
                return False
            if relation == "!=" and cmp == 0:
                return False
            if relation == ">=" and cmp < 0:
                return False
            if relation == "<=" and cmp > 0:
                return False
            if relation == ">" and cmp <= 0:
                return False
            if relation == "<" and cmp >= 0:
                return False
            if relation == "~=":
                # For the simple project declarations, ~=X.Y means >=X.Y
                # and <(X+1).Y.  Return unknown for a less obvious form.
                parts = _version_tuple(expected)
                if cmp < 0 or len(parts) < 2 or _version_tuple(actual)[0] != parts[0]:
                    return False
        return True
    except ValueError:
        return None


def _installed_version(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None
    except Exception:
        return None


def _python_status(verified: str | None, supported: str | None) -> dict[str, Any]:
    actual = platform_version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    supported_ok = _satisfies(actual, supported)
    verified_status = "not_declared" if verified is None else ("match" if actual == verified else "mismatch")
    return {
        "actual": actual,
        "verified": verified,
        "supported": supported,
        "verified_status": verified_status,
        "supported_status": "supported" if supported_ok is True else "unsupported" if supported_ok is False else "unknown",
    }


@dataclass(frozen=True)
class EnvironmentCheck:
    """Serializable result of a recommended-environment check."""

    status: str
    python: Mapping[str, Any]
    packages: Mapping[str, Mapping[str, Any]]
    sources: Mapping[str, str]
    backend: Mapping[str, Any]
    limitations: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "1.0",
            "status": self.status,
            "python": dict(self.python),
            "packages": {str(k): dict(v) for k, v in self.packages.items()},
            "sources": dict(self.sources),
            "backend": dict(self.backend),
            "limitations": list(self.limitations),
        }


def check_recommended_environment(
    requirements_path: str | Path | None = None,
    pyproject_path: str | Path | None = None,
    *,
    verified_python_version: str | None = "3.12.7",
) -> EnvironmentCheck:
    """Compare installed versions with verified pins and supported ranges.

    The Python pin defaults to the version recorded in the repository's
    tested-environment document.  Callers may pass ``None`` when that pin is
    not available; the supported range is still checked.
    """

    root = _project_root()
    requirements = Path(requirements_path) if requirements_path else root / "requirements-tested.txt"
    pyproject = Path(pyproject_path) if pyproject_path else root / "pyproject.toml"
    verified = _read_verified(requirements)
    python_supported, supported = _read_supported(pyproject)
    python_result = _python_status(verified_python_version, python_supported)
    packages: dict[str, dict[str, Any]] = {}
    failures = 0
    unknowns = 0
    names = sorted(set(verified) | set(supported))
    for name in names:
        actual = _installed_version(name)
        expected_verified = verified.get(name)
        expected_supported = supported.get(name)
        verified_status = "not_declared"
        if expected_verified is not None:
            if actual is None:
                verified_status = "missing"
            else:
                verified_status = "match" if actual == expected_verified else "mismatch"
        supported_ok = None if actual is None else _satisfies(actual, expected_supported)
        supported_status = "not_declared" if expected_supported is None else (
            "missing" if actual is None else "supported" if supported_ok is True else "unsupported" if supported_ok is False else "unknown"
        )
        if verified_status in {"missing", "mismatch"} or supported_status in {"missing", "unsupported"}:
            failures += 1
        if verified_status == "not_declared" or supported_status == "unknown":
            unknowns += 1
        packages[name] = {
            "actual": actual,
            "verified": expected_verified,
            "supported": expected_supported,
            "verified_status": verified_status,
            "supported_status": supported_status,
        }
    if python_result["supported_status"] == "unsupported":
        failures += 1
    if python_result["supported_status"] == "unknown":
        unknowns += 1
    if failures:
        status = "fail"
    elif unknowns:
        status = "unknown"
    else:
        status = "pass"
    limitations: list[str] = [
        "This check validates the Python package environment only; it does not install dependencies.",
        "Ngspice and other external simulator executables are not assessed.",
    ]
    if not verified:
        limitations.append(f"verified requirement pins were unavailable: {requirements}")
    return EnvironmentCheck(
        status=status,
        python=python_result,
        packages=packages,
        sources={"verified": str(requirements.name), "supported": str(pyproject.name)},
        backend={"status": "not_assessed", "reason": "real Ngspice/backend discovery is deferred to a reviewed adapter"},
        limitations=tuple(limitations),
    )


def environment_check_to_dict(**kwargs: Any) -> dict[str, Any]:
    """Convenience wrapper for JSON/CLI callers."""

    return check_recommended_environment(**kwargs).to_dict()


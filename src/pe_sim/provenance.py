"""Read-only source provenance helpers.

The runner must never invent a source revision.  When Git is unavailable or
the requested path is not inside a repository, the result is explicitly
marked ``unknown`` so callers can apply their own publication policy.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from collections.abc import Mapping, Sequence
import importlib.metadata
import hashlib
import locale
import os
import platform
import re
import subprocess
import sys
from typing import Protocol, runtime_checkable, Any


@runtime_checkable
class BackendProvenanceAdapter(Protocol):
    """Optional adapter contract for external simulator evidence.

    An adapter must return a JSON-serialisable mapping.  The generic runner
    never guesses a backend version; adapters may discover it from an
    executable or declare it from a controlled package installation.
    """

    def backend_provenance(self) -> Mapping[str, Any]:
        ...


def not_assessed_backend(name: str = "external-backend", *, reason: str | None = None) -> dict[str, object]:
    """Return an explicit absence-of-assessment record.

    ``not_assessed`` is intentionally distinct from ``unknown``: no adapter
    was supplied, so no discovery attempt was made and no version claim is
    possible.
    """

    return {
        "status": "not_assessed",
        "name": str(name),
        "version": None,
        "executable": None,
        "solver_settings": None,
        "limitations": [reason or "no reviewed backend provenance adapter was supplied"],
    }


def _version_from_output(output: str) -> str | None:
    """Extract a conservative version token from common ``--version`` text."""

    # Prefer a token adjacent to a backend name, then fall back to a plain
    # semantic version.  Returning ``None`` is safer than treating arbitrary
    # command output as a verified version.
    match = re.search(r"(?:ngspice|ltspice|xyce|version)\s*[-_:]?\s*v?([0-9]+(?:\.[0-9]+)+(?:[-+][0-9A-Za-z.-]+)?)", output, re.I)
    if match:
        return match.group(1)
    match = re.search(r"\bv?([0-9]+(?:\.[0-9]+){1,2}(?:[-+][0-9A-Za-z.-]+)?)\b", output)
    return match.group(1) if match else None


@dataclass(frozen=True)
class ExecutableBackendAdapter:
    """Small opt-in adapter for recording an external executable identity.

    This class performs discovery only when explicitly instantiated by a
    caller.  Missing executables, non-zero exits, and unparseable output are
    retained as ``unavailable``/``partial`` evidence rather than fabricated
    versions.  Solver settings are supplied by the caller because the generic
    infrastructure cannot infer a simulator's numerical configuration.
    """

    executable: str | Path
    name: str = "external-backend"
    version_args: tuple[str, ...] = ("--version",)
    solver_settings: Mapping[str, Any] | None = None
    timeout_s: float = 5.0

    def backend_provenance(self) -> Mapping[str, Any]:
        command = str(self.executable)
        base: dict[str, Any] = {
            "name": self.name,
            "executable": command,
            "version_args": list(self.version_args),
            "solver_settings": dict(self.solver_settings) if self.solver_settings is not None else None,
        }
        try:
            completed = subprocess.run(
                [command, *self.version_args],
                check=False,
                capture_output=True,
                text=True,
                timeout=float(self.timeout_s),
            )
        except (FileNotFoundError, OSError, subprocess.SubprocessError, ValueError) as exc:
            return {
                **base,
                "status": "unavailable",
                "version": None,
                "returncode": None,
                "limitations": [f"backend executable could not be queried: {type(exc).__name__}: {exc}"],
            }
        output = (completed.stdout or "") + ("\n" + completed.stderr if completed.stderr else "")
        version = _version_from_output(output)
        limitations: list[str] = []
        status = "known" if completed.returncode == 0 and version and self.solver_settings is not None else "partial"
        if completed.returncode != 0:
            limitations.append(f"backend version command returned exit code {completed.returncode}")
        if version is None:
            limitations.append("backend version output could not be parsed")
        if self.solver_settings is None:
            limitations.append("solver settings were not declared by the adapter")
        return {
            **base,
            "status": status,
            "version": version,
            "returncode": completed.returncode,
            "version_output_sha256": hashlib.sha256(output.encode("utf-8", errors="replace")).hexdigest(),
            "limitations": limitations,
        }


@dataclass(frozen=True)
class GitProvenance:
    """The minimum source identity needed to bind a run to its code."""

    source_commit: str = "unavailable"
    branch: str = "unavailable"
    working_tree_status: str = "unknown"
    status: str = "unknown"
    limitations: tuple[str, ...] = ("Git repository or source revision unavailable",)

    def to_manifest_fields(self) -> dict[str, object]:
        """Return fields suitable for the top-level manifest."""

        return {
            "source_commit": self.source_commit,
            "branch": self.branch,
            "working_tree_status": self.working_tree_status,
            "provenance": {
                "status": self.status,
                "producer": "pe_sim",
                "limitations": list(self.limitations),
            },
        }


def _git(path: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(path), *args],
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    )
    return result.stdout.strip()


def collect_git_provenance(path: str | Path | None = None) -> GitProvenance:
    """Collect commit, branch and clean/dirty status without mutating Git.

    ``path`` defaults to the process working directory.  A clean repository is
    ``known``; a dirty repository is ``exploratory``; failures are ``unknown``.
    The distinction is intentionally conservative and is recorded in the
    manifest so a downstream tool can reject non-clean runs.
    """

    root = Path(path or Path.cwd()).resolve()
    try:
        commit = _git(root, "rev-parse", "HEAD")
        if not commit:
            raise RuntimeError("empty git revision")
        branch = _git(root, "symbolic-ref", "--short", "-q", "HEAD") or "detached"
        porcelain = _git(root, "status", "--porcelain", "--untracked-files=all")
    except (FileNotFoundError, OSError, RuntimeError, subprocess.SubprocessError) as exc:
        return GitProvenance(limitations=(f"Git provenance unavailable: {exc}",))

    dirty = bool(porcelain)
    status = "exploratory" if dirty else "known"
    limitations: tuple[str, ...] = (
        ("working tree contains uncommitted or untracked changes",) if dirty else ()
    )
    return GitProvenance(
        source_commit=commit,
        branch=branch,
        working_tree_status="dirty" if dirty else "clean",
        status=status,
        limitations=limitations,
    )


def _metadata_version(distribution: str) -> str | None:
    """Read an installed distribution version without turning absence into a lie."""

    try:
        return importlib.metadata.version(distribution)
    except importlib.metadata.PackageNotFoundError:
        return None
    except Exception:
        return None


def collect_backend_provenance(adapter: object | None) -> dict[str, object]:
    """Normalize an explicit backend adapter into portable evidence.

    ``adapter`` may expose ``backend_provenance()``/``provenance()`` or be a
    mapping.  A missing adapter yields ``not_assessed``.  This helper is useful
    for environment checks and cross-machine manifests where no Plant object
    is available yet.
    """

    if adapter is None:
        return not_assessed_backend()
    if isinstance(adapter, Mapping):
        value: object = adapter
    else:
        provider = getattr(adapter, "backend_provenance", None)
        if not callable(provider):
            provider = getattr(adapter, "provenance", None)
        if callable(provider):
            try:
                value = provider()
            except Exception as exc:  # pragma: no cover - defensive boundary
                return {
                    **not_assessed_backend(type(adapter).__qualname__, reason="backend provenance adapter raised"),
                    "status": "unavailable",
                    "limitations": [f"backend provenance raised {type(exc).__name__}: {exc}"],
                }
        else:
            return not_assessed_backend(type(adapter).__qualname__, reason="adapter does not expose provenance()")
    if not isinstance(value, Mapping):
        return {
            **not_assessed_backend(type(adapter).__qualname__, reason="adapter returned a non-mapping provenance value"),
            "status": "unavailable",
        }
    result = dict(value)
    result.setdefault("name", type(adapter).__qualname__ if not isinstance(adapter, Mapping) else "external-backend")
    result.setdefault("version", None)
    result.setdefault("solver_settings", None)
    result.setdefault("status", "known" if result.get("version") else "partial")
    limitations = result.get("limitations")
    if limitations is None:
        result["limitations"] = [] if result.get("version") else ["backend version was not declared"]
    elif isinstance(limitations, str):
        result["limitations"] = [limitations]
    else:
        result["limitations"] = [str(item) for item in limitations]
    if result.get("status") == "known" and result.get("solver_settings") is None:
        result["status"] = "partial"
        if "solver settings were not declared by the adapter" not in result["limitations"]:
            result["limitations"].append("solver settings were not declared by the adapter")
    return result


def _declared_backend(component: object) -> dict[str, object]:
    """Extract optional backend provenance declared by an adapter.

    The generic runtime cannot infer a solver version from a Plant object. An
    adapter may expose ``backend_provenance()`` (preferred) or a mapping-valued
    ``backend_provenance`` attribute. Missing declarations are represented as
    ``not_declared`` with a limitation instead of an ``unknown`` placeholder.
    """

    provider = getattr(component, "backend_provenance", None)
    value: object = None
    if callable(provider):
        try:
            value = provider()
        except Exception as exc:  # pragma: no cover - defensive adapter boundary
            return {
                "status": "unavailable",
                "name": type(component).__qualname__,
                "version": None,
                "solver_settings": None,
                "limitations": [f"backend provenance raised {type(exc).__name__}: {exc}"],
            }
    elif isinstance(provider, Mapping):
        value = provider
    if isinstance(value, Mapping):
        result = dict(value)
        result.setdefault("status", "known" if result.get("version") else "partial")
        result.setdefault("name", type(component).__qualname__)
        result.setdefault("version", None)
        result.setdefault("solver_settings", None)
        result.setdefault("limitations", [] if result.get("version") else ["backend version was not declared"])
        if result.get("status") == "known" and result.get("solver_settings") is None:
            result["status"] = "partial"
            result["limitations"] = [*result["limitations"], "solver settings were not declared by the adapter"]
        return result

    # A few adapters expose simple attributes instead of a method.
    name = getattr(component, "backend_name", None)
    version = getattr(component, "backend_version", None)
    settings = getattr(component, "solver_settings", None)
    if name is not None or version is not None or settings is not None:
        limitations = [] if version is not None else ["backend version was not declared"]
        return {
            "status": "known" if version is not None and settings is not None else "partial",
            "name": str(name or type(component).__qualname__),
            "version": version,
            "solver_settings": settings,
            "limitations": limitations if settings is not None else [*limitations, "solver settings were not declared by the adapter"],
        }
    return {
        "status": "not_declared",
        "name": type(component).__qualname__,
        "version": None,
        "solver_settings": None,
        "limitations": ["adapter does not declare backend version or solver settings"],
    }


def collect_environment_provenance(
    components: Sequence[object] = (),
    *,
    backend_adapters: Sequence[object] = (),
    backend_adapter: object | None = None,
) -> dict[str, object]:
    """Collect portable runtime, platform, dependency and backend evidence.

    Python and platform values are obtained from the running interpreter. The
    infrastructure has no mandatory third-party runtime dependencies, so the
    dependency list records the project distribution when installed. Backend
    identity is adapter-declared; for the built-in fake/reference fixtures it
    is explicitly ``not_declared`` rather than silently reported as a version.
    """

    if backend_adapter is not None:
        backend_adapters = tuple(backend_adapters) + (backend_adapter,)
    limitations: list[str] = []
    runner_version = _metadata_version("power-electronics-ai-sim")
    if runner_version is None:
        limitations.append("project distribution metadata is unavailable")
    dependencies: dict[str, str] = {}
    for distribution in ("numpy", "jsonschema"):
        version = _metadata_version(distribution)
        if version is not None:
            dependencies[distribution] = version

    backends: list[dict[str, object]] = []
    seen: set[tuple[str, str, str]] = set()
    for component in components:
        backend = _declared_backend(component)
        marker = (str(backend.get("name")), str(backend.get("version")), str(backend.get("solver_settings")))
        if marker not in seen:
            seen.add(marker)
            backends.append(backend)
        limitations.extend(str(item) for item in backend.get("limitations", ()))
    for adapter in backend_adapters:
        backend = collect_backend_provenance(adapter)
        marker = (str(backend.get("name")), str(backend.get("version")), str(backend.get("solver_settings")))
        if marker not in seen:
            seen.add(marker)
            backends.append(backend)
        limitations.extend(str(item) for item in backend.get("limitations", ()))
    if not backends:
        backends.append(not_assessed_backend())
        limitations.extend(backends[-1]["limitations"])
    backend_statuses = {str(item.get("status")) for item in backends}
    if any(status in {"unavailable", "unknown"} for status in backend_statuses):
        status = "partial"
    elif any(status in {"partial", "not_declared", "not_assessed"} for status in backend_statuses):
        status = "partial"
    else:
        status = "known"
    return {
        "status": status,
        "runner": {
            "name": "pe_sim",
            "distribution": "power-electronics-ai-sim",
            "version": runner_version,
        },
        "python": {
            "implementation": platform.python_implementation(),
            "version": platform.python_version(),
            "version_info": list(sys.version_info[:3]),
        },
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "architecture": platform.architecture()[0],
        },
        # These values are descriptive evidence only.  A backend adapter must
        # still declare the solver's actual tolerances and thread settings.
        "execution": {
            "locale": locale.setlocale(locale.LC_NUMERIC),
            "floating_point": {
                "radix": sys.float_info.radix,
                "mant_dig": sys.float_info.mant_dig,
                "epsilon": sys.float_info.epsilon,
                "max": sys.float_info.max,
            },
            "thread_environment": {
                key: os.environ[key]
                for key in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS")
                if key in os.environ
            },
        },
        "dependencies": dependencies,
        "backends": backends,
        "limitations": sorted(set(limitations)),
    }

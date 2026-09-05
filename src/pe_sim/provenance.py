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
import platform
import subprocess
import sys


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
        return result

    # A few adapters expose simple attributes instead of a method.
    name = getattr(component, "backend_name", None)
    version = getattr(component, "backend_version", None)
    settings = getattr(component, "solver_settings", None)
    if name is not None or version is not None or settings is not None:
        limitations = [] if version is not None else ["backend version was not declared"]
        return {
            "status": "known" if version is not None else "partial",
            "name": str(name or type(component).__qualname__),
            "version": version,
            "solver_settings": settings,
            "limitations": limitations,
        }
    return {
        "status": "not_declared",
        "name": type(component).__qualname__,
        "version": None,
        "solver_settings": None,
        "limitations": ["adapter does not declare backend version or solver settings"],
    }


def collect_environment_provenance(components: Sequence[object] = ()) -> dict[str, object]:
    """Collect portable runtime, platform, dependency and backend evidence.

    Python and platform values are obtained from the running interpreter. The
    infrastructure has no mandatory third-party runtime dependencies, so the
    dependency list records the project distribution when installed. Backend
    identity is adapter-declared; for the built-in fake/reference fixtures it
    is explicitly ``not_declared`` rather than silently reported as a version.
    """

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
    backend_statuses = {str(item.get("status")) for item in backends}
    if any(status in {"unavailable", "unknown"} for status in backend_statuses):
        status = "partial"
    elif any(status in {"partial", "not_declared"} for status in backend_statuses):
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
        "dependencies": dependencies,
        "backends": backends,
        "limitations": sorted(set(limitations)),
    }

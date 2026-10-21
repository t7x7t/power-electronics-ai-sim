"""External simulator discovery without storing binaries or local paths.

The repository keeps only adapter code.  A user points the runtime at an
installed simulator through, in order:

1. an explicit CLI argument (``--ngspice``);
2. the ``PE_SIM_NGSPICE`` environment variable;
3. a user-level configuration file outside the checkout;
4. ``PATH`` discovery.

The user-level file location is:

- Windows: ``%APPDATA%\\pe-sim\\backends.toml``
- Linux/macOS: ``$XDG_CONFIG_HOME/pe-sim/backends.toml`` or
  ``~/.config/pe-sim/backends.toml``

``PE_SIM_CONFIG_HOME`` overrides the base directory, which is useful for tests
and for portable installations.  No discovery step writes into the repository.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import shutil
from typing import Any, Iterable, Mapping

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised on Python 3.10
    import tomli as tomllib  # type: ignore[no-redef]

from .registry import BackendDescriptor, get_backend


CONFIG_SCHEMA_VERSION = "1.0"
CONFIG_ENV = "PE_SIM_CONFIG_HOME"
NGSPICE_ENV = "PE_SIM_NGSPICE"
_SKIP_DIRECTORIES = frozenset({"node_modules", ".git", "__pycache__", ".venv", "venv", "dist", "build"})


class BackendConfigError(ValueError):
    """Raised when a user-level backend configuration is malformed."""


class BackendNotFoundError(FileNotFoundError):
    """Raised when no reviewed executable can be resolved."""

    def __init__(self, backend: str, attempts: Iterable[str]):
        self.backend = backend
        self.attempts = tuple(str(item) for item in attempts)
        super().__init__(
            f"{backend} executable was not found; attempted: " + "; ".join(self.attempts)
        )


def user_config_path() -> Path:
    """Return the user-level configuration path without creating it."""

    override = os.environ.get(CONFIG_ENV)
    if override:
        return Path(override).expanduser() / "backends.toml"
    if os.name == "nt":
        base = os.environ.get("APPDATA")
        root = Path(base) if base else Path.home() / "AppData" / "Roaming"
        return root / "pe-sim" / "backends.toml"
    xdg = os.environ.get("XDG_CONFIG_HOME")
    root = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return root / "pe-sim" / "backends.toml"


def load_backend_config(path: str | Path | None = None) -> Mapping[str, Any]:
    """Read and validate the optional user-level backend configuration."""

    config_path = Path(path) if path is not None else user_config_path()
    if not config_path.is_file():
        return {}
    try:
        document = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise BackendConfigError(f"backend configuration could not be read: {exc}") from exc
    if not isinstance(document, Mapping):
        raise BackendConfigError("backend configuration must be a TOML table")
    schema = document.get("schema_version")
    if schema is not None and str(schema) != CONFIG_SCHEMA_VERSION:
        raise BackendConfigError(
            f"unsupported backend configuration schema_version {schema!r}; expected {CONFIG_SCHEMA_VERSION!r}"
        )
    return document


def _configured_executable(config: Mapping[str, Any], backend: str) -> str | None:
    section = config.get(backend)
    if section is None:
        return None
    if not isinstance(section, Mapping):
        raise BackendConfigError(f"[{backend}] must be a table")
    executable = section.get("executable")
    if executable is None:
        return None
    if not isinstance(executable, str) or not executable.strip():
        raise BackendConfigError(f"[{backend}].executable must be a non-empty string")
    return executable.strip()


def _resolve_file(candidate: str) -> Path | None:
    path = Path(candidate).expanduser()
    if path.is_file():
        return path.resolve()
    located = shutil.which(candidate)
    return Path(located).resolve() if located else None


def _prefer_console_variant(path: Path, sibling: str | None) -> tuple[Path, tuple[str, ...]]:
    """Prefer a declared console sibling over a GUI executable.

    Official Windows ngspice builds may ship both ``ngspice.exe`` (GUI) and
    ``ngspice_con.exe`` (console).  The GUI variant can open a window or ignore
    batch output, so the reviewed adapter prefers the console build when the
    descriptor declares it.
    """

    notes: list[str] = []
    if sibling and path.name.lower() == "ngspice.exe":
        console = path.with_name(sibling)
        if console.is_file():
            notes.append(f"GUI {path.name} was replaced by the sibling console {sibling}")
            return console.resolve(), tuple(notes)
        notes.append(
            f"resolved {path.name} is a GUI variant; batch mode may not behave like the console build"
        )
    return path, tuple(notes)


@dataclass(frozen=True)
class BackendResolution:
    """One resolved external executable and where the decision came from."""

    backend: str
    requested: str
    executable: str
    source: str
    notes: tuple[str, ...] = ()
    config_path: Path | None = None

    @property
    def executable_name(self) -> str:
        return Path(self.executable).name

    def to_dict(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "requested": self.requested,
            "executable": self.executable,
            "executable_name": self.executable_name,
            "source": self.source,
            "notes": list(self.notes),
            "config_path": str(self.config_path) if self.config_path is not None else None,
        }


def resolve_backend_executable(
    descriptor: BackendDescriptor,
    explicit: str | None = None,
    *,
    config: Mapping[str, Any] | None = None,
    config_path: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> BackendResolution:
    """Resolve an executable backend using the documented order.

    The environment variable name is derived from the backend name, for
    example ``PE_SIM_NGSPICE``.  The configuration section is the backend name.
    """

    if descriptor.kind != "executable":
        raise ValueError(f"backend {descriptor.name!r} is not executable")
    environment = os.environ if environ is None else environ
    environment_name = f"PE_SIM_{descriptor.name.upper()}"
    attempts: list[str] = []

    def _accept(requested: str, located: Path, source: str, *, config_file: Path | None = None) -> BackendResolution:
        executable, notes = _prefer_console_variant(located, descriptor.console_sibling)
        return BackendResolution(
            descriptor.name,
            requested,
            str(executable),
            source,
            notes,
            config_file,
        )

    if explicit:
        attempts.append(f"explicit argument: {explicit}")
        located = _resolve_file(explicit)
        if located is not None:
            return _accept(explicit, located, "explicit")
        raise BackendNotFoundError(descriptor.name, attempts)

    environment_value = environment.get(environment_name)
    if environment_value:
        attempts.append(f"{environment_name}: {environment_value}")
        located = _resolve_file(environment_value)
        if located is not None:
            return _accept(environment_value, located, "environment")
        raise BackendNotFoundError(descriptor.name, attempts)

    # Load the user configuration only after explicit and environment inputs
    # have been checked, so a malformed optional file cannot break an explicit
    # command-line selection.
    loaded = load_backend_config(config_path) if config is None else dict(config)
    resolved_config_path = Path(config_path) if config_path is not None else user_config_path()
    configured = _configured_executable(loaded, descriptor.config_section)
    if configured:
        attempts.append(f"config {resolved_config_path}: {configured}")
        located = _resolve_file(configured)
        if located is not None:
            return _accept(configured, located, "config", config_file=resolved_config_path)
        raise BackendNotFoundError(descriptor.name, attempts)

    for candidate in descriptor.executable_names:
        attempts.append(f"PATH: {candidate}")
        located = _resolve_file(candidate)
        if located is not None:
            return _accept(candidate, located, "path")

    raise BackendNotFoundError(descriptor.name, attempts)


def resolve_ngspice_executable(
    explicit: str | None = None,
    *,
    config: Mapping[str, Any] | None = None,
    config_path: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> BackendResolution:
    """Resolve the reviewed ngspice executable using the documented order."""

    return resolve_backend_executable(
        get_backend("ngspice"),
        explicit,
        config=config,
        config_path=config_path,
        environ=environ,
    )


def scan_for_backend(
    descriptor: BackendDescriptor,
    roots: Iterable[str | Path],
    *,
    max_depth: int = 4,
) -> list[Path]:
    """Search local roots for a backend's executables without writing anything.

    This is a convenience for locating an existing installation.  The result is
    printed by ``pe-sim backend scan``; it is never committed or auto-selected.
    """

    if max_depth < 1:
        raise ValueError("max_depth must be at least 1")
    found: list[Path] = []
    names = set(descriptor.executable_names)
    for root_value in roots:
        root = Path(root_value).expanduser()
        if not root.is_dir():
            continue
        base_depth = len(root.resolve().parts)
        for directory, subdirectories, filenames in os.walk(root):
            depth = len(Path(directory).parts) - base_depth
            if depth >= max_depth:
                subdirectories[:] = []
            subdirectories[:] = [
                name for name in subdirectories if name.lower() not in _SKIP_DIRECTORIES
            ]
            for name in names:
                if name in filenames:
                    candidate = Path(directory) / name
                    if candidate.is_file():
                        found.append(candidate.resolve())
    preferred = (descriptor.console_sibling or "").lower()
    if preferred:
        ordering = lambda item: (item.name.lower() != preferred, str(item).lower())
    else:
        ordering = lambda item: (str(item).lower(),)
    return sorted(set(found), key=ordering)


def scan_for_ngspice(roots: Iterable[str | Path], *, max_depth: int = 4) -> list[Path]:
    """Backward-compatible wrapper for the ngspice descriptor."""

    return scan_for_backend(get_backend("ngspice"), roots, max_depth=max_depth)


def config_template(
    executable: str = "<path-to-ngspice-console-executable>",
    *,
    backend: str = "ngspice",
    minimum_major: int = 40,
) -> str:
    """Return a copyable user-level configuration block."""

    return (
        f'schema_version = "{CONFIG_SCHEMA_VERSION}"\n\n'
        f"[{backend}]\n"
        f'executable = "{executable}"\n'
        f"minimum_major = {int(minimum_major)}\n"
    )


def _repository_root() -> Path:
    return Path(__file__).resolve().parents[3]


def write_user_config(
    descriptor: BackendDescriptor,
    executable: str | Path,
    *,
    config_path: str | Path | None = None,
) -> Path:
    """Write or update the user-level configuration outside the repository.

    This is an explicit side effect requested by ``--write-config``.  The
    function refuses to write inside the project checkout so machine-local
    paths cannot accidentally become committed configuration.
    """

    if descriptor.kind != "executable":
        raise BackendConfigError(
            f"backend {descriptor.name!r} is not an executable and has no path to store"
        )
    path = Path(config_path).expanduser() if config_path is not None else user_config_path()
    try:
        path.resolve().relative_to(_repository_root())
    except ValueError:
        pass
    else:
        raise BackendConfigError("refusing to write the user configuration inside the repository")

    existing = dict(load_backend_config(path)) if path.is_file() else {}
    existing["schema_version"] = CONFIG_SCHEMA_VERSION
    current = existing.get(descriptor.config_section)
    section = dict(current) if isinstance(current, Mapping) else {}
    section["executable"] = Path(executable).as_posix()
    section.setdefault("minimum_major", descriptor.minimum_major)
    existing[descriptor.config_section] = section

    lines = [f'schema_version = "{CONFIG_SCHEMA_VERSION}"', ""]
    for name in sorted(key for key in existing if key != "schema_version"):
        value = existing[name]
        if not isinstance(value, Mapping):
            continue
        lines.append(f"[{name}]")
        stored = value.get("executable")
        if isinstance(stored, str):
            lines.append(f'executable = "{stored}"')
        minimum = value.get("minimum_major")
        if isinstance(minimum, int) and not isinstance(minimum, bool):
            lines.append(f"minimum_major = {minimum}")
        lines.append("")

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text("\n".join(lines), encoding="utf-8")
    os.replace(temporary, path)
    return path


__all__ = [
    "BackendConfigError",
    "BackendNotFoundError",
    "BackendResolution",
    "CONFIG_ENV",
    "CONFIG_SCHEMA_VERSION",
    "NGSPICE_ENV",
    "config_template",
    "load_backend_config",
    "resolve_ngspice_executable",
    "scan_for_ngspice",
    "user_config_path",
]

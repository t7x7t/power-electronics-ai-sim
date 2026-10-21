"""Data-only registry of supported and planned simulation backends.

The registry describes how to find, verify, and install an engine.  Adapter
implementations remain in their own modules.  Adding a new executable backend
should require a descriptor entry plus a reviewed adapter, not changes to the
Runner, Plant contract, or evidence rules.

No install command is executed automatically and no binary is stored in the
repository.  ``pe-sim backend guide`` and ``pe-sim backend setup`` print the
platform-specific commands for the user to run.
"""

from __future__ import annotations

from dataclasses import dataclass
import sys
from typing import Any


SUPPORTED = "supported"
PLANNED = "planned"
OPTIONAL_LIBRARY = "optional-library"

WINDOWS = "windows"
LINUX = "linux"
MACOS = "macos"
ANY = "any"


def current_platform() -> str:
    """Return the registry platform name for the running interpreter."""

    if sys.platform.startswith("win"):
        return WINDOWS
    if sys.platform == "darwin":
        return MACOS
    return LINUX


@dataclass(frozen=True)
class InstallRecipe:
    """One platform-specific installation path for an external engine."""

    platform: str
    method: str
    commands: tuple[str, ...]
    notes: tuple[str, ...] = ()
    url: str | None = None

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "platform": self.platform,
            "method": self.method,
            "commands": list(self.commands),
            "notes": list(self.notes),
        }
        if self.url is not None:
            value["url"] = self.url
        return value


@dataclass(frozen=True)
class BackendDescriptor:
    """Capability, discovery, and onboarding metadata for one backend."""

    name: str
    display_name: str
    kind: str
    status: str
    executable_names: tuple[str, ...] = ()
    version_args: tuple[str, ...] = ("--version",)
    minimum_major: int = 0
    console_sibling: str | None = None
    documentation: str | None = None
    install_recipes: tuple[InstallRecipe, ...] = ()
    notes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.kind not in {"executable", "library"}:
            raise ValueError("backend kind must be 'executable' or 'library'")
        if self.status not in {SUPPORTED, PLANNED, OPTIONAL_LIBRARY}:
            raise ValueError("unsupported backend status")
        if self.kind == "executable" and not self.executable_names:
            raise ValueError("executable backends must declare executable_names")

    @property
    def config_section(self) -> str:
        return self.name

    def recipes_for(self, platform: str | None = None) -> tuple[InstallRecipe, ...]:
        target = platform or current_platform()
        return tuple(
            recipe for recipe in self.install_recipes if recipe.platform in {target, ANY}
        )

    def to_dict(self, platform: str | None = None) -> dict[str, Any]:
        target = platform or current_platform()
        return {
            "name": self.name,
            "display_name": self.display_name,
            "kind": self.kind,
            "status": self.status,
            "config_section": self.config_section,
            "executable_names": list(self.executable_names),
            "version_args": list(self.version_args),
            "minimum_major": self.minimum_major,
            "console_sibling": self.console_sibling,
            "documentation": self.documentation,
            "notes": list(self.notes),
            "platform": target,
            "install_recipes": [recipe.to_dict() for recipe in self.recipes_for(target)],
        }


NGSPICE = BackendDescriptor(
    name="ngspice",
    display_name="ngspice",
    kind="executable",
    status=SUPPORTED,
    executable_names=("ngspice_con.exe", "ngspice.exe", "ngspice"),
    version_args=("--version",),
    minimum_major=40,
    console_sibling="ngspice_con.exe",
    documentation="docs/31_ngspice_backend_scope_v1.md",
    install_recipes=(
        InstallRecipe(
            WINDOWS,
            "conda",
            (
                "conda create -n pe-sim-ngspice -c conda-forge python=3.12 ngspice",
                "conda activate pe-sim-ngspice",
                "python -m pip install -e .",
                "pe-sim backend doctor --name ngspice",
            ),
            notes=(
                "conda-forge installs both ngspice_con.exe (console) and ngspice.exe (GUI).",
                "Accept the conda channel Terms of Service first if conda asks for it.",
            ),
        ),
        InstallRecipe(
            WINDOWS,
            "manual",
            (
                "# download the official Windows archive and extract it to a non-system drive",
                "# example: D:\\tools\\ngspice-44.2",
                "# then either add Spice64\\bin to PATH or run:",
                'pe-sim backend setup --name ngspice --executable "D:\\tools\\ngspice-44.2\\Spice64\\bin\\ngspice_con.exe" --write-config',
            ),
            url="https://ngspice.sourceforge.io/download.html",
            notes=("Prefer the console build ngspice_con.exe; the GUI build may ignore batch output.",),
        ),
        InstallRecipe(LINUX, "apt", ("sudo apt install ngspice", "pe-sim backend doctor --name ngspice")),
        InstallRecipe(MACOS, "brew", ("brew install ngspice", "pe-sim backend doctor --name ngspice")),
    ),
)

XYCE = BackendDescriptor(
    name="xyce",
    display_name="Xyce",
    kind="executable",
    status=PLANNED,
    executable_names=("Xyce.exe", "Xyce", "xyce"),
    version_args=("-v",),
    documentation="docs/33_backend_plugin_contract_v1.md",
    install_recipes=(
        InstallRecipe(
            WINDOWS,
            "conda",
            ("conda install -c conda-forge xyce",),
            notes=("Adapter not implemented yet; the descriptor is registered to show the extension path.",),
        ),
        InstallRecipe(LINUX, "conda", ("conda install -c conda-forge xyce",)),
        InstallRecipe(MACOS, "conda", ("conda install -c conda-forge xyce",)),
    ),
    notes=("Planned backend: registry entry only; no reviewed adapter or acceptance evidence yet.",),
)

LTSPICE = BackendDescriptor(
    name="ltspice",
    display_name="LTspice",
    kind="executable",
    status=PLANNED,
    executable_names=("LTspice.exe", "ltspice"),
    version_args=("-v",),
    documentation="docs/33_backend_plugin_contract_v1.md",
    install_recipes=(
        InstallRecipe(
            WINDOWS,
            "manual",
            ("# install LTspice from Analog Devices, then point the user config at LTspice.exe",),
            url="https://www.analog.com/en/resources/design-tools-and-calculators/ltspice-simulator.html",
            notes=("Adapter not implemented yet; the descriptor is registered to show the extension path.",),
        ),
    ),
    notes=("Planned backend: registry entry only; no reviewed adapter or acceptance evidence yet.",),
)

PYSPICE = BackendDescriptor(
    name="pyspice",
    display_name="PySpice (circuit frontend)",
    kind="library",
    status=OPTIONAL_LIBRARY,
    documentation="docs/34_pyspice_optional_backend_v0.md",
    install_recipes=(
        InstallRecipe(
            ANY,
            "pip",
            ('pip install "PySpice==1.5"',),
            notes=(
                "PySpice is GPLv3 and is not a core dependency.",
                "It is used for circuit construction only; execution uses ngspice.",
            ),
        ),
    ),
    notes=(
        "Library backend, not an executable.",
        "PySpice 1.5 native execution is not compatible with ngspice 41.",
    ),
)


_BACKENDS: dict[str, BackendDescriptor] = {
    descriptor.name: descriptor for descriptor in (NGSPICE, XYCE, LTSPICE, PYSPICE)
}


def list_backends() -> tuple[BackendDescriptor, ...]:
    return tuple(_BACKENDS.values())


def get_backend(name: str) -> BackendDescriptor:
    try:
        return _BACKENDS[str(name)]
    except KeyError as exc:
        raise KeyError(
            f"unknown backend {name!r}; known backends: {', '.join(sorted(_BACKENDS))}"
        ) from exc


__all__ = [
    "ANY",
    "BackendDescriptor",
    "InstallRecipe",
    "LINUX",
    "LTSPICE",
    "MACOS",
    "NGSPICE",
    "OPTIONAL_LIBRARY",
    "PLANNED",
    "PYSPICE",
    "SUPPORTED",
    "WINDOWS",
    "XYCE",
    "current_platform",
    "get_backend",
    "list_backends",
]

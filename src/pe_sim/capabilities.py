"""Versioned, read-only report of optional capabilities.

A capability report lets a packaged user discover which optional capabilities
exist, whether each one is usable in the current process, and what documented
action enables it.  It is deliberately small and side-effect free:

* it starts no simulator and no other external executable;
* it writes no file and creates no run directory or cache;
* it reads no environment variable to form a claim, except that the ngspice
  probe delegates to the reviewed resolver, which may consult
  ``PE_SIM_NGSPICE``;
* a broken probe yields ``unavailable`` for that capability instead of an
  exception.

The report is **not** run evidence, not a test result, and not a qualification
claim.  ``installed`` says the capability appears usable in this process now;
it does not say the capability passed acceptance or produced valid numbers.
"""

from __future__ import annotations

from dataclasses import dataclass
import importlib.util
import shutil
from typing import Any, Callable

from .backends.discovery import (
    BackendConfigError,
    BackendNotFoundError,
    resolve_ngspice_executable,
)


#: Schema tag for the capability document.
CAPABILITY_SCHEMA = "pe-sim.capabilities.v1"

#: Closed status vocabulary.
#:
#: ``installed``
#:     Usable in this process now, as far as a side-effect-free probe can tell.
#: ``available``
#:     Not present, but obtainable by the documented ``install_hint`` action.
#: ``unavailable``
#:     Present but not usable here (for example a malformed configuration or a
#:     failed probe), or not obtainable by the supported path.
CAPABILITY_STATUSES: tuple[str, ...] = ("installed", "available", "unavailable")

_REPORT_LIMITATIONS: tuple[str, ...] = (
    "this report describes capability availability in the current Python "
    "process only; it is not run evidence, not a test result, and not a "
    "qualification claim",
    "no simulator or submitted executable was started, no run directory was "
    "created, and no file was written by this report",
    "status values are probed at call time and can differ between machines, "
    "Python versions, and environments",
    "an installed capability has not necessarily passed acceptance; numerical "
    "and physical validity remain out of scope",
)


@dataclass(frozen=True)
class OptionalCapability:
    """One optional capability and the limits of its availability report.

    ``status`` must be one of :data:`CAPABILITY_STATUSES` and
    ``capability_id`` must be a non-empty string.  The object only describes
    availability; it grants no authority and carries no evidence.
    """

    capability_id: str
    description: str
    status: str
    provided_by: str
    install_hint: str
    limitations: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.capability_id, str) or not self.capability_id.strip():
            raise ValueError("capability_id must be a non-empty string")
        if self.status not in CAPABILITY_STATUSES:
            raise ValueError(
                f"unsupported capability status: {self.status!r}; "
                f"expected one of {list(CAPABILITY_STATUSES)}"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "capability_id": self.capability_id,
            "description": self.description,
            "status": self.status,
            "provided_by": self.provided_by,
            "install_hint": self.install_hint,
            "limitations": list(self.limitations),
        }


@dataclass(frozen=True)
class _CapabilityDefinition:
    """Static metadata shared by a capability and its failure fallback."""

    capability_id: str
    description: str
    provided_by: str
    install_hint: str
    limitations: tuple[str, ...]


_Probe = Callable[[], tuple[str, tuple[str, ...]]]


def _probe_pyspice() -> tuple[str, tuple[str, ...]]:
    """Locate PySpice without importing it or starting a solver."""

    spec = importlib.util.find_spec("PySpice")
    if spec is None:
        return "available", ("no PySpice module was located for this interpreter",)
    return "installed", ("a PySpice module location was found for this interpreter",)


def _probe_ngspice() -> tuple[str, tuple[str, ...]]:
    """Resolve the reviewed ngspice executable without executing it."""

    try:
        resolution = resolve_ngspice_executable()
    except BackendNotFoundError:
        return "available", (
            "no ngspice executable was resolved from PE_SIM_NGSPICE, the "
            "user-level configuration, or PATH",
        )
    except BackendConfigError as exc:
        return "unavailable", (
            f"the user-level backend configuration could not be read: {exc}",
        )
    return "installed", (
        f"an executable path was resolved via {resolution.source}; "
        "its version was not queried",
    )


def _probe_workbench() -> tuple[str, tuple[str, ...]]:
    """Report the packaged stdlib visualization service."""

    return "installed", ()


def _probe_attestation_anchor() -> tuple[str, tuple[str, ...]]:
    """Check for a Git executable without running it."""

    if shutil.which("git") is None:
        return "available", ("no git executable was found on PATH",)
    return "installed", ("a git executable was found on PATH",)


_PYSPICE = _CapabilityDefinition(
    capability_id="pyspice_netlist_builder",
    description=(
        "Build the reviewed Buck netlist with the PySpice object API; "
        "execution stays on the ngspice process boundary"
    ),
    provided_by="the separately installed PySpice package (GPLv3)",
    install_hint=(
        "install requirements-pyspice-tested.txt in a Python 3.10-3.12 "
        "environment, then run `python -m pip check`; on Python 3.13 use an "
        "isolated environment and run `pip check` there instead"
    ),
    limitations=(
        "PySpice is not a package extra by release policy (docs/28); it is a "
        "separate, manual installation",
        "the verified pin set in requirements-pyspice-tested.txt excludes "
        "Python 3.13",
        "the probe only locates the module; PySpice is not imported, no "
        "netlist is built, and no solver is started",
    ),
)

_NGSPICE = _CapabilityDefinition(
    capability_id="external_ngspice_backend",
    description=(
        "Run the reviewed switching Buck through a separately installed "
        "ngspice executable resolved by pe-sim"
    ),
    provided_by="an external ngspice executable resolved by pe_sim.backends.discovery",
    install_hint=(
        "run `pe-sim backend doctor --name ngspice` to test discovery, "
        "`pe-sim backend scan --name ngspice --root <directory>` to locate an "
        "existing installation, or set PE_SIM_NGSPICE to the console executable"
    ),
    limitations=(
        "the probe resolves a path only; no executable was started and no "
        "version was queried",
        "a resolved path does not prove a compatible version; "
        "`pe-sim backend doctor` performs the executable check",
        "ngspice is not bundled, linked, installed, or relicensed by pe-sim",
    ),
)

_WORKBENCH = _CapabilityDefinition(
    capability_id="workbench_visualization_service",
    description=(
        "Serve verified visualization contracts read-only from the local run "
        "store using a stdlib HTTP server"
    ),
    provided_by="pe_sim.visualization_service (ships with the package)",
    install_hint=(
        "no installation is required; run `pe-sim visualization-serve` "
        "against a verified run store"
    ),
    limitations=(
        "the service is local and read-only; this report does not start it",
        "serving a contract adds no numerical or physical claim",
    ),
)

_ATTESTATION = _CapabilityDefinition(
    capability_id="attestation_anchor",
    description=(
        "Anchor a detached attestation to a Git commit through the existing "
        "attestation anchors"
    ),
    provided_by="the Git executable on PATH plus pe_sim.attestation",
    install_hint=(
        "install Git and ensure `git` is on PATH; then run "
        "`pe-sim attest-run` without `--no-anchor`"
    ),
    limitations=(
        "only PATH presence is checked; no git command was executed and no "
        "repository state, commit identity, or signature was inspected",
    ),
)

_PROBES: tuple[tuple[_CapabilityDefinition, _Probe], ...] = (
    (_PYSPICE, _probe_pyspice),
    (_NGSPICE, _probe_ngspice),
    (_WORKBENCH, _probe_workbench),
    (_ATTESTATION, _probe_attestation_anchor),
)


def _evaluate(definition: _CapabilityDefinition, probe: _Probe) -> OptionalCapability:
    """Run one probe; any failure becomes ``unavailable`` with a limitation."""

    try:
        status, notes = probe()
        if status not in CAPABILITY_STATUSES:
            raise ValueError(f"probe returned unsupported status {status!r}")
    except Exception as exc:  # A broken probe must not break the report.
        status = "unavailable"
        notes = (
            f"the availability probe failed with {type(exc).__name__}: {exc}",
        )
    return OptionalCapability(
        capability_id=definition.capability_id,
        description=definition.description,
        status=status,
        provided_by=definition.provided_by,
        install_hint=definition.install_hint,
        limitations=definition.limitations + tuple(notes),
    )


def capability_report() -> dict[str, Any]:
    """Return the versioned capability document without side effects.

    The capability list is emitted in the fixed order declared by the module,
    so repeated calls with the same environment produce the same document.
    This function never writes, never starts an external process, and never
    raises because an optional dependency is absent.
    """

    return {
        "schema": CAPABILITY_SCHEMA,
        "capabilities": [
            _evaluate(definition, probe).to_dict()
            for definition, probe in _PROBES
        ],
        "limitations": list(_REPORT_LIMITATIONS),
    }


__all__ = [
    "CAPABILITY_SCHEMA",
    "CAPABILITY_STATUSES",
    "OptionalCapability",
    "capability_report",
]

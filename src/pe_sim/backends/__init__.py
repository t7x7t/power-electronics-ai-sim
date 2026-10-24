"""Reviewed external-simulator backends.

The package exposes a process-boundary ngspice adapter.  Importing it does not
start a simulator and does not require ngspice to be installed; execution is
explicit and every failure is returned as structured evidence.
"""

from .discovery import (
    CONFIG_ENV,
    CONFIG_SCHEMA_VERSION,
    NGSPICE_ENV,
    BackendConfigError,
    BackendNotFoundError,
    BackendResolution,
    config_template,
    load_backend_config,
    resolve_backend_executable,
    resolve_ngspice_executable,
    scan_for_backend,
    scan_for_ngspice,
    user_config_path,
    write_user_config,
)
from .registry import (
    BackendDescriptor,
    InstallRecipe,
    current_platform,
    get_backend,
    list_backends,
)
from .netlist import (
    NETLIST_TEMPLATE_VERSION,
    BuckNetlistParameters,
    NetlistParameterError,
    default_sample_step_s,
    netlist_digest,
    render_buck_netlist,
)
from .netlist_builder import (
    NativeBuckNetlistBuilder,
    NetlistBuilder,
    NetlistBuilderError,
    PyspiceBuckNetlistBuilder,
    PyspiceUnavailableError,
    pyspice_version,
)
from .ngspice_plants import NgspiceBuckPlant
from .process import (
    NgspiceExecutableAdapter,
    NgspiceProcess,
    NgspiceRunResult,
    executable_digest,
    parse_ngspice_version,
)
from .settings import NgspiceSolverSettings
from .whole_run_engine import (
    IntegrationOutcome,
    LinearStateSpace,
    WholeRunBuckPlant,
    integrate_rk4,
)
from .wrdata import TraceTable, WrdataParseError, parse_wrdata


__all__ = [
    "CONFIG_ENV",
    "CONFIG_SCHEMA_VERSION",
    "NGSPICE_ENV",
    "BackendConfigError",
    "BackendDescriptor",
    "BackendNotFoundError",
    "BackendResolution",
    "InstallRecipe",
    "NETLIST_TEMPLATE_VERSION",
    "BuckNetlistParameters",
    "NativeBuckNetlistBuilder",
    "NetlistBuilder",
    "NetlistBuilderError",
    "NetlistParameterError",
    "PyspiceBuckNetlistBuilder",
    "PyspiceUnavailableError",
    "NgspiceBuckPlant",
    "NgspiceExecutableAdapter",
    "NgspiceProcess",
    "NgspiceRunResult",
    "NgspiceSolverSettings",
    "IntegrationOutcome",
    "LinearStateSpace",
    "TraceTable",
    "WholeRunBuckPlant",
    "WrdataParseError",
    "default_sample_step_s",
    "config_template",
    "current_platform",
    "executable_digest",
    "get_backend",
    "integrate_rk4",
    "list_backends",
    "load_backend_config",
    "netlist_digest",
    "parse_ngspice_version",
    "parse_wrdata",
    "pyspice_version",
    "render_buck_netlist",
    "resolve_backend_executable",
    "resolve_ngspice_executable",
    "scan_for_backend",
    "scan_for_ngspice",
    "user_config_path",
    "write_user_config",
]

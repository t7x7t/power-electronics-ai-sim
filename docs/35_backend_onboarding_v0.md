# Backend Onboarding (v0)

**Status:** user-facing onboarding flow. It describes how a fresh clone moves
from "no simulator installed" to a verified simulation without adding binaries
or machine-local paths to the repository.

## Principle

The repository ships adapters and onboarding metadata only. A simulator is
always an external installation. The project never downloads, bundles, or
silently installs a simulator.

## Scenario A: no simulator installed

```powershell
pe-sim backend list
pe-sim backend setup --name ngspice
```

The second command detects that no executable is available and prints:

- the platform-specific installation recipes (conda, manual download, apt,
  brew as applicable);
- a copyable user-configuration template;
- the exact next command.

It does not install anything and does not write a configuration file.

Typical Windows flow:

```powershell
# option 1: conda-forge
conda create -n pe-sim-ngspice -c conda-forge python=3.12 ngspice
conda activate pe-sim-ngspice
python -m pip install -e .
pe-sim backend doctor --name ngspice

# option 2: official archive
# extract to e.g. D:\tools\ngspice-44.2, then:
pe-sim backend setup --name ngspice `
  --executable "D:\tools\ngspice-44.2\Spice64\bin\ngspice_con.exe" `
  --write-config
```

On Windows, always prefer `ngspice_con.exe` (console). The GUI `ngspice.exe`
may open a window and ignore batch output.

## Scenario B: simulator already installed

If the executable is on `PATH`, `pe-sim backend doctor --name ngspice` finds it
immediately. Otherwise use one of:

```powershell
# expected reference environment (PySpice 1.5 must be installed separately)
pip install "PySpice==1.5"

# explicit path for one command
pe-sim run examples/ngspice_buck_step/config.json --backend ngspice-buck --model-builder pyspice --ngspice <path>

# environment variable
$env:PE_SIM_NGSPICE = "<path>"

# persistent user-level configuration (recommended)
pe-sim backend setup --name ngspice --executable <path> --write-config
```

`--write-config` writes only to the user-level file outside the checkout:

- Windows: `%APPDATA%\pe-sim\backends.toml`
- Linux/macOS: `$XDG_CONFIG_HOME/pe-sim/backends.toml` or
  `~/.config/pe-sim/backends.toml`
- override: `PE_SIM_CONFIG_HOME`

The writer refuses to write inside the repository, so a local path cannot
accidentally become committed configuration.

After that, no path argument is needed:

```powershell
pe-sim backend doctor --name ngspice
pe-sim run examples/ngspice_buck_step/config.json --backend ngspice-buck --model-builder pyspice
```

## Resolution order

1. explicit CLI argument;
2. `PE_SIM_<BACKEND>` environment variable;
3. user-level `backends.toml`;
4. `PATH` (each declared executable name in order).

A missing executable is reported with every attempt and with installation
guidance. The runtime never falls back to a different simulator or to the Fake
backend.

## Backend registry

`pe-sim backend list` shows registered backends and their status:

| Backend | Kind | Status | Notes |
| --- | --- | --- | --- |
| `ngspice` | executable | `supported` | reviewed switching Buck adapter; console/GUI preference |
| `xyce` | executable | `planned` | descriptor only; no adapter or acceptance yet |
| `ltspice` | executable | `planned` | descriptor only; no adapter or acceptance yet |
| `pyspice` | library | `optional-library` | circuit frontend only; see `docs/34` |

Planned entries are intentionally visible. They show the extension path and
prevent a user from mistaking an unimplemented engine for a supported one.

## Evidence after onboarding

Every run records the resolved executable name, SHA-256, parsed version, solver
settings, netlist/output/log digests, and the resolution limitations. A local
absolute path is not written into portable run evidence.

`QUALIFIED` remains an automated per-run qualification state. It is not a
physical, hardware, or product approval.

## End-user acceptance

`scripts/user_acceptance.py` runs the full flow from a clean checkout snapshot:
installation in a fresh virtual environment, scenario A, scenario B, a real
switching Buck run, determinism, export, and the external consumer. Its scope
and local evidence are recorded in
[`36_user_acceptance_v0.md`](36_user_acceptance_v0.md).

## Adding another executable backend

1. Add a `BackendDescriptor` with executable names, version probe, minimum
   version, install recipes, and documentation link.
2. Implement a reviewed adapter that produces the same `SimulationResult`
   evidence shape.
3. Add fake-executable tests for success, timeout, non-zero exit, malformed
   output, and missing binary.
4. Add an opt-in real-executable acceptance suite.
5. Update the release support matrix; do not expand the physical claim.

The contract details are in `docs/33_backend_plugin_contract_v1.md`.

# Backend Plugin Contract (v1)

**Status:** architecture record for external simulator integration. It defines
how a simulator is discovered, executed, evidenced, and extended without adding
binaries, model libraries, or third-party source to this repository.

## Principle

The repository ships **adapter code and contracts only**. A simulator is an
external installation supplied by the user. The checkout must remain:

- free of simulator binaries, DLLs, installers, and model libraries;
- free of machine-local absolute paths;
- free of mandatory third-party runtime dependencies;
- runnable and testable without any simulator installed.

## Layers

```text
ExperimentSpec / PlantAdapter
        |
   CircuitPlant            (model semantics + window timing)
        |
   ModelBuilder            (deterministic netlist/circuit input)
        |
   EngineAdapter           (probe + solve + evidence)
        |
   ProcessBoundary         (subprocess, timeout, log, hashes)
        |
   External executable     (user-installed, outside the repository)
```

### 1. EngineAdapter

A reviewed engine adapter answers four questions:

- **What is installed?** `probe()` returns a portable provenance record:
  name, version, executable name, SHA-256, size, solver settings, and
  limitations.
- **What can it do?** `capabilities()` declares supported analyses
  (transient, DC, AC, sweep), units, and any backend-specific limits.
- **How does it run one request?** `solve(request) -> SimulationResult`
  executes exactly one zero-order-hold window or analysis.
- **What did it produce?** `SimulationResult` carries validated traces plus
  netlist/output/log digests, exit code, duration, and failure category.

### 2. ModelBuilder

A model builder produces deterministic input for a circuit family. It is
engine-neutral: a Buck builder can target ngspice, Xyce, or a future engine as
long as the engine accepts the same circuit description. Formatting must be
stable and free of timestamps, host names, and absolute paths so its digest is
a valid identity.

### 3. CircuitPlant

A Plant composes a model builder and an engine adapter, and implements the
public `PlantAdapter` lifecycle. It owns the window cache, snapshot/restore
semantics, action bounds, and the translation between engine signals and
`PlantObservation` measurements.

## Discovery and configuration

Resolution order:

1. explicit CLI argument (`--ngspice <path>`);
2. environment variable (`PE_SIM_NGSPICE`);
3. user-level configuration file;
4. `PATH`.

User-level configuration lives outside the checkout:

- Windows: `%APPDATA%\pe-sim\backends.toml`
- Linux/macOS: `$XDG_CONFIG_HOME/pe-sim/backends.toml` or
  `~/.config/pe-sim/backends.toml`
- override base directory: `PE_SIM_CONFIG_HOME`

`examples/backends.example.toml` is a template with placeholders only. The real
file is never committed. `pe-sim backend scan --root <dir>` locates existing
installations and prints a copyable template; it never writes the configuration
or the repository.

### Windows console variant

Official Windows builds may ship both `ngspice.exe` (GUI) and
`ngspice_con.exe` (console). Discovery automatically prefers the sibling
console binary and records why. A GUI-only installation is accepted with an
explicit limitation because its batch behavior is not reliable.

## Registration models

### Onboarding commands and the registry

`pe_sim.backends.registry` is a data-only registry of backend descriptors:
executable names, version probes, minimum versions, install recipes, and
documentation links.  `pe-sim backend list` shows supported and planned
backends; `pe-sim backend guide` prints platform-specific installation
recipes; `pe-sim backend setup` detects an existing installation and can write
the user-level configuration with an explicit `--write-config`.

No command downloads or installs a simulator automatically.  The registry makes
adding a new engine a descriptor entry plus a reviewed adapter, not a change to
the Runner or Plant contract.  See `docs/35_backend_onboarding_v0.md`.

### Built-in allow-list (current)

`pe_sim.backends.__init__` exposes reviewed adapters explicitly. Unknown engines
are rejected. This is the default for the repository.

### Optional entry points (future)

A separately installed plugin package may declare:

```toml
[project.entry-points."pe_sim.backends"]
xyce = "pe_sim_xyce.backend:factory"
```

The core discovers plugins with `importlib.metadata.entry_points(group="pe_sim.backends")`.
A plugin must:

- return a reviewed `EngineAdapter`;
- carry its own license and provenance record;
- never modify the Runner, Plant contract, or published packages;
- pass the conformance suite before admission.

Entry-point loading is not enabled by default in this slice. It requires an
explicit allow-list or user opt-in because it executes third-party code.

### Local plugin directory (deferred)

Loading arbitrary files from a local directory is deliberately not implemented.
It would turn a configuration path into an execution surface. If it is ever
added, it must be opt-in, signature- or hash-pinned, and documented as a
security boundary.

## Evidence rules

Every engine run records:

- engine name, version, executable name, SHA-256, size;
- explicit solver settings (never inferred);
- netlist/circuit digest, output digest, log digest;
- exit code, wall-clock duration, failure category;
- limitations, including GUI-variant and below-minimum-version notes.

A local absolute executable path is omitted from portable evidence. A missing
or unparseable version is `partial` or `unavailable`, never `known`.

## Adding a new engine

### Case study: PySpice

PySpice is a Python library, not an executable. It must run in the same Python
process as the adapter, so it cannot use the `ProcessBoundary` layer for its
own execution. The verified implementation therefore splits the two concerns:

- `PyspiceBuckNetlistBuilder` uses PySpice only to construct the circuit and
  render a netlist;
- the reviewed `NgspiceProcess` adapter executes that netlist.

PySpice's native shared-library and subprocess-server execution paths were
observed to be incompatible with the verified ngspice 41 installation. See
`docs/34_pyspice_optional_backend_v0.md`. A future native PySpice engine must
be a separate optional plugin and must pass its own compatibility gate before
it can be claimed.

1. Create `src/pe_sim/backends/<engine>/` with `engine.py`, `parser.py`, and an
   optional model builder.
2. Implement `probe()` and `solve()` against the frozen request/result shapes.
3. Add a discovery entry with the same precedence rules.
4. Add fake-executable tests for success, timeout, non-zero exit, malformed
   output, and missing binary.
5. Add an opt-in acceptance suite gated by an environment variable.
6. Record the supported surface and exclusions in a new scope document.
7. Update the release support matrix; do not expand the physical claim.

An engine that cannot satisfy these rules remains outside the release claim.

## Non-goals

- bundling simulators, model libraries, or installers;
- a universal SPICE netlist language or schematic editor;
- cross-engine numerical equivalence claims;
- automatic download or silent installation;
- executing unreviewed plugin code by default;
- using simulation output as hardware or product approval.

# Stage 11 Minimum Visualization Data Core (v1)

**Status:** active scope and acceptance boundary

## Why this phase is deliberately small

The project provides reusable simulation infrastructure, not a replacement for
MATLAB or a fixed graphical product.  Visualization is useful only when it is
a faithful consumer of recorded simulation facts.  A complete workbench,
layout system, project browser, or plugin marketplace would impose a large and
mostly non-general maintenance burden before those facts have a stable public
representation.

Stage 11 therefore starts with a minimum data core.  It gives users and AI
agents a stable way to build a custom frontend later, while preserving a small
reference implementation surface in the core project.

## The three read-only data planes

### `TopologyDescriptor`

`TopologyDescriptor` is the semantic topology graph.  It contains components,
declared ports, nets, concise parameter summaries, optional display-symbol
references, schema version, and a stable fingerprint.  Connectivity comes only
from the component-port-to-net declarations.  A drawing, layout, icon, SVG,
or generated image is never electrical truth.

### `TraceDataset`

`TraceDataset` carries sampled time-domain observations.  Signals must be
registered before publication and declare stable ID, display name, unit, type,
source, and optional metadata.  Samples are time-stamped, finite and
monotonic; the sampling policy records whether they are every-step,
fixed-interval, event-selected, or manual.  A trace is a read-only record, not
a mechanism for reading arbitrary Plant or Controller object attributes.

The default visibility boundary remains in force: Plant truth is not exposed
to a Controller or a data consumer merely because it exists.  A truth field
requires separate explicit registration and publishing by the responsible
adapter.

### `OutputDataset`

`OutputDataset` is the structured result plane for simulation-target outputs
that are not naturally time traces: scalar summaries, named sequences, tables,
sweep results, and standardized Stage 10 outputs.  Every field must have a
declared identifier, value shape/type, unit when applicable, source, and
versioned definition.  It is intended to replace repeated one-off Python plot
scripts with data that any suitable renderer can display as tables, values,
curves, or comparisons.

`OutputDataset` is implemented in `src/pe_sim/outputs.py`. Its explicit
publication, validation, provenance, and immutable-package rules complete the
minimum data core; it remains a renderer-neutral contract rather than a
plotting API.

## Provenance and immutable package boundary

When a dataset derives from a run, it must retain the available `run_id`,
Manifest hash, package hash, source commit, producer identity, and relevant
schema/data identity.  A consumer must distinguish missing or unverifiable
provenance from trusted run evidence.  Dataset creation and derived artifacts
must not mutate `runs/<run_id>` or recalculate the run package identity.

The current contracts are data definitions.  They do not authorize a sidecar
writer, a browser, a renderer, or a live stream to modify a published run.

## Extension boundary

New Plant, solver, Controller, or post-processing integrations should add a
small explicit exporter or publisher that produces one of the three contracts.
They must validate their data and declare visibility, units, provenance, and
schema compatibility.  The core must not acquire topology-specific branches,
reflection-based internal-state access, or a universal component library to
accommodate an extension.

AI assistance may create a candidate display-only symbol asset or a separate
consumer application.  Before use, a responsible user must bind every visual
terminal to a pre-existing declared port.  The asset cannot add a component,
change a parameter, add/remove a net, infer wiring, or become an input to the
simulation model.  A renderer may be wrong; the descriptor and run evidence
remain the reviewable sources.

## Current non-goals

The accepted minimum core and the optional fixture consumer intentionally do
not ship or support:

- a connected browser or desktop workbench backed by Runner/Plant state;
- REST/SSE/WebSocket delivery, live Runner updates, or report rendering;
- a general remote visualization API or live Runner UI integration;
- generic dynamic UI/plugin loading, a plugin marketplace, or arbitrary third
  party code execution;
- topology editing, netlist authoring, an icon catalog, automatic layout, or
  direct AI changes to circuit semantics.

The repository does include `workbench/`, a small read-only TypeScript/Vite
fixture consumer.  It loads only local JSON contract fixtures and demonstrates
the topology canvas, selectable multi-signal waveform view, and scalar/series/
table output view.  It is not a simulation authority and does not replace the
separate scope and acceptance review required for connected or editable
workbenches.

`pe_sim.workbench_export` is a separate, constrained offline fixture exporter.
It verifies the recorded publication state, artifact index, Manifest digest,
and package digest before projecting a reviewed project-owned L1 Buck or Boost
run into the three contracts outside `runs/<run_id>`. It does not start a
server, read a live Runner, edit a package, accept an arbitrary topology, or
turn exploratory evidence into formal comparison evidence.

`pe_sim.cli visualization-serve` is a separately scoped local v1 read-only
adapter documented in `docs/26_stage11_visualization_service_v1.md`. It serves
only verified, `PUBLISHED` reviewed Buck/Boost reference packages through the
same three contracts. It does not expose a Runner, provide writes, execute a
simulation, or expand this phase into a connected workbench.

## Minimum-core acceptance gate

The minimum core is complete when all of the following are locally verified:

1. Topology, Trace, and Output contracts have versioned serialization and
   parse/validation tests, including invalid graph, invalid field, invalid
   unit/type, non-finite value, non-monotonic time, and unknown-signal cases as
   applicable.
2. Publishers are explicit and fail closed for unregistered or inaccessible
   values.  The default Runner behavior and Controller measurement boundary are
   unchanged.
3. Buck and Boost reference adapters can provide the applicable semantic
   topology, trace, and structured outputs without changing their equations or
   published run package bytes.
4. Provenance is retained where a source run exists, and tampered, missing, or
   unknown provenance is visibly distinguishable from evidence that passed its
   relevant integrity check.
5. The focused tests, import/CLI smoke check, and `git diff --check` pass.

The repeatable local gate is:

```powershell
python -m pytest -q tests/test_topology_descriptor.py tests/test_traces.py tests/test_outputs.py tests/test_stage11_minimum_core.py
python -c "from pe_sim import TopologyDescriptor, TraceDataset, OutputDataset"
python -m compileall -q src tests
git diff --check
```

This gate has been satisfied by the local contract and integration tests. The
fixture workbench is a separately scoped consumer of these contracts and is
accepted only for local display demonstration; it remains outside the
simulation authority.

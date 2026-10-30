# Stage 11 Contracts (v1): Topology Descriptor, Trace, and Visualization Scope

**Merged from:** the three former `docs/24` Stage 11 records — topology
descriptor v1, trace and observation contract v1, and minimum visualization
data core/scope v1 — merged 2026-10-25. Content is preserved; only duplication
was removed.

**Status:** active contract for the Stage 11 data core, and the active scope
and acceptance boundary for this phase. The topology and trace source files
declared no separate status line: they are contract increments. The
visualization-scope source declared `Status: active scope and acceptance
boundary`. The sources do not disagree; this merged document keeps the scope
statement and additionally records the two contract slices as active
contracts.

## 目录

1. Why this phase is deliberately small
2. The three read-only data planes
3. Topology descriptor contract
4. Trace and observation contract
5. Provenance and immutable package boundary
6. Extension boundary
7. Current non-goals
8. Minimum-core acceptance gate
9. Local service boundary

## 1. Why this phase is deliberately small

The project provides reusable simulation infrastructure, not a replacement for
MATLAB or a fixed graphical product.  Visualization is useful only when it is
a faithful consumer of recorded simulation facts.  A complete workbench,
layout system, project browser, or plugin marketplace would impose a large and
mostly non-general maintenance burden before those facts have a stable public
representation.

Stage 11 therefore starts with a minimum data core.  It gives users and AI
agents a stable way to build a custom frontend later, while preserving a small
reference implementation surface in the core project.

## 2. The three read-only data planes

### 2.1 `TopologyDescriptor`

`TopologyDescriptor` is the semantic topology graph.  It contains components,
declared ports, nets, concise parameter summaries, optional display-symbol
references, schema version, and a stable fingerprint.  Connectivity comes only
from the component-port-to-net declarations.  A drawing, layout, icon, SVG,
or generated image is never electrical truth.

`pe_sim.topology` provides the first renderer-neutral topology contract for
visualization.  It is intentionally separate from Plant equations and from any
specific frontend.  A topology descriptor communicates components, terminals,
nets, parameter summaries, and a display-only `symbol_id` reference.

This allows a user or an AI-assisted workflow to introduce a reviewed custom
symbol asset without changing the connectivity or simulation model.  The
descriptor is the reviewable source for a renderer; an SVG, PNG, or UI layout
is never the source of electrical truth.

### 2.2 `TraceDataset`

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

### 2.3 `OutputDataset`

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
plotting API.  The full `OutputDataset` contract is recorded in
`docs/26_stage11_consumer_and_service_v1.md` (§1).

## 3. Topology descriptor contract

### 3.1 Contract

- Schema: `topology-descriptor-v1`.
- `TopologyDescriptor` contains a version, topology identifier, components,
  nets, optional metadata, and a SHA-256 fingerprint.
- Each component has stable IDs, typed ports, parameter summaries, optional
  model IDs, and an optional `symbol_id`.
- Every port must reference a declared net. Component and net IDs must be
  unique. Invalid graphs fail before a renderer receives them.
- The fingerprint identifies the complete descriptor payload and is checked on
  deserialization. It is identity
  evidence only, not a model-correctness, physical-performance, or hardware
  approval claim.

### 3.2 Reference exporters

`BuckPlant.topology_descriptor()` and `BoostPlant.topology_descriptor()`
export the project-owned ideal averaged test-fixture graphs. Their switch
symbols and parameters describe the averaged reference model; they do not
claim switching-device, layout, thermal, or product schematic fidelity.

`topology_to_dot()` supplies a dependency-free Graphviz DOT interchange view,
and `topology_to_adjacency()` supplies a small JSON-friendly graph for future
frontends. Neither renderer helper changes the descriptor or simulation.

### 3.3 Boundaries and next work

This slice intentionally does not define screen layout, an icon library,
netlist parsing, editable topology authoring, or a broad component catalog.
Before real LLC/Ngspice models are introduced, their adapters should export a
reviewed descriptor and record its fingerprint with the source run. The
trace-data contract that associates selectable waveform signals, units,
sampling policy, event markers, and run-package provenance with this graph is
recorded in §4 of this document.

## 4. Trace and observation contract

This increment defines the data foundation for waveform and live observation
views. It is deliberately independent of a GUI and does not change the
Runner's existing `samples.json` contract.

### 4.1 Scope

`pe_sim.traces` provides:

- `SignalDefinition` and `SignalRegistry`: explicit, stable signal IDs,
  display names, units, source labels, types, and metadata;
- `SamplingPolicy`: explicit `every_step`, `fixed_interval`, `event`, or
  `manual` collection. A point that is not selected is not silently replaced;
  `TraceCollector.dropped_count` records max-point drops;
- `TraceSample`: finite, monotonic, timestamped sparse observations;
- `TraceDataset`: versioned (`trace schema 1.0`) JSON-safe dataset;
- `TraceProvenance`: optional run, Manifest SHA-256, package SHA-256, source
  commit, and producer metadata;
- `TraceCollector.record_observation`: a narrow adapter for
  `PlantObservation`-like values. Truth fields are opt-in and must already be
  registered.

### 4.2 Source and visibility

The `source` field is descriptive (`measurement`, `truth`, `controller`,
`action`, or `runtime` are recommended values). Registration is not a blanket
permission grant: a caller explicitly selects which registered values are
published. The trace layer never uses reflection to expose arbitrary object
internals and never passes truth to a Controller.

### 4.3 Raw and decimated data

The collector records the chosen policy exactly. `every_step` is raw with
respect to the caller's publish calls. `fixed_interval` and `event` are
explicitly sampled views; no implicit downsampling occurs. A future backend
may publish both raw and decimated datasets, but they must be separate and
retain their policies and provenance.

### 4.4 Provenance and immutable packages

Trace data may later be emitted as a derived-data sidecar or consumed by a
visualization. Its
`TraceProvenance` must point back to the source run and its Manifest/Package
hashes. The collector does not edit `runs/<run_id>` or recalculate the
immutable package. A sidecar writer/reader and all rendering transport remain
future work. `TraceCollector.record_to_sink(..., sink)` defines only a generic
opt-in hand-off of an immutable validated dataset snapshot to an
`ObservationSink`; no bundled sink, Runner wiring, browser server, or failure
policy is part of the currently supported visualization surface. Default Runner
behavior is unchanged.

### 4.5 Runtime and backend limits

This is an observation contract, not a guarantee that an external Ngspice
process can stream every solver step. External backends may expose only
chunked output, progress events, or final waveforms. Mapping those outputs to
this contract must preserve timestamps, units, sampling policy, and backend
provenance.

### 4.6 Verification

```text
pytest -q tests/test_traces.py --basetemp .tmp/pytest-traces
python -m compileall -q src tests
git diff --check
```

### 4.7 Deferred consumers

This contract deliberately ships without a local viewer, HTTP API,
visualization CLI, renderer, or Runner live-stream integration.  A future
consumer may use a trace dataset only after the Stage 11 minimum data core is
accepted.  That consumer must preserve the timestamp, units, sampling policy,
visibility, and provenance described here, and must create derived artifacts
outside immutable run packages.  See §1 and §8 of this document for the current
scope and acceptance boundary.

## 5. Provenance and immutable package boundary

When a dataset derives from a run, it must retain the available `run_id`,
Manifest hash, package hash, source commit, producer identity, and relevant
schema/data identity.  A consumer must distinguish missing or unverifiable
provenance from trusted run evidence.  Dataset creation and derived artifacts
must not mutate `runs/<run_id>` or recalculate the run package identity.

The current contracts are data definitions.  They do not authorize a sidecar
writer, a browser, a renderer, or a live stream to modify a published run.

## 6. Extension boundary

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

## 7. Current non-goals

The accepted minimum core and the optional fixture consumer intentionally do
not ship or support:

- a connected browser or desktop workbench backed by Runner/Plant state;
- REST/SSE/WebSocket delivery, live Runner updates, or report rendering;
- a general remote visualization API or live Runner UI integration;
- a plugin marketplace or arbitrary third party code execution;
- topology editing, netlist authoring, an icon catalog, automatic layout, or
  direct AI changes to circuit semantics.

A local, read-only visualization host is **allowed** (decision D2, 2026-10-30,
recorded in `docs/70` section 12.6 and specified by
`docs/71_human_visualization_requirements_v1.md`).  It is a consumer, not a
capability: it may load operator-supplied display modules from a local
directory, but those modules receive no Runner, Plant, Controller, mutation or
authority beyond what any external consumer already has; the host fetches on
their behalf, issues GET only, and nothing writes `runs/`.  Loading display
code under those limits is therefore neither "generic dynamic UI/plugin
loading" nor "arbitrary third party code execution".

The repository previously included `workbench/`, a small read-only
TypeScript/Vite fixture consumer.  That browser surface was withdrawn on
2026-10-23 because its rendered content was not usable.  The renderer-neutral
contracts, the headless exporter, and the loopback read-only service remain;
they are data interfaces, not a UI claim.  Any browser or desktop workbench
beyond the local read-only host described above still requires its own scope
and acceptance review.

`pe_sim.workbench_export` is a separate, constrained offline fixture exporter.
It verifies the recorded publication state, artifact index, Manifest digest,
and package digest before projecting a reviewed project-owned run into the
three contracts outside `runs/<run_id>`.  The export decision is a registration
decision, not a topology name list: a registered plant may be projected when
its registration declares `export_eligible` and a `topology_exporter` and the
recorded plant class matches the registration; the shipped registrations
currently declare nine such exporters.  It does not start a server, read a live
Runner, edit a package, accept an arbitrary topology, or turn exploratory
evidence into formal comparison evidence.

`pe_sim.cli visualization-serve` is a separately scoped local v1 read-only
adapter documented in `docs/26_stage11_consumer_and_service_v1.md` (§3). It
serves only verified, `PUBLISHED` packages whose registered plant declares an
export-eligible `topology_exporter`; no topology name list is consulted. It
does not expose a Runner, provide writes, execute a simulation, or expand this
phase into a connected workbench.

## 8. Minimum-core acceptance gate

The minimum core is complete when all of the following are locally verified:

1. Topology, Trace, and Output contracts have versioned serialization and
   parse/validation tests, including invalid graph, invalid field, invalid
   unit/type, non-finite value, non-monotonic time, and unknown-signal cases as
   applicable.
2. Publishers are explicit and fail closed for unregistered or inaccessible
   values.  The default Runner behavior and Controller measurement boundary are
   unchanged.
3. The averaged Buck and Boost test fixtures can provide the applicable semantic
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
fixture workbench it was once demonstrated with is withdrawn (see §7), so this
repository currently has no first-party workbench consumer. Any future browser
or desktop workbench is a new, separately scoped consumer that still needs its
own scope and acceptance review and remains outside the simulation authority.

## 9. Local service boundary

The local read-only HTTP adapter is defined separately in
`docs/26_stage11_consumer_and_service_v1.md` (§3). It is intentionally smaller
than a general web API and serves only already-published reference runs whose
registered plant declares an export-eligible `topology_exporter`. The fixture
workbench this paragraph once described is withdrawn (§7); a future frontend is
a new, separately scoped consumer that still needs its own scope and acceptance
review, and would read those three contract routes rather than couple panels to
Runner or Plant internals.

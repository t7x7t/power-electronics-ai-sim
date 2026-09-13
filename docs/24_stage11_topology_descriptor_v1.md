# Stage 11 Topology Descriptor v1

## Purpose

`pe_sim.topology` provides the first renderer-neutral topology contract for
visualization. It is intentionally separate from Plant equations and from any
specific frontend. A topology descriptor communicates components, terminals,
nets, parameter summaries, and a display-only `symbol_id` reference.

This allows a user or an AI-assisted workflow to introduce a reviewed custom
symbol asset without changing the connectivity or simulation model. The
descriptor is the reviewable source for a renderer; an SVG, PNG, or UI layout
is never the source of electrical truth.

## Contract

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

## Reference exporters

`BuckPlant.topology_descriptor()` and `BoostPlant.topology_descriptor()`
export the project-owned ideal averaged L1 reference graphs. Their switch
symbols and parameters describe the averaged reference model; they do not
claim switching-device, layout, thermal, or product schematic fidelity.

`topology_to_dot()` supplies a dependency-free Graphviz DOT interchange view,
and `topology_to_adjacency()` supplies a small JSON-friendly graph for future
frontends. Neither renderer helper changes the descriptor or simulation.

## Boundaries and next work

This slice intentionally does not define screen layout, an icon library,
netlist parsing, editable topology authoring, or a broad component catalog.
Before real LLC/Ngspice models are introduced, their adapters should export a
reviewed descriptor and record its fingerprint with the source run. A later
trace-data contract will associate selectable waveform signals, units,
sampling policy, event markers, and run-package provenance with this graph.

# Stage 11 Trace and Observation Contract (v1)

This increment defines the data foundation for waveform and live observation
views. It is deliberately independent of a GUI and does not change the
Runner's existing `samples.json` contract.

## Scope

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

## Source and visibility

The `source` field is descriptive (`measurement`, `truth`, `controller`,
`action`, or `runtime` are recommended values). Registration is not a blanket
permission grant: a caller explicitly selects which registered values are
published. The trace layer never uses reflection to expose arbitrary object
internals and never passes truth to a Controller.

## Raw and decimated data

The collector records the chosen policy exactly. `every_step` is raw with
respect to the caller's publish calls. `fixed_interval` and `event` are
explicitly sampled views; no implicit downsampling occurs. A future backend
may publish both raw and decimated datasets, but they must be separate and
retain their policies and provenance.

## Provenance and immutable packages

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

## Runtime and backend limits

This is an observation contract, not a guarantee that an external Ngspice
process can stream every solver step. External backends may expose only
chunked output, progress events, or final waveforms. Mapping those outputs to
this contract must preserve timestamps, units, sampling policy, and backend
provenance.

## Verification

```text
pytest -q tests/test_traces.py --basetemp .tmp/pytest-traces
python -m compileall -q src tests
git diff --check
```

## Deferred consumers

This contract deliberately ships without a local viewer, HTTP API,
visualization CLI, renderer, or Runner live-stream integration.  A future
consumer may use a trace dataset only after the Stage 11 minimum data core is
accepted.  That consumer must preserve the timestamp, units, sampling policy,
visibility, and provenance described here, and must create derived artifacts
outside immutable run packages.  See
`docs/24_stage11_visualization_scope_v1.md` for the current scope and
acceptance boundary.

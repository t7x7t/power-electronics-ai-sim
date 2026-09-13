# Stage 11 OutputDataset Contract (v1)

**Status:** minimum-core implementation slice

`OutputDataset` is the third renderer-neutral data plane for Stage 11. It
represents values that a simulation user wants to inspect or compare but that
are not naturally a time trace: scalar summaries, time-indexed series, and
tabular sweep or metric results. It is deliberately a data contract, not a
plotting API or a physical interpretation layer.

## Contract

The implementation is in `src/pe_sim/outputs.py` and has these public types:

- `OutputFieldDefinition`: stable table-column ID, display name, unit, dtype,
  description, and JSON-safe metadata;
- `OutputDefinition`: stable output ID, display name, `scalar`/`series`/`table`
  kind, source, definition version, units and shape metadata;
- `OutputRegistry`: explicit registration boundary; duplicate IDs are rejected;
- `OutputCollector`: publishes each registered output at most once;
- `OutputDataset` and `OutputEntry`: immutable validated values with a stable
  SHA-256 fingerprint and JSON serialization.

Scalar and series outputs require a unit and a declared dtype (`float64`,
`int64`, `bool`, or `string`). A series payload is exactly:

```json
{"time_s": [0.0, 0.1], "values": [12.0, 12.1]}
```

Times must be finite, non-negative, and monotonic, and the two arrays must have
the same length. A table definition declares every column, including its unit
and dtype. A table payload is exactly `{"rows": [...]}`; every row must have
all and only the declared columns, with values matching their declared types.

All numeric values are finite. JSON `NaN`, `Infinity`, arbitrary objects, extra
fields, unknown output IDs, duplicate publications, malformed IDs, unsupported
schema versions, and mismatched fingerprints fail closed.

## Provenance and boundaries

`OutputDataset` reuses the acyclic `TraceProvenance` value object. It can retain
`run_id`, Manifest SHA-256, package SHA-256, source commit, producer, and
metadata without embedding a trace or creating a provenance cycle. Missing
provenance is allowed for exploratory/user-created data and remains visibly
empty. A producer must supply values explicitly; this module never reflects
over arbitrary Plant, Controller, Runner, or solver attributes.

The dataset is read-only evidence for renderers. Creating or serializing it
does not modify `runs/<run_id>` or any immutable package. It does not establish
physical performance, controller quality, hardware safety, or any other
engineering conclusion. A renderer may choose a table, number, line, scatter,
or comparison view, but it must preserve the output ID, definition version,
units, and provenance when exporting derived artifacts.

## Verification

Focused contract tests:

```powershell
python -m pytest -q tests/test_outputs.py --basetemp .tmp/pytest-stage11-output
```

The joint minimum-core gate is documented in
`docs/24_stage11_visualization_scope_v1.md` and adds the Topology, Trace, and
Buck reference integration tests.

The complete minimum-core gate combines these tests with the Trace and
Topology tests and reviewed Buck/Boost descriptor exporters. Stage 10 metrics
are not published automatically, and this contract does not add a sidecar
writer, stream data from the Runner, or provide a browser workbench.

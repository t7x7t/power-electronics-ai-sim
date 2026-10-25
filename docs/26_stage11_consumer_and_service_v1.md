# Stage 11 Consumer and Service (v1): OutputDataset, External Consumer, and Local Service

**Merged from:** the three former Stage 11 records — the OutputDataset contract
v1 (`docs/25`), the external consumer extension guide v1, and the local
visualization service v1 (both `docs/26`) — merged 2026-10-25. Content is
preserved; only duplication was removed. (The OutputDataset source was
numbered `25` in this repository, not `26` as in the merge map.)

**Status:** the sources use different, non-conflicting labels for different
slices, and the more conservative one is retained for the normative contract:
`minimum-core implementation slice` (`OutputDataset`), `minimum public
extension delivery` (external consumer guide), and no separate status line for
the optional local service. The service and external-consumer sections remain
separately scoped adapters and do not expand the minimum core or the release
claim.

## 目录

1. `OutputDataset` contract
2. Stable input contract for external consumers
3. Local read-only service
4. Minimal external consumer
5. Safety and ownership boundaries
6. Minimum verification

## 1. `OutputDataset` contract

`OutputDataset` is the third renderer-neutral data plane for Stage 11. It
represents values that a simulation user wants to inspect or compare but that
are not naturally a time trace: scalar summaries, time-indexed series, and
tabular sweep or metric results. It is deliberately a data contract, not a
plotting API or a physical interpretation layer.

### 1.1 Contract

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

### 1.2 Provenance and boundaries

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

### 1.3 Verification

Focused contract tests:

```powershell
python -m pytest -q tests/test_outputs.py --basetemp .tmp/pytest-stage11-output
```

The joint minimum-core gate is documented in
`docs/24_stage11_contracts_v1.md` (§8) and adds the Topology, Trace, and
Buck reference integration tests.

The complete minimum-core gate combines these tests with the Trace and
Topology tests and reviewed Buck/Boost descriptor exporters. Stage 10 metrics
are not published automatically, and this contract does not add a sidecar
writer, stream data from the Runner, or provide a browser workbench.

## 2. Stable input contract for external consumers

This section is for a user or AI Agent writing a separate visualization
frontend, report renderer, or display plugin.  The extension point is
deliberately data based: an external consumer reads three completed JSON
documents and never imports `Plant`, `Controller`, `Runner`, or a mutable run
package.

An extension consumes exactly these three versioned documents:

| Plane | Schema identifier | Required use |
| --- | --- | --- |
| topology | `schema: topology-descriptor-v1` | components, declared ports, nets, parameter summaries, and display-only `symbol_id` hints |
| trace | `schema_version: 1.0` | registered signals, units, sampling policy, and finite monotonic samples |
| outputs | `schema_version: 1.0` | versioned scalar, series, and table results |

The exact fields, validation rules, and fingerprint algorithms are specified in
`docs/24_stage11_contracts_v1.md` (§3 topology descriptor, §4 trace and
observation contract) and §1 of this document (outputs).  Consumers must reject
an unknown `schema_version`, retain unknown *metadata* only as display data,
and must not invent signals, wiring, units, or result values.

`TopologyDescriptor` is a reusable circuit description and has its own
`fingerprint`; it is not required to carry run provenance.  The Trace and
Output documents for one run must identify the same `provenance.run_id`.
When present, their `manifest_sha256`, `package_sha256`, and `source_commit`
should also agree.  A consumer may display incomplete provenance, but must
label it as unverified rather than silently treating it as formal evidence.
Fingerprints validate each document's published contents; they do not replace
verification of the source run package.

`v1` is additive only within an existing schema: a producer must not change a
required field's meaning or type.  A consumer built for these identifiers must
fail closed for another version.  A breaking contract change receives a new
schema/version and needs an explicit adapter.  The public Python validators
remain useful at a trusted integration boundary, but an external browser or
agent is intentionally not required to import this project.

## 3. Local read-only service

The optional local service has no simulation control endpoints.  Start it only
against a runs directory you control. The default bind address is loopback.
Use a caller-owned reverse proxy or a separate authenticated deployment if a
non-local consumer is required.

### 3.1 CLI

```powershell
python -m pe_sim.cli visualization-serve --runs-root runs --host 127.0.0.1 --port 8765
```

### 3.2 Boundary

The service never starts a simulation, accesses a live `Runner`, modifies a
run package, or loads arbitrary plugins. Every request is scoped to the
configured `runs` root. A run is served only when its `manifest.json` and
`.run-state.json` identify a published run, its artifact index matches the
directory, and both Manifest and package SHA-256 digests recompute correctly.
Only reviewed `buck` and `boost` `ideal_averaged_reference` identities are
currently exportable.

### 3.3 Versioned routes

All responses are JSON. Errors use:

```json
{"schema_version":"pe-sim.visualization-service.v1","error":{"code":"...","message":"..."}}
```

Its stable GET routes are:

```text
GET /health
GET /v1/discovery
GET /v1/runs/{run_id}/topology
GET /v1/runs/{run_id}/trace
GET /v1/runs/{run_id}/outputs
```

- `GET /health` returns service status and `read_only: true`.
- `GET /v1/discovery` returns the contract schema IDs and the list of runs
  that pass the publication/integrity gate.
- `GET /v1/runs/{run_id}/topology` returns `TopologyDescriptor` v1.
- `GET /v1/runs/{run_id}/trace` returns `TraceDataset` v1.
- `GET /v1/runs/{run_id}/outputs` returns `OutputDataset` v1.

`/v1/discovery` lists only verified, displayable reference runs.  Each plane
route verifies the run package before returning a contract.

Discovery reports the public contract identifiers exactly as serialized:
`topology-descriptor-v1`, trace `1.0`, and outputs `1.0`. A discovered
`run_id` is always both the verified Manifest identity and its routeable
directory identifier; packages where those identities differ are not served.

`run_id` is a single safe path component. Parent traversal, separators,
symlinks, unknown routes, and non-GET methods are rejected. The service sends
`Cache-Control: no-store` because the source package is revalidated per read.
For browser consumers, CORS is deliberately restricted: an `Origin` is echoed
only for `http` or `https` loopback hosts (`localhost`, `127.0.0.1`, or `[::1]`)
with no credentials. Such origins receive `Access-Control-Allow-Origin` and
`Vary: Origin`; other origins receive no CORS permission. `OPTIONS` preflight
is supported only for `GET` and returns `204`; write-method preflights are
rejected.

The service is loopback by default, rejects path traversal, has no credentials,
and rejects write methods.  It is a local data source, not a remote multi-user
API; do not expose it on an untrusted network without a separately reviewed
deployment boundary and authentication design.

### 3.4 Verification

```powershell
python -m pytest -q tests/test_visualization_service.py
python -m pe_sim.cli visualization-serve --help
```

The workbench remains a separate fixture consumer. It can later replace its
fixture loader with these three contract routes without coupling panels to
Runner or Plant internals.

## 4. Minimal external consumer

The browser workbench that previously shipped under `workbench/` is
temporarily withdrawn. The three contracts, the headless exporter, the local
read-only service, and the external consumer example remain. The example is
intentionally independent from `src/pe_sim/`:

- `index.html` is a dependency-free browser display plugin.  It loads either
  the three derived JSON files or the three local service routes and shows the
  provenance, topology inventory, latest registered values, and scalar
  outputs.
- `verify_contracts.py` uses only the Python standard library.  It reads the
  same files or URLs, checks the schema identities and shared provenance, and
  prints a compact display summary suitable for an AI Agent or CI wrapper.

For derived files, first produce data outside the immutable run package:

```powershell
pe-sim export-workbench runs/ngspice-buck-reference .tmp/visualization-demo
python examples/visualization_consumer/verify_contracts.py --directory .tmp/visualization-demo
```

For the local service, use:

```powershell
python examples/visualization_consumer/verify_contracts.py --service http://127.0.0.1:8765 --run ngspice-buck-reference
```

Open `examples/visualization_consumer/index.html` through a small static file
server, for example `python -m http.server 8000 --directory examples/visualization_consumer`.
Give it `?directory=../../.tmp/visualization-demo` for files, or
`?service=http://127.0.0.1:8765&run=ngspice-buck-reference` for the
local service.  Service mode needs normal browser CORS permission if its origin
differs from the static server.

## 5. Safety and ownership boundaries

An extension may choose its own HTML, plotting library, layout, or an
AI-generated display-only symbol asset.  It may not use `symbol_id` or visual
placement to alter component identity, ports, nets, parameter values, or
simulation behavior.  It must not write into `runs/<run_id>`, start a Runner,
reflect over internal objects, or interpret display data as hardware approval.

There is deliberately no browser-side arbitrary plugin execution mechanism.
The safe extension model is a separately installed consumer that receives only
the three contracts.  This lets users and AI Agents evolve their own frontend
without granting it simulation authority or creating a project-maintained
plugin runtime.

## 6. Minimum verification

```powershell
python -m pytest -q tests/test_visualization_service.py tests/test_workbench_export.py
pe-sim export-workbench runs/ngspice-buck-reference .tmp/visualization-demo
python examples/visualization_consumer/verify_contracts.py --directory .tmp/visualization-demo
```

The automated checks and the example confirm that the headless exporter/service
produce the three published v1 JSON documents, the example runs without
importing `pe_sim`, prints provenance, and rejects a mismatched run identity.

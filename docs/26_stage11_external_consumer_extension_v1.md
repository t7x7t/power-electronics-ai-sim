# Stage 11 External Consumer Extension Guide (v1)

**Status:** minimum public extension delivery

This guide is for a user or AI Agent writing a separate visualization frontend,
report renderer, or display plugin.  The extension point is deliberately data
based: an external consumer reads three completed JSON documents and never
imports `Plant`, `Controller`, `Runner`, or a mutable run package.

## Stable input contract

An extension consumes exactly these three versioned documents:

| Plane | Schema identifier | Required use |
| --- | --- | --- |
| topology | `schema: topology-descriptor-v1` | components, declared ports, nets, parameter summaries, and display-only `symbol_id` hints |
| trace | `schema_version: 1.0` | registered signals, units, sampling policy, and finite monotonic samples |
| outputs | `schema_version: 1.0` | versioned scalar, series, and table results |

The exact fields, validation rules, and fingerprint algorithms are specified in
`docs/24_stage11_topology_descriptor_v1.md`,
`docs/24_stage11_trace_contract_v1.md`, and
`docs/25_stage11_output_contract_v1.md`.  Consumers must reject an unknown
`schema_version`, retain unknown *metadata* only as display data, and must not
invent signals, wiring, units, or result values.

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

## Local read-only service

The optional local service has no simulation control endpoints.  Start it only
against a runs directory you control:

```powershell
python -m pe_sim.cli visualization-serve --runs-root runs --host 127.0.0.1 --port 8765
```

Its stable GET routes are:

```text
GET /health
GET /v1/discovery
GET /v1/runs/{run_id}/topology
GET /v1/runs/{run_id}/trace
GET /v1/runs/{run_id}/outputs
```

`/v1/discovery` lists only verified, displayable reference runs.  Each plane
route verifies the run package before returning a contract.  The service is
loopback by default, rejects path traversal, has no credentials, and rejects
write methods.  It is a local data source, not a remote multi-user API; do not
expose it on an untrusted network without a separately reviewed deployment
boundary and authentication design.

## Minimal external consumer

`examples/visualization_consumer/` is intentionally independent from both
`workbench/` and `src/pe_sim/`:

- `index.html` is a dependency-free browser display plugin.  It loads either
  the three derived JSON files or the three local service routes and shows the
  provenance, topology inventory, latest registered values, and scalar
  outputs.
- `verify_contracts.py` uses only the Python standard library.  It reads the
  same files or URLs, checks the schema identities and shared provenance, and
  prints a compact display summary suitable for an AI Agent or CI wrapper.

For derived files, first produce data outside the immutable run package:

```powershell
python -m pe_sim.workbench_export runs/workbench-buck-demo-20260913 .tmp/visualization-demo
python examples/visualization_consumer/verify_contracts.py --directory .tmp/visualization-demo
```

For the local service, use:

```powershell
python examples/visualization_consumer/verify_contracts.py --service http://127.0.0.1:8765 --run workbench-buck-demo-20260913
```

Open `examples/visualization_consumer/index.html` through a small static file
server, for example `python -m http.server 8000 --directory examples/visualization_consumer`.
Give it `?directory=../../.tmp/visualization-demo` for files, or
`?service=http://127.0.0.1:8765&run=workbench-buck-demo-20260913` for the
local service.  Service mode needs normal browser CORS permission if its origin
differs from the static server.

## Safety and ownership boundaries

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

## Minimum verification

```powershell
python -m pytest -q tests/test_external_visualization_consumer.py
python examples/visualization_consumer/verify_contracts.py --directory workbench/public/fixtures/run-demo
```

The automated check confirms the example runs without importing `pe_sim`,
accepts the three published v1 JSON documents, prints provenance, and rejects
a mismatched run identity.

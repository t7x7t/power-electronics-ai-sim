# Stage 11 Local Visualization Service (v1)

This document defines the optional, local, read-only HTTP adapter for the
Stage 11 visualization contracts. It is intentionally smaller than a general
web API: the service exposes already-published Buck/Boost reference runs for a
custom frontend or AI-agent-built consumer.

## Boundary

The service never starts a simulation, accesses a live `Runner`, modifies a
run package, or loads arbitrary plugins. Every request is scoped to the
configured `runs` root. A run is served only when its `manifest.json` and
`.run-state.json` identify a published run, its artifact index matches the
directory, and both Manifest and package SHA-256 digests recompute correctly.
Only reviewed `buck` and `boost` `ideal_averaged_reference` identities are
currently exportable.

## CLI

```powershell
python -m pe_sim.cli visualization-serve --runs-root runs --host 127.0.0.1 --port 8765
```

The default bind address is loopback. Use a caller-owned reverse proxy or a
separate authenticated deployment if a non-local consumer is required.

## Versioned routes

All responses are JSON. Errors use:

```json
{"schema_version":"pe-sim.visualization-service.v1","error":{"code":"...","message":"..."}}
```

- `GET /health` returns service status and `read_only: true`.
- `GET /v1/discovery` returns the contract schema IDs and the list of runs
  that pass the publication/integrity gate.
- `GET /v1/runs/{run_id}/topology` returns `TopologyDescriptor` v1.
- `GET /v1/runs/{run_id}/trace` returns `TraceDataset` v1.
- `GET /v1/runs/{run_id}/outputs` returns `OutputDataset` v1.

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

## Verification

```powershell
python -m pytest -q tests/test_visualization_service.py
python -m pe_sim.cli visualization-serve --help
```

The workbench remains a separate fixture consumer. It can later replace its
fixture loader with these three contract routes without coupling panels to
Runner or Plant internals.

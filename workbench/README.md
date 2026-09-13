# PE Sim Minimum Workbench

This is a deliberately small, read-only browser consumer for the Stage 11
renderer-neutral contracts. It loads three JSON fixtures and renders:

- a semantic topology descriptor as a deterministic SVG;
- selectable multi-signal traces with registry units, zoom, overlay, and a
  click cursor;
- scalar, series, and table outputs.

The workbench does not import Python, inspect a `Runner`, edit a topology, or
stream live simulation state. It can read public JSON fixtures or an explicit
local read-only service, while leaving the panels unchanged. The descriptor
remains electrical truth; `symbol_id` is only a display hint.

## Contract Boundary

The only supported inputs are the versioned `TopologyDescriptor`,
`TraceDataset`, and `OutputDataset` JSON contracts described in
`docs/24_stage11_*.md` and `docs/25_stage11_output_contract_v1.md`.
The fixture adapter is intentionally the only integration point in this
reference application. The panels neither obtain nor infer simulation data
from a Plant, Controller, Runner, Manifest, or mutable run directory.

This is a reference consumer, not a second simulation authority. It preserves
the units and provenance that are supplied by the contracts but does not
validate Python-side package evidence. Data intended as formal evidence must
therefore be validated before export through the established simulation and
run-package workflow.

## Verified Run Demo

`fixtures/run-demo/` is a derived, read-only projection of the checked local
Buck run `runs/workbench-buck-demo-20260913/`. It is not part of that immutable
run package. The exporter recomputes the recorded Manifest and package hashes,
requires the `PUBLISHED` state marker, then exports only a reviewed Buck/Boost
topology descriptor, registered trace signals, and structured outputs. It
preserves the run ID, Manifest/package hashes, source commit, run mode, and
working-tree state in contract provenance. An exploratory or dirty run can be
shown, but the header identifies it as such; display is not formal comparison
evidence.

Regenerate the derived demo after intentionally rerunning its source package:

```powershell
python -m pe_sim.workbench_export runs/workbench-buck-demo-20260913 workbench/public/fixtures/run-demo --overwrite
```

The default URL displays this verified derived example. Add
`?fixture=reference` to view the original static demonstration fixture instead.
Neither selection causes the browser to read `runs/` or a Python object.

## Optional Local Service

An external local service may supply the same completed contracts. This is an
optional browser adapter, not a Python service implementation: the workbench
does not start, configure, or control a service. Its only allowed requests are
the following `GET` endpoints:

```text
/v1/runs/{run_id}/topology
/v1/runs/{run_id}/trace
/v1/runs/{run_id}/outputs
```

Select it explicitly with a URL such as:

```text
http://127.0.0.1:5173/?source=service&service=http://127.0.0.1:8765&run=workbench-buck-demo-20260913
```

For safety, `service` must be a credential-free loopback `http` or `https`
origin (`localhost`, `127.0.0.1`, or `[::1]`), with no path, query, or
fragment. `run` accepts only a bounded identifier made of letters, digits,
periods, underscores, and hyphens. Invalid parameters and failed requests are
shown in the page error banner. Requests omit browser credentials and the UI
has no endpoint or permission to run simulations, access Plants, Controllers,
Runners, Manifests, or mutable packages. A service must return CORS headers
permitting the workbench origin.

Without `source=service`, the default derived demo remains active; use
`?fixture=reference` for the original static fixture.

## Run

```powershell
cd workbench
npm ci
npm run dev
```

Open the URL printed by Vite. For a production build:

```powershell
npm run build
npm run preview
```

## Acceptance Check

```powershell
cd workbench
npm run build
```

Then run `npm run dev` and verify the Buck fixture displays all of the
following:

1. a read-only SVG topology with components, ports, nets, and parameter
   summaries;
2. selectable registered traces with their declared units, zoom controls, and
   a click cursor showing the timestamp and selected values;
3. scalar, series, and table outputs from `outputs.json`.

The initial implementation uses platform Canvas and SVG to keep dependencies
and the extension surface small. It currently renders completed data contracts
only; no live stream, topology editing, arbitrary plugin loading, symbol
marketplace, SSE adapter, or backend validation is included.

## Toolchain policy

Use `npm ci`, never `npm install`, for a checked-out revision: it installs the
exact dependency graph recorded in `package-lock.json` and fails when that
lockfile and `package.json` disagree. The declared compatibility range and the
exact locally verified toolchain are deliberately different; see
[`docs/28_release_toolchain_policy_v0.md`](../docs/28_release_toolchain_policy_v0.md).

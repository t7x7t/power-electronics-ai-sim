# External Visualization Consumer Example

This example is a safe starting point for a user or AI Agent writing a custom
visualization frontend.  It has no dependency on `workbench/`, `pe_sim`, or
simulation internals.  It reads only `topology.json`, `trace.json`, and
`outputs.json` and displays their common provenance and simple values.

Validate three local derived contracts with the standard-library script:

```powershell
python verify_contracts.py --directory ../../workbench/public/fixtures/run-demo
```

Or read an explicitly started local visualization service:

```powershell
python verify_contracts.py --service http://127.0.0.1:8765 --run workbench-buck-demo-20260913
```

For the browser example, serve this directory rather than opening the file
directly:

```powershell
python -m http.server 8000
```

Open `http://127.0.0.1:8000/?directory=../../workbench/public/fixtures/run-demo`.
Service mode is
`?service=http://127.0.0.1:8765&run=workbench-buck-demo-20260913` and requires
the service to permit that browser origin through CORS.

The example deliberately does not run simulations, write run packages, load
third-party code, or treat drawings as circuit truth.  See
`docs/26_stage11_external_consumer_extension_v1.md` for compatibility and
safety rules.

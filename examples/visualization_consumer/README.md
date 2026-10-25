# External Visualization Consumer Example

This example is a safe starting point for a user or AI Agent writing a custom
visualization frontend.  It has no dependency on `pe_sim` or simulation
internals.  It reads only `topology.json`, `trace.json`, and `outputs.json` and
displays their common provenance and simple values.

The browser workbench is temporarily withdrawn. First export the three
contracts from a published run, then validate them with the standard-library
script:

```powershell
pe-sim export-workbench ../../runs/ngspice-buck-reference ../../.tmp/visualization-demo
python verify_contracts.py --directory ../../.tmp/visualization-demo
```

Or read an explicitly started local visualization service:

```powershell
python verify_contracts.py --service http://127.0.0.1:8765 --run ngspice-buck-reference
```

For the browser example, serve this directory rather than opening the file
directly:

```powershell
python -m http.server 8000
```

Open `http://127.0.0.1:8000/?directory=../../.tmp/visualization-demo`.
Service mode is
`?service=http://127.0.0.1:8765&run=ngspice-buck-reference` and requires the
service to permit that browser origin through CORS.

The example deliberately does not run simulations, write run packages, load
third-party code, or treat drawings as circuit truth.  See
`docs/26_stage11_consumer_and_service_v1.md` (§2 and §5) for compatibility and
safety rules.

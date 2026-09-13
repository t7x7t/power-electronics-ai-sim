# Stage 11 Local Browser Acceptance (v0)

## Purpose

`tests/test_workbench_browser_e2e.py` is the browser-level acceptance check
for the source-distributed minimum workbench. It complements contract,
service, and TypeScript build checks: those checks prove data shape and HTTP
responses, while this one proves that a real browser renders the three public
contracts and that the essential waveform interactions work.

It is deliberately a local release-check dependency, not a Python package
runtime dependency and not a workbench npm dependency. The test uses the
Python `playwright` package and its locally installed Chromium browser.

## What the test proves

With a supplied local service it opens the workbench in service mode and
asserts all of the following:

- the selected loopback service run reaches the ready state without a visible
  load error;
- the topology contract produces an SVG topology panel;
- the trace contract produces a visible Canvas waveform panel;
- the output contract produces at least one structured output card;
- a registered signal can be toggled, zoom changes to 150%, and clicking the
  waveform produces a timestamped cursor readout.

The suite starts Vite from `PE_SIM_WORKBENCH_DIR` and waits for its loopback
HTTP endpoint. The release gate passes an isolated `npm ci` workbench copy, so
no test operation changes `workbench/node_modules` in the source checkout. It
starts no simulation and it cannot write run packages.

This is UI evidence only. It does not validate circuit physics, browser
appearance across all platforms, live streaming, topology editing, or remote
deployment.

## Local opt-in execution

First install the lockfile-defined workbench dependencies and Python
Playwright with Chromium in the local test environment. These commands are
deliberately prerequisites, not source changes:

```powershell
cd workbench
npm ci
cd ..
python -m pip install playwright
python -m playwright install chromium
$env:PE_SIM_RUN_BROWSER_E2E = "1"
python -m pytest -q tests/test_workbench_browser_e2e.py
```

Without `PE_SIM_RUN_BROWSER_E2E=1`, pytest reports the test as skipped. That
keeps ordinary development and source users free to choose whether they need a
browser check. When explicitly enabled, missing Playwright, Chromium,
workbench dependencies, Vite readiness, or an interaction assertion is a test
failure.

For service mode, start the project read-only local service separately and
provide both variables below. The service address must match the workbench's
loopback-only policy.

```powershell
$env:PE_SIM_BROWSER_SERVICE_URL = "http://127.0.0.1:8765"
$env:PE_SIM_BROWSER_RUN_ID = "buck-contract-fixture"
python -m pytest -q tests/test_workbench_browser_e2e.py
```

The authoritative release path is `scripts/release_check.ps1`. It injects
these values only after it has started and health-checked its own read-only
loopback service, records the browser test log in the evidence package, and
stops only processes it created.

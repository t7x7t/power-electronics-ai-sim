# Optional Capabilities v1

**Status:** Phase C item 1 deliverable, limited to discovery. The project still
declares **no install-time extras**; `docs/28_release_toolchain_policy_v0.md`
keeps PySpice out of dependencies and extras, and
`docs/34_pyspice_optional_backend_v0.md` records why. This document defines a
versioned, machine-readable report so a packaged user can discover optional
capabilities, see whether each one is usable here, and learn the documented
action that enables it without reading source.

## 1. What a capability report is

`pe_sim.capabilities.capability_report()` returns one JSON-serializable
document tagged `pe-sim.capabilities.v1`. `pe-sim capabilities` prints the same
document. The report is **read-only**:

- it starts no simulator and no other external executable;
- it creates no run directory, cache, or file;
- it reads no environment variable to form a claim, except that the ngspice
  probe delegates to the reviewed discovery resolver, which may consult
  `PE_SIM_NGSPICE`;
- a probe that raises becomes `unavailable` for that capability; the report
  does not propagate the exception.

The capability order is fixed, so repeated calls in one environment produce the
same document. Abridged example (one entry shown):

```json
{
  "schema": "pe-sim.capabilities.v1",
  "capabilities": [
    {
      "capability_id": "workbench_visualization_service",
      "description": "Serve verified visualization contracts read-only from the local run store using a stdlib HTTP server",
      "status": "installed",
      "provided_by": "pe_sim.visualization_service (ships with the package)",
      "install_hint": "no installation is required; run `pe-sim visualization-serve` against a verified run store",
      "limitations": [
        "the service is local and read-only; this report does not start it",
        "serving a contract adds no numerical or physical claim"
      ]
    }
  ],
  "limitations": [
    "this report describes capability availability in the current Python process only; it is not run evidence, not a test result, and not a qualification claim"
  ]
}
```

## 2. The three statuses

| Status | Meaning |
| --- | --- |
| `installed` | Usable in this process now, as far as a side-effect-free probe can tell. |
| `available` | Not present, but obtainable by the documented `install_hint` action. |
| `unavailable` | Present but not usable here (for example a malformed configuration or a failed probe), or not obtainable by the supported path. |

`installed` is an availability statement, not an acceptance result. See §6.

## 3. The reported capabilities

| Capability id | Provided by | The probe does |
| --- | --- | --- |
| `pyspice_netlist_builder` | A separately installed PySpice package (GPLv3) | `importlib.util.find_spec("PySpice")`; it does not import PySpice. |
| `external_ngspice_backend` | A separately installed ngspice executable | Calls the reviewed resolver in `pe_sim.backends.discovery`; it does not scan directories or start the executable. |
| `workbench_visualization_service` | `pe_sim.visualization_service` (stdlib) | Reports the packaged service; it does not start a server. |
| `attestation_anchor` | `git` on `PATH` plus `pe_sim.attestation` | Checks `shutil.which("git")`; it does not run Git. |

### `pyspice_netlist_builder`

`installed` means a PySpice module location exists for this interpreter. The
report does not import PySpice, build a netlist, or start a solver. `available`
means no module was located. The `install_hint` points at
`requirements-pyspice-tested.txt`; the limitations state that PySpice is not a
package extra by release policy and that the verified pin set excludes Python
3.13.

To enable it:

```powershell
# Python 3.10-3.12: the verified pin set
pip install -r requirements-pyspice-tested.txt
python -m pip check

# Python 3.13: use an isolated environment and let pip resolve the dependency
# set, then run `python -m pip check` there.  There is no verified 3.13 lock.
```

Then select the builder explicitly:

```powershell
pe-sim run examples/ngspice_buck_step/config.json --backend ngspice-buck --model-builder pyspice
```

Execution still uses the external ngspice process; native PySpice execution is
not claimed (`docs/34`).

### `external_ngspice_backend`

`installed` means the reviewed resolver returned a path. The probe does not
run the executable, so `installed` does **not** prove a compatible version;
`pe-sim backend doctor` performs that check. `available` means no path was
resolved from the documented order (explicit argument, `PE_SIM_NGSPICE`,
user-level configuration, `PATH`). `unavailable` means discovery itself cannot
be used here, for example because the user-level configuration is malformed.

To enable it:

```powershell
pe-sim backend doctor --name ngspice
pe-sim backend scan --name ngspice --root D:\Applications
# or point at the console executable directly:
$env:PE_SIM_NGSPICE = "D:\path\to\ngspice_con.exe"
```

ngspice is not bundled, installed, linked, or relicensed by this project.

### `workbench_visualization_service`

This capability is always `installed`: the read-only stdlib HTTP service ships
with the package and has no separate dependency. The report does not start it.
To use it:

```powershell
pe-sim visualization-serve --runs-root <verified-run-store>
```

### `attestation_anchor`

`installed` means a `git` executable is on `PATH`. The probe does not run Git
and does not inspect repository state, commit identity, or signatures.
`available` means Git was not found. To enable it, install Git and ensure
`git` is on `PATH`, then write an anchored attestation:

```powershell
pe-sim attest-run <run-dir>
```

`--no-anchor` remains available when Git is absent, but then the attestation is
not anchored and the capability should not be expected to report `installed`.

## 4. Why there is still no PySpice extra

Adding a `pyspice` extra would contradict the existing release policy
(`docs/28`) and the licensing rationale in `docs/34`: PySpice is GPLv3, the
core package is MIT, and `environment-check` treats declared extras as expected
packages. The verified pin set also keeps `numpy==1.26.4`, which has no Python
3.13 wheel. Resolving this needs a separate release-policy decision, for
example a separate wheel or requirements handoff. The capability report does
not change `pyproject.toml`, does not add an extra, and does not alter
`environment-check` behaviour or its document schema.

## 5. CLI exit codes

```powershell
pe-sim capabilities [--require CAPABILITY_ID ...] [--compact]
```

| Exit | When | Output |
| --- | --- | --- |
| `0` | No `--require`, or every required capability is `installed` | The capability document |
| `1` | At least one required capability is not `installed` | The capability document is still printed so the failing status is visible |
| `2` | A required id is unknown | The existing `pe-sim.rejection.v1` envelope with `error_code: "invalid_request"`; the capability document is not printed |

`--compact` emits single-line JSON. Repeating `--require` checks each named
capability. Unknown ids are checked before the document is printed, so a
client always gets exactly one JSON document per invocation.

## 6. What the report does not claim

- It is **not evidence**, not a test result, and not a qualification verdict.
- It does **not** start a simulator, server, or Git command, and it does not
  write a run package or any other file.
- `installed` does **not** mean the capability passed acceptance, that a
  numerical result is valid, or that a physical model is applicable.
- `external_ngspice_backend: installed` does **not** prove a compatible ngspice
  version; only `pe-sim backend doctor` queries the executable.
- `pyspice_netlist_builder: installed` does **not** mean PySpice was imported or
  that a netlist can be built; only module presence was located.
- It does **not** inspect repository state or attestation anchors; it only
  checks whether Git is on `PATH`.
- Status is local and process-scoped. It is not a cross-machine or
  cross-release guarantee, and it can differ between Python versions and
  environments.

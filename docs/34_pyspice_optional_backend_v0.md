# Optional PySpice Circuit Frontend (v0)

**Status:** reference circuit-construction record. The reviewed Buck reference
is built with the PySpice object API and executed by the reviewed ngspice
process adapter. PySpice remains a separate installation because of its GPLv3
license and solver-version limitations; it is not a core runtime dependency.

## Why PySpice is a separate installation

PySpice is licensed under **GPLv3**, while this repository is MIT. Importing it
into the core runtime would create an avoidable copyleft coupling for every
downstream user. PySpice is also an old, low-maintenance project: the latest
production release is **1.5.0 (2021-05)** and its documentation states support
for ngspice up to version **34**. It is therefore the reference construction
frontend, not a bundled or mandatory dependency.

The core package never imports PySpice unless the reference builder is selected. PySpice is also not declared
as an optional extra in project metadata: `pe-sim environment-check` treats
declared extras as expected packages, and the project does not want to make a
GPLv3 dependency look like part of its own distribution. Install it manually in
the environment that will build PySpice circuits:

```powershell
pip install "PySpice==1.5"
pip install -e .       # the MIT-licensed core
```

## Verified compatibility findings

The native-execution findings below were observed with Python 3.10.21,
PySpice 1.5, and the conda-forge ngspice **41** installation. The supported
construction + external-process path was additionally verified on 2026-10-21
with Python 3.13.2, PySpice 1.5, and ngspice 41. Results:

| Execution path | Result |
| --- | --- |
| PySpice shared-library mode (`ngspice.dll`) | A simple RC circuit runs but emits `Unsupported Ngspice version 41`. The switching Buck crashes with `malloc: Internal Error` and `ngspice.dll cannot recover`. |
| PySpice `ngspice-subprocess` server mode | Fails while parsing the output: `NameError: Cannot locate binary data`. |
| PySpice circuit construction + external ngspice CLI | Works. The generated netlist is executed by the reviewed `NgspiceProcess` adapter and matches the native netlist result. |

The failures are version/ABI incompatibilities in PySpice, not defects in the
project's Plant contract. PySpice's native execution is therefore **not
claimed** by this project.

## Reference mode: model construction only

`PyspiceBuckNetlistBuilder` builds the reviewed asynchronous Buck with the
PySpice object API:

- `V`, `PulseVoltageSource`, voltage-controlled switch `S`, diode `D`,
  inductor and capacitor with initial conditions, resistive load;
- switch and diode models via `circuit.model(...)`;
- `circuit.str()` renders the netlist;
- the builder appends the reviewed `.options`, `.tran ... uic`, and
  `.control` / `wrdata` block;
- execution stays on the external ngspice process boundary.

### Initial-condition pitfall

PySpice renders `ic=0.45A`. SPICE interprets the trailing `A` as the **atto**
scale factor (1e-18), which silently zeroes the initial condition. The builder
therefore strips the unit suffix from `ic=` values. This is safe because the
builder supplies base units (`A`, `V`) only. A focused test asserts that
`ic=0.45A` never appears in the rendered netlist.

### Evidence

The Plant identity records:

```json
{
  "netlist_builder": {
    "name": "pyspice-model",
    "pyspice_version": "1.5",
    "execution": "external-ngspice-process",
    "native_pyspice_execution": false
  }
}
```

The ngspice executable provenance, solver settings, netlist digest, output
digest, and log digest are recorded exactly as for the native builder.

## Usage

Use an environment where PySpice is importable. The verified combination is
Python 3.10 with PySpice 1.5.

```powershell
pip install "PySpice==1.5"
pip install -e .
pe-sim backend doctor
pe-sim run examples/ngspice_buck_step/config.json --backend ngspice-buck --model-builder pyspice
```

The reference command selects `--model-builder pyspice`. The native builder
remains the default and is retained for offline/CI tests; it is not the
reference construction path. Selecting `pyspice` without the optional dependency
fails before creating a run directory with a `CONFIG_REJECTED` message.

## What is deliberately not claimed

- PySpice shared-library execution against ngspice 41 or newer;
- PySpice subprocess-server execution against ngspice 41 or newer;
- compatibility with every PySpice release or ngspice version;
- physical, thermal, hardware, or product validity;
- any expansion of the supported circuit beyond the reviewed Buck reference.

A future native PySpice execution adapter requires a compatible pair, for
example ngspice <= 34 or a newer PySpice release, and its own acceptance
matrix. It should live behind the backend plugin contract in
`docs/33_backend_plugin_contract_v1.md` and remain outside the core MIT claim.

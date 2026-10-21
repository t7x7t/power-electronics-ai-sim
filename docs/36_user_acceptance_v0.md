# User Acceptance (v0)

**Status:** repeatable end-user acceptance harness. It verifies that a person
who obtains the repository can install it, discover or configure an external
simulator, run a simulation, export the public visualization contracts, and
verify the resulting evidence without adding binaries or machine-local paths to
the repository.

It is infrastructure acceptance only. It does not validate a physical circuit,
a device model, thermal behavior, hardware safety, product readiness, or
cross-machine numerical equivalence.

## Why this exists

Unit and integration tests exercise internal APIs. They do not prove that the
documented user path works. The user acceptance run starts from a clean
Git snapshot and uses only the installed `pe-sim` command, the example
configuration, and the public consumer example. It has already caught two
release-blocking packaging defects:

1. `jsonschema` was only a test-extra dependency while the CLI required it;
2. the default experiment schema was resolved from the source checkout layout
   and was missing from a non-editable install.

Both are covered by regression tests in `tests/test_packaging.py`.

## Scenario matrix

| Scenario | Starting point | Expected result |
| --- | --- | --- |
| A: no simulator | clean checkout, fresh venv, empty user config, no ngspice on `PATH` | `pe-sim backend setup --name ngspice` reports `NOT_FOUND` with platform installation recipes; `pe-sim run ... --backend ngspice-buck` is rejected before creating a run directory |
| B: installed simulator | clean checkout, fresh venv, PySpice 1.5 via the verified pin set, `--ngspice <console executable>` | `pip check` is clean; `backend setup --write-config` writes a user-level configuration outside the checkout; `doctor` reports the version and digest; the reference run uses `--model-builder pyspice` and publishes a `QUALIFIED` package |
| B+: Windows GUI binary | `ngspice.exe` selected explicitly | the resolver substitutes the console sibling `ngspice_con.exe` and records the replacement note |
| Circuit variation | modified `plant_config` and `timebase` | the run remains `QUALIFIED`, the manifest records the modified parameters, and the sample count follows the new duration |
| Determinism | same configuration, two output directories | `compare-runs` and `audit-reproducibility` report `exact_match` (or a tolerance match) |
| Data export | published run package | `pe-sim export-workbench` writes `topology.json`, `trace.json`, and `outputs.json` outside the immutable run directory |
| External consumer | exported fixtures | `examples/visualization_consumer/verify_contracts.py` validates all three contracts using only the standard library |

When no simulator is available, simulator-dependent steps are recorded as
`skip` and the report status is `partial`, never `pass`.

## Running it

From the repository root, in any Python environment that can run the project
sources (the harness creates its own isolated venv):

```powershell
python scripts/user_acceptance.py `
  --ngspice "D:\tools\ngspice-44.2\Spice64\bin\ngspice_con.exe"
```

The acceptance workspace defaults to a temporary directory. Use `--workdir` to
keep the evidence in a known location. The harness writes:

- `acceptance-report.json` — machine-readable step results and provenance;
- `acceptance-report.md` — human-readable summary table;
- `checkout/` — the clean snapshot checkout used for the run;
- `derived/<run_id>/` — exported visualization contracts.

Useful options:

| Option | Meaning |
| --- | --- |
| `--install-mode editable` | default; matches the documented `pip install -e .` flow |
| `--install-mode wheel` | non-editable install; verifies wheel contents and packaged schemas |
| `--ngspice PATH` | reviewed console executable; otherwise `PE_SIM_NGSPICE` and `PATH` are inspected |
| `--require-ngspice` | turn a missing simulator into a failure instead of a `partial` report |
| `--skip-pyspice` | use the native netlist fallback; this is not the reference construction path |
| `--python PATH` | base interpreter used to create the fresh venv |

## What the report records

The report records the source commit and snapshot commit, host platform and
Python version, install mode, reference netlist builder (`pyspice-model`),
backend name/version/SHA-256, and per-step evidence. Absolute simulator paths
are intentionally excluded from portable run evidence; the local report
identifies the executable by name, version, and digest.

## Locally verified evidence (2026-10-21)

Reference executable: ngspice 41, console `ngspice_con.exe`, SHA-256
`41a6bda700d9160089f2a39a14f94408172f7748215b68b81d39ea1bc26e300c`,
Windows x86_64.

| Run | Install mode | Result |
| --- | --- | --- |
| Full flow | editable, Python 3.13.2 | pass: 14 steps, PySpice-built reference, `QUALIFIED`, 10 samples, export contracts verified |
| Full flow, pinned PySpice environment | editable, Python 3.10.21, `requirements-pyspice-tested.txt` | pass: `pip check` clean, PySpice-built reference `QUALIFIED`, export contracts verified |
| Full flow | wheel, Python 3.13.2 | pass: packaging and packaged-schema path verified |
| No simulator | wheel, Python 3.13.2 | partial: scenario A guidance passed; switching phases skipped |

Observed timings for the full flow are dominated by dependency installation
(about 45–90 seconds, including PySpice 1.5); the simulation, export, and
consumer checks take a few seconds. The harness runs `pip check` after
installation and records a clean result in the `install_package` step.

## Boundaries and known behavior

- `samples.npz` is written only when `numpy` is installed; `samples.json` is
  always the authoritative sample artifact.
- `environment-check` is a release-toolchain check. A fresh user venv does not
  contain the pinned test set, so the acceptance report records it as
  informational rather than as a user failure.
- The snapshot is an export of the working tree, including uncommitted changes,
  because this increment is not yet committed. Once the revision is published,
  the same harness validates the published commit.
- The harness never installs ngspice, never falls back to the Fake backend when
  an external backend is requested, and never writes the user configuration
  inside the checkout.
- The reference acceptance path installs PySpice 1.5 separately and uses
  `--model-builder pyspice`. PySpice is GPLv3 and is not a package dependency.
  On Python 3.10-3.12 the harness installs `requirements-pyspice-tested.txt`;
  on Python 3.13 it uses an isolated resolver-selected set and still asserts
  `pip check` is clean.
- `--skip-pyspice` records the native netlist fallback explicitly; that fallback
  is not the reference construction path.
- `QUALIFIED` is an automated per-run state. It is not physical, hardware,
  product, or maintainer approval.

## Relationship to the release gate

The acceptance run is an end-user check, not a replacement for
`scripts/release_check.ps1`. A release that mentions the ngspice backend should
record both:

1. the release check result for the exact clean commit; and
2. an end-user acceptance report with `--require-ngspice`, or a `partial`
   report that explicitly states that no simulator was available.

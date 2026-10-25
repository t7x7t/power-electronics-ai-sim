# Release Toolchain Policy (v0)

**Status:** local release-gate input. This policy identifies the exact
toolchain used for the first minimum-local-release checks. It does not make a
cross-platform, physical-model, or external-backend reproducibility claim.

## Two different version statements

The project intentionally makes two kinds of version statement. They must not
be substituted for one another.

The verified reference combination below was checked locally on 2026-09-13.

| Statement | Meaning | Current declaration |
| --- | --- | --- |
| Minimum compatibility | The lowest runtime range the project permits a user to try for this source release. It is not a claim that every version or platform in the range has passed release acceptance. | Python `>=3.10` in `pyproject.toml`. |
| Verified release toolchain | The exact versions on which the local release checks were run. A release gate must record the observed versions again rather than infer them from these files. | Python `3.12.7`; pytest `7.4.4`; jsonschema `4.23.0`; numpy `1.26.4`. |

The Python test dependency pins are recorded in
`requirements-tested.txt`. They are a reference test environment. `jsonschema`
is also a required runtime dependency because the CLI validates experiment
documents before constructing a run; `pytest` and `numpy` remain test-only.
The source CI installs these pins while it exercises Python 3.10, 3.11, and
3.12; this provides Python-version compatibility coverage without treating an
unreviewed resolver update as a source regression. It is not a replacement for
the exact release-gate record.

The browser workbench is temporarily withdrawn. The project therefore has no
Node/npm release-toolchain requirement; the headless visualization contract
exporter and local read-only service remain Python-only surfaces.

## External simulator (ngspice)

The reviewed switching Buck adapter invokes a separately installed ngspice
executable. ngspice is not bundled, linked, or relicensed. Its minimum
compatible major version, verified version, executable digest, and solver
settings policy are defined in `docs/32_ngspice_environment_lock_v0.md`.

- Ordinary CI does not install ngspice.
- Real-backend acceptance is opt-in via `PE_SIM_NGSPICE`.
- A release claim that mentions ngspice must record the exact executable
  version, SHA-256, platform, and solver settings used for the acceptance run.
- An absent ngspice executable is recorded as `not_run`, never as a pass.

## Clean installation policy

The Python base package remains small; `jsonschema` is its only required
third-party runtime dependency:

```powershell
python -m pip install -e .
```

The Buck reference additionally requires a separate **PySpice 1.5** installation
for circuit construction. PySpice is GPLv3 and is deliberately not a package
dependency or extra (decision E1 below). Use `requirements-pyspice-tested.txt`
in a dedicated Python 3.10-3.12 environment so `numpy==1.26.4` stays compatible
with the selected `scipy`/`matplotlib`/`contourpy` versions; run
`python -m pip check` before acceptance. The reference command is documented in
`docs/34` and `docs/35`. ngspice remains a separately installed external
executable.

For the locally verified test set, install the exact pins in a fresh Python
environment before installing the project test extra:

```powershell
python -m pip install -r requirements-tested.txt
python -m pip install -e ".[test]"
```

The test extra intentionally retains compatibility ranges for ordinary
development. A release gate uses the pinned reference file and records the
installed versions so a changed resolver result cannot be mistaken for the
verified combination.

## Install-time extras policy (decision E1, 2026-10-25)

**`test` is the only declared extra.** Optional simulator frontends are not
package extras. This is a recorded decision, not an unfinished item; see
[`adr/0003-install-time-extras.md`](adr/0003-install-time-extras.md).

PySpice stays a manual installation through
`requirements-pyspice-tested.txt`, discovered by `pe-sim capabilities`
(`docs/49`). Declaring a `pyspice` extra would be unsound here for three
independent reasons:

1. PySpice is GPLv3 while this repository is MIT; an extra presents a
   copyleft runtime as a supported variant of an MIT package (ADR 0002,
   `docs/34`).
2. `requires-python` includes Python 3.13, but the verified pin set keeps
   `numpy==1.26.4`, which has no 3.13 wheel; an unpinned extra resolves to a
   combination that fails `pip check`.
3. `environment-check` treats declared extras as expected packages. A
   `pyspice` extra would report a missing package in every correct MIT-only
   installation.

A new extra is allowed only when it installs cleanly across the declared
Python range, passes `pip check`, keeps the license boundary reviewed, does not
change the base `environment-check` verdict, and updates
`docs/49_optional_capabilities_v1.md` plus this section in the same commit.
The mechanical rules are enforced by `tests/test_release_policy.py`.

## What a release check must record

Before producing release evidence, record at least:

```powershell
python --version
python -m pip freeze
```

It must also run `pe-sim environment-check`.
[`scripts/release_check.ps1`](../scripts/release_check.ps1) is the
authoritative local automation for this record; its invocation and evidence
format are documented in
[`docs/29_local_release_check_v0.md`](29_local_release_check_v0.md). A version
tag is allowed only after its evidence is reviewed against this policy and the
minimum release scope matrix.

## Upgrade rule

Changing any verified version, Python pin, workbench dependency, lockfile, or
declared compatibility range requires a clean local re-run of the relevant
release checks and a refreshed evidence record. It does not require expanding
the v0 simulation claim. External simulators such as Ngspice have no verified
version in this release and must remain explicitly unassessed unless their own
adapter, lock, and acceptance evidence are added. The current ngspice
adapter, lock, and acceptance scope are recorded in
`docs/31_ngspice_backend_scope_v1.md` and
`docs/32_ngspice_environment_lock_v0.md`; until a verified executable is
recorded there, the external-simulator surface remains `Partial`.

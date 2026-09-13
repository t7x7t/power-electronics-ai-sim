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
| Minimum compatibility | The lowest runtime range the project permits a user to try for this source release. It is not a claim that every version or platform in the range has passed release acceptance. | Python `>=3.10` in `pyproject.toml`; optional workbench Node `^18.0.0 || ^20.0.0 || >=22.0.0` and npm `>=8.0.0` in `workbench/package.json` (aligned with the Vite toolchain's admitted engines). |
| Verified release toolchain | The exact versions on which the local release checks were run. A release gate must record the observed versions again rather than infer them from these files. | Python `3.12.7`; pytest `7.4.4`; jsonschema `4.23.0`; numpy `1.26.4`; Node `24.15.0`; npm `11.12.1`. |

The Python test dependency pins are recorded in
`requirements-tested.txt`. They are a reference test environment, not runtime
dependencies of the base package. The source CI currently exercises Python
3.10, 3.11, and 3.12; this is useful compatibility coverage but is not a
replacement for the exact release-gate record.

The workbench is optional and source-distributed. Its `packageManager` and
Volta metadata pin the verified npm/Node pair for tools that honor those
fields, while `engines` states the broader admitted compatibility range. Vite
and its locked transitive dependencies may have their own narrower platform
requirements; an unsupported install or build is a failed local prerequisite,
not a reason to weaken release evidence.

## Clean installation policy

For a checked-out revision of the workbench, use the committed lockfile:

```powershell
cd workbench
npm ci
npm run build
```

`npm ci` removes an existing `node_modules` directory and installs exactly the
dependency graph represented by `package-lock.json`. It fails if the lockfile
does not match `package.json`; that failure must be resolved and reviewed
before a release check. `npm install` is reserved for an intentional dependency
change by a maintainer, after which the changed `package.json` and
`package-lock.json` are reviewed together and `npm ci` is rerun.

The Python base package remains dependency-light:

```powershell
python -m pip install -e .
```

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

## What a release check must record

Before producing release evidence, record at least:

```powershell
python --version
node --version
npm --version
python -m pip freeze
```

It must also run `pe-sim environment-check`, `npm ci`, and `npm run build`.
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
adapter, lock, and acceptance evidence are added.

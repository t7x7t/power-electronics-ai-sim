# Stage 9: Preparatory Reproducibility Slice (v1)

This increment adds evidence tooling for the deterministic, project-owned
Fake/Buck/Boost fixtures. It does **not** validate Ngspice, a physical model,
or cross-machine numerical equivalence.

## Recommended environment check

`pe-sim environment-check` reads `requirements-tested.txt` as the verified
pin set and `pyproject.toml` as the supported range declaration. The JSON
result distinguishes:

- `verified_status=match|mismatch|missing|not_declared`;
- `supported_status=supported|unsupported|missing|unknown|not_declared`;
- overall `status=pass|fail|unknown`.

The command only inspects the current Python installation. It never installs
packages and reports the real backend as `not_assessed`; no Ngspice version is
invented.

```powershell
pe-sim environment-check
```

An explicit executable can be assessed without changing the default behavior:

```powershell
pe-sim environment-check --backend-executable ngspice --backend-name ngspice
```

The locally verified combination is Python 3.12.7, pytest 7.4.4,
jsonschema 4.23.0, and numpy 1.26.4. A supported range match does not mean
that the version has been locally verified.

## Backend and solver evidence

The generic runtime cannot infer an external simulator's executable, solver,
or numerical settings from a Plant object.  Adapters may implement the
`BackendProvenanceAdapter` protocol (`backend_provenance()`) or use the
provided `ExecutableBackendAdapter` for an explicit `--version` query:

```python
from pe_sim import ExecutableBackendAdapter, collect_environment_provenance

adapter = ExecutableBackendAdapter(
    "ngspice",
    name="ngspice",
    solver_settings={"reltol": 1e-3, "abstol": 1e-12},
)
environment = collect_environment_provenance(backend_adapters=(adapter,))
```

No executable is queried unless an adapter is supplied.  Missing adapters are
recorded as `status=not_assessed`; failed queries are `unavailable` and an
unparseable version is `partial`.  Solver settings are always adapter-declared
and therefore cannot be silently fabricated.  The recorded executable string
and version-output digest are evidence for review, not a cross-machine
attestation.

## Run comparison

`pe-sim compare-runs LEFT RIGHT --tolerance 1e-9` compares two published run
directories. It first requires clean source provenance and a known Python
environment. Dirty or unknown provenance returns `outcome=unknown`; it can
never be reported as reproducible. The outcomes are:

- `exact_match`: identity and samples are byte/value equal;
- `tolerance_match`: identity is equal and numeric sample differences are
  within the declared absolute tolerance;
- `mismatch`: inputs differ or sample values exceed tolerance;
- `unknown`: provenance or environment evidence is insufficient.

Before comparing samples, a published run also passes an integrity gate. The
gate requires a publishable terminal status (`RUN_OK`, `QUALIFIED`, or
`COMPARABLE`), every required artifact, a complete artifact index, a `PUBLISHED`
run-state marker, and independently recomputable `manifest_sha256` and
`package_sha256` values (including the mirrored `hashes` summary). Runs marked
`RUN_FAILED`, `INCOMPLETE`, `DISQUALIFIED`, or with missing/tampered files are
returned as `unknown`; they are evidence of execution outcome, not comparable
simulation results. A full 40- or 64-character hexadecimal Git object id is
required for `source_commit`.

The small hand-written manifests used by the preparatory fixture tests may
omit runtime status and digest fields. They remain useful for exercising the
comparison algorithm, but reports explicitly identify them as non-release-
grade fixture evidence. They are never treated as a substitute for a
published run package.

Partial environment evidence is retained as a limitation for the built-in
fixtures. This does not claim real-backend reproducibility.

When a run explicitly attempts an external backend, `unknown`, `unavailable`,
or `partial` backend provenance is a hard comparison gate and yields
`outcome=unknown`. This prevents a failed executable query or missing solver
settings from being hidden behind a tolerance match. The preparatory
exception is limited to built-in fixtures whose backend is explicitly
`not_declared` or `not_assessed`.

Python callers can use `pe_sim.compare_runs(left, right, tolerance=...)` and
serialize the returned `ReproducibilityReport` with `to_dict()`.

The report includes SHA-256 identity and canonical-sample digests,
environment equality, and a `difference_summary` that attributes differences
to identity, environment, structure, or numeric values within/exceeding the
declared tolerance.  A tolerance match is not an exact hash match: it means
only that all observed numeric deltas are bounded by the caller's tolerance.

## Repeated-run audit

For two or more runs from the same machine, compare every pair and aggregate
the result:

```powershell
pe-sim audit-reproducibility runs/run-a runs/run-b runs/run-c --tolerance 1e-9
```

`reproducibility-audit` and `audit-runs` are aliases.  The command returns
pairwise reports and counts.  Any unknown or mismatching pair prevents a
successful aggregate outcome.  Same-machine scope is inferred from equal
manifest environment identities; it is not a host attestation.

## Cross-machine evidence

Before attempting a cross-machine trial, create a portable evidence matrix:

```powershell
pe-sim cross-machine-evidence machine-a/run machine-b/run
```

The matrix records source/package hashes, platform identity, environment
status, and an environment identity digest. Each run is subjected to the same
published-package integrity gate; missing or inconsistent summaries leave the
matrix `not_assessed` and identify the affected run. `ready_for_trial` requires
at least two distinct platform identities, aligned source commits, and valid
provenance. It deliberately does **not** claim numeric equivalence; a reviewed
backend adapter, locked solver settings, and an explicit tolerance policy are
still required.

## Baseline/release evidence

`pe-sim baseline-report RUN/manifest.json` creates a JSON template linking
source commit, branch, dirty state, Manifest/package digests, environment,
CI evidence, tests, and human review. CI and review fields default to
`not_recorded`; they must be supplied by the operator and are never inferred.
`build_baseline_report()` and `write_baseline_report()` provide the Python API.

The report is an evidence record, not a release approval or an engineering
sign-off. A dirty worktree is explicitly marked as unsuitable for a formal
baseline.

## Deliberate limits and next step

External executable locking, real-model repetition, and cross-machine numeric
claims remain gated on a reviewed backend adapter and the Stage 14
source/license review. The present tooling makes each limitation visible
rather than silently accepting an unknown environment.

## Verification

```text
pytest -q tests/test_stage9_preparation.py --basetemp .tmp/pytest-stage9-prep
pytest -q tests/test_stage9_reproducibility.py --basetemp .tmp/pytest-stage9-repro
python -m compileall -q src tests
git diff --check
```

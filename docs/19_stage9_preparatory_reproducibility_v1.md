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

The locally verified combination is Python 3.12.7, pytest 7.4.4,
jsonschema 4.23.0, and numpy 1.26.4. A supported range match does not mean
that the version has been locally verified.

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

Partial environment evidence is retained as a limitation for the built-in
fixtures. This does not claim real-backend reproducibility.

Python callers can use `pe_sim.compare_runs(left, right, tolerance=...)` and
serialize the returned `ReproducibilityReport` with `to_dict()`.

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

External executable discovery/locking, solver and thread settings, real-model
repetition, and cross-machine trials remain gated on a reviewed backend
adapter and the Stage 14 source/license review. The present tooling makes the
limitation visible rather than silently accepting an unknown environment.

## Verification

```text
pytest -q tests/test_stage9_preparation.py --basetemp .tmp/pytest-stage9-prep
python -m compileall -q src tests
git diff --check
```


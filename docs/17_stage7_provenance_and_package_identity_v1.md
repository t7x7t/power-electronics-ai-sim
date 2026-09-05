# Stage 7: Provenance and Package Identity (v1)

Stage 7 makes every run answer a narrow but important question: which source,
runtime environment, model configuration, and controller configuration produced
these artifacts? It does not certify the physical model or claim cross-machine
reproducibility; those are Stage 9 responsibilities.

## Recorded identity

`manifest.json` records the Git commit, branch, clean/dirty state, a portable
worktree report, runtime Python/platform information, installed dependency
versions, and adapter-declared backend/solver information. Python and platform
values are collected from the running interpreter. A backend that cannot
declare its executable/version is recorded as `not_declared` with a limitation,
never as a fabricated version.

The same environment object is written to `environment.json`. Failed and
interrupted runs use the same provenance path, so a failure can be diagnosed
against the environment that actually produced it.

## Component hashes

Plant and Controller identities are canonical JSON objects. An adapter may
provide `manifest_identity()` with its parameters, implementation digest and
capabilities. The generic fallback records module/class, capabilities, and
public configuration attributes while excluding mutable execution state such
as clock, state, history, and last action. The SHA-256 digest of that canonical
identity is stored as `plant.hash` or `controller.hash` and repeated in
`provenance.plant_sha256` / `provenance.controller_sha256`.

Changing a model parameter or controller setting must therefore change the
component digest. Local checkout paths and object memory addresses are not
part of the fallback identity.

## Manifest and package digests

The following deterministic rules are public and independently re-computable:

1. `manifest_sha256` hashes canonical Manifest JSON after removing
   `manifest_sha256`, `package_sha256`, `manifest_hash`, `package_hash`, and
   the same derived keys under `hashes`.
2. `package_sha256` hashes canonical JSON containing that derived-key-free
   Manifest plus a sorted list of every other file's relative POSIX path and
   byte SHA-256. `manifest.json` is excluded as a file and represented by the
   canonical Manifest object, avoiding recursion while still binding the
   package to the Manifest contents.
3. `hashes` repeats both values for tools that prefer a grouped field.

The artifact index continues to exclude `manifest.json`; this is intentional.
An independent reader can call `manifest_digest(manifest)` and
`package_digest(run_dir, manifest)` to verify the stored values.

## Dirty worktree evidence

Exploratory runs retain the categorized dirty report. Its repository-local root
is removed before publication, then the report is hashed as
`worktree_analysis_sha256`. The digest is repeated in `provenance` alongside
the source commit and component hashes. Formal-comparison mode still rejects a
dirty or unknown worktree before creating a run directory.

## Scope and limitations

This increment does not lock dependencies, pin Ngspice, or prove cross-machine
numeric reproducibility. It also does not infer a backend version that an
adapter does not expose. Those checks belong to Stage 9 and to concrete backend
adapters. The hashes provide identity and audit evidence, not engineering or
hardware approval.

## Verification

```text
pytest -q --basetemp .tmp/pytest-stage7
python -m compileall -q src tests
git diff --check
```


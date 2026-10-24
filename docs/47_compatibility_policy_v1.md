# Compatibility Policy and Discovery Root v1

**Status:** Phase C first increment, shipped in 0.5.9. It records D3 and D4 and
implements read-with-policy compatibility verdicts and an explicit discovery
root. Install-time capability extras are deferred (§3), and it does **not**
claim T4: the upgrade gate is still open.

## 1. Decisions

### D3 - explicit discovery root

A package is written only where its experiment document's `output.directory`
says. The kernel never relocates a published package.

Discovery resolves its root in this order:

1. an explicit CLI argument;
2. `PE_SIM_RUNS_ROOT` when it holds a non-blank value;
3. `runs` in the working directory.

`pe-sim list-runs [root]` and `pe-sim visualization-serve [--runs-root PATH]`
both use this order. Resolution never creates, repairs, verifies, or enumerates
anything by itself.

Why not a user-home default? A silent relocation would break reproducibility and
the parent-link evidence chain. A user-scoped store is a deployment choice: an
installer or operator sets `PE_SIM_RUNS_ROOT`, and the resolution order makes
that choice explicit.

### D4 - read-with-policy, never rewrite on read

A reader classifies a package and states the limits of reading it. It never
migrates or rewrites a published package. If a migration is ever produced, it is
a **new derived package** with a parent link; the original stays byte-identical.

## 2. Compatibility verdict (`pe-sim.compatibility.v1`)

`assess_package_compatibility(manifest)` is pure: it reads only the manifest
mapping, never touches the disk, and never raises for malformed input.

| Status | When | Reading rule |
| --- | --- | --- |
| `current` | same major/minor as the reader | direct reading; `cross_version_comparison: current` |
| `read_only` | older major/minor | summary is allowed; absent fields stay absent; `cross_version_comparison: not_assessed` |
| `incompatible` | newer, missing, or malformed | do not rely on the schema; `cross_version_comparison: unsupported` |

Every verdict carries:

- `package_schema` and `reader_schema` (both named, so a client never guesses);
- `reasons` and `limitations`;
- `may_rewrite: false` - a verdict can never authorize a write;
- `schema: "pe-sim.compatibility.v1"`.

`inspect-run` reports the verdict at `summary.compatibility`. **Integrity and
compatibility are separate verdicts:** an older package can still verify
(`status: ok`) while the reader says `read_only`. The package is intact; it is
the *interpretation* that is limited.

Patch suffixes (`0.1.4`) are ignored for ordering; a different major or minor is
not.

## 3. Install-time extras: deferred by an existing policy

The roadmap's Phase C item 1 asks for capability extras. `docs/28` explicitly
states that PySpice is "deliberately not a package dependency or extra", and
the verified pin set (`numpy==1.26.4`) does not support the Python 3.13 runtime
used by this checkout. Adding a `pyspice` extra now would either contradict that
policy or produce an extra that fails to install on the tested interpreter.

**Disposition:** item 1 is **not delivered** in 0.5.9. Resolving it needs a
release-policy decision (a separate wheel or requirements handoff, or a
compatible pin set). The compatibility and discovery work does not depend on it,
and `environment-check` behaviour is unchanged.

## 4. What is not done

- **Install-time extras**: deferred; see §3. `docs/28` keeps PySpice out of
dependencies and extras.
- **Schema migration registry**: deliberately deferred to the first schema bump,
  as `docs/43` requires. There is no schema bump yet.
- **Upgrade gate (T4)**: install -> run -> upgrade -> older package still
  verifies. The reader policy exists; the gate that exercises two installed
  releases does not.
- Marketplace, cloud sync, and automatic migration are out of scope.

## 5. Acceptance evidence

```text
pytest -q tests/test_compatibility.py tests/test_run_store.py `
  tests/test_inspection.py tests/test_run_catalog.py `
  --basetemp .tmp/pytest-compat
```

`tests/test_compatibility.py` covers every status and malformed input;
`tests/test_inspection.py` proves an older package stays `ok` while its
compatibility is `read_only`; `tests/test_run_store.py` and
`tests/test_run_catalog.py` cover the discovery order without creating
directories.

## 6. Related documents

| Document | Relationship |
| --- | --- |
| `docs/43_kernel_roadmap_v1.md` | Phase C, D3/D4, T4 |
| `docs/44_run_interaction_contract_v1.md` | `list-runs` and `inspect-run` surfaces |
| `docs/02_run_artifacts_and_state_machine_v0.md` | immutable packages |
| `docs/08_ci_tested_environment_v0.md` | tested environment and extras |

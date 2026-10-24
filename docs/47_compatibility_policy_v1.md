# Compatibility Policy and Discovery Root v1

**Status:** Phase C first increment shipped in 0.5.9 (D3/D4, read-with-policy
verdicts, explicit discovery root). The **upgrade gate landed in 0.5.11**: an
installed release reads an older release's package and states cross-release
comparability honestly, so test **T4 passes**. Install-time capability extras
remain deferred by the existing release policy (§3), and the schema migration
registry is still deferred to the first schema bump (§4).

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

`inspect-run` reports the verdict in the top-level `compatibility` field of
its JSON output (`RunInspection.summary["compatibility"]` on the Python
object). **Integrity and
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

**Disposition:** item 1 is **not delivered**. Resolving it needs a
release-policy decision (a separate wheel or requirements handoff, or a
compatible pin set). The compatibility and discovery work does not depend on it,
and `environment-check` behaviour is unchanged. 0.5.11 records the same
decision rather than quietly adding an extra that would contradict `docs/28`.

## 4. Upgrade gate (T4, delivered in 0.5.11)

**Reading across releases is not the same as comparing across releases.** A
0.5.9 package and a 0.5.11 package both declare `schema_version: "0.1"`, so both
are `current` for reading. That says nothing about whether their numbers may be
compared: the code that produced them changed.

`pe_sim.reproducibility.compare_runs` now adds a `compatibility` block when, and
only when, there is something to say:

| Situation | `cross_version_comparison` | Effect |
| --- | --- | --- |
| same schema, same `source_commit` | block omitted | historical payload unchanged |
| same schema, different `source_commit` | `not_assessed` | reasons name both commits; a numeric difference cannot be attributed to the model alone |
| different schema (either side `read_only`) | `not_assessed` | absent fields are not inferred |
| either side `incompatible` | `unsupported` | the outcome is `unknown`, never a numeric result |

The block carries both full verdicts (`left`/`right`), `reasons`, and
`limitations`. It is additive: packages from the same commit serialize exactly
what they serialized before.

`scripts/upgrade_check.ps1` is the gate itself:

1. require a clean source commit;
2. build the checkout as a wheel and install it into an isolated venv with the
   pinned test requirements;
3. assert the installed `pe-sim --version` matches `pyproject.toml`;
4. `inspect-run` an older package and require `status: ok` with a readable
   compatibility verdict;
5. `list-runs` the older package's directory;
6. produce a package with the installed release and `compare-runs` it against
   the older package;
7. require the report to declare `compatibility.cross_version_comparison =
   "not_assessed"`;
8. write `upgrade-evidence.json` (`pe-sim.upgrade-evidence.v1`) and exit
   non-zero on any failed check.

The checked-in fixture `tests/fixtures/older_packages/v0.5.9-buck` was produced
by the v0.5.9 release itself (commit `ed45521…`), not hand-written.
`scripts/release_check.ps1` runs the same gate as its `upgrade-gate` step.

## 5. What is not done

- **Install-time extras**: deferred; see §3. `docs/28` keeps PySpice out of
  dependencies and extras.
- **Schema migration registry**: deliberately deferred to the first schema bump,
  as `docs/43` requires. There is no schema bump yet.
- **Automatic migration**: out of scope. A migration, if it ever exists, is a
  newly derived package with a parent link.
- Marketplace, cloud sync, and automatic repair are out of scope.

## 6. Acceptance evidence

```text
pytest -q tests/test_compatibility.py tests/test_run_store.py `
  tests/test_inspection.py tests/test_run_catalog.py `
  tests/test_upgrade_gate.py --basetemp .tmp/pytest-compat
powershell -NoProfile -File scripts/upgrade_check.ps1 `
  -EvidenceDirectory .tmp/upgrade-evidence -PythonExecutable <python-3.12.7>
```

`tests/test_compatibility.py` covers every status and malformed input;
`tests/test_inspection.py` proves an older package stays `ok` while its
compatibility is `read_only`; `tests/test_run_store.py` and
`tests/test_run_catalog.py` cover the discovery order without creating
directories; `tests/test_upgrade_gate.py` uses the real 0.5.9 package to prove
readability, discovery, the unchanged same-commit payload, and both the
`not_assessed` and `unsupported` branches.

## 7. Related documents

| Document | Relationship |
| --- | --- |
| `docs/43_kernel_roadmap_v1.md` | Phase C, D3/D4, T4 |
| `docs/44_run_interaction_contract_v1.md` | `list-runs` and `inspect-run` surfaces |
| `docs/02_run_artifacts_and_state_machine_v0.md` | immutable packages |
| `docs/08_ci_tested_environment_v0.md` | tested environment and extras |

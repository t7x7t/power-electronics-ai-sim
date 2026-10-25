# Local Release Check (v0)

`scripts/release_check.ps1` is the local, fail-closed implementation of the
automation gate required by the release scope and toolchain policy:

- `docs/27_minimum_local_release_scope_v0.md` defines what a passing gate may
  support, and the required human approval after the gate;
- `docs/28_release_toolchain_policy_v0.md` defines the recorded toolchain and
  clean-install policy.

Run it from a clean checkout in PowerShell. The script creates a dedicated
isolated virtual environment under `.tmp/release-venv/<commit>` and uses its
absolute interpreter for every Python gate; no manual activation is needed:

```powershell
pwsh -File scripts/release_check.ps1
```

On Windows PowerShell, use:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/release_check.ps1
```

The default creator is the Windows Python launcher (`py -3.12`). Override it
when necessary with `-PythonExecutable`, `-PythonVersion`, or
`-VirtualEnvironment`. Existing environments are reused only as an explicit
path and their installed toolchain is still checked by the release gates.
The generated evidence records the interpreter path and isolated-environment
mode, making accidental use of a global Conda environment visible.

The release environment installs `requirements-release.txt`, which is the
verified Python test pin set. The browser workbench is temporarily withdrawn,
so no Node/npm or browser-automation dependency is part of the release gate.

It writes a new evidence directory by default under
`.tmp/release-evidence/<timestamp>-<commit>/`, which is intentionally ignored
by Git.  To retain evidence elsewhere, supply an explicit empty or
release-specific destination:

```powershell
pwsh -File scripts/release_check.ps1 -EvidenceDirectory D:\release-evidence\v0.2.1
```

The resulting `release-evidence.json` has schema
`pe-sim.local-release-evidence.v1`.  It records the exact source commit, Git
diagnostic, observed Python versions, pip freeze log, each gate's
status/exit code/log path, and the non-claim limitations.  A non-zero script
exit, `outcome: "fail"`, or any skipped gate is release-blocking.

The evidence also records SHA-256 digests of the checked-in release inputs,
including the GitHub Actions workflow. This binds the local acceptance record
to the lightweight CI configuration without claiming that the local script ran
a hosted CI service.

The gate checks a clean Git worktree, Python environment policy, the complete
pytest suite, and the headless visualization contract service/consumer path.
It does not create official Buck/Boost demonstrations: pass their already-reviewed,
clean-commit run root with `-FormalDemoRunsRoot` to verify and record its
integrity, source commit, and `formal_comparison` mode.  It starts the
loopback read-only visualization service against a checked-in contract fixture
and uses the independent standard-library consumer against that route.  It
stops only the service process it created before exit.

Since 0.5.11 the gate also runs the **upgrade gate**
(`scripts/upgrade_check.ps1`) as its `upgrade-gate` step. That step builds the
checkout as a wheel, installs it into its own isolated environment, reads a
package produced by the previous release, produces a package with the installed
release, and requires the comparison report to declare
`compatibility.cross_version_comparison = "not_assessed"`. The default fixture
is `tests/fixtures/older_packages/v0.5.9-buck`; pass `-OlderPackage` to point
at another package. The step writes `upgrade-evidence.json`
(`pe-sim.upgrade-evidence.v1`) inside the evidence directory, and a failed
check is release-blocking like any other gate.

`-PythonExecutable` accepts either a launcher (`py`) or an absolute path to a
`python.exe`. A path is used directly, because the `-3.12` argument is a
launcher-only argument; the exact version is still verified by
`toolchain-exact`.

The script does not commit, stash, reset, tag, publish, create, or modify a
source run package.  It will preserve its diagnostics even when the working
tree is dirty, but it cannot pass or verify formal demonstration evidence in
that state.  A passing result remains automation evidence only: a human
 reviewer must inspect it, record approval, and create any release tag.

## Release record and approval

Since 0.7.0 the reviewed form of that human step is a release record, defined
in [`53_release_governance_v1.md`](53_release_governance_v1.md). After this
gate passes for a clean commit:

```powershell
python -m build
python scripts/distribution_inventory.py dist/*.whl `
    -o dist/distribution-inventory.json --license-file LICENSE
python scripts/release_record.py create `
    --version 0.7.0 --evidence D:\release-evidence\v0.7.0\gate `
    --wheel dist\*.whl --sdist dist\*.tar.gz `
    --inventory dist\distribution-inventory.json `
    --formal-demo-evidence D:\release-evidence\v0.7.0\formal-demos\formal-demo-evidence.json `
    --output D:\release-evidence\v0.7.0\release-record.json
python scripts/release_record.py approve D:\release-evidence\v0.7.0\release-record.json `
    --reviewer "<reviewer>" --statement "<what was reviewed and accepted>"
python scripts/release_record.py verify D:\release-evidence\v0.7.0\release-record.json `
    --dist dist `
    --evidence-dir D:\release-evidence\v0.7.0\gate `
    --evidence-dir D:\release-evidence\v0.7.0\formal-demos `
    --require-approved
git tag -a v0.7.0 -m "v0.7.0 <record_digest> <commit>"
python scripts/release_record.py verify D:\release-evidence\v0.7.0\release-record.json `
    --dist dist `
    --evidence-dir D:\release-evidence\v0.7.0\gate `
    --evidence-dir D:\release-evidence\v0.7.0\formal-demos `
    --require-approved --require-tag
```

`--evidence-dir` is repeatable; each root is searched by filename, so gate
evidence and formal-demo evidence can stay in the directories their producers
wrote them to.  `--require-tag` is added only after the tag exists: the
pre-tag run binds the approval to the artifacts, and the post-tag run binds
the tag to the recorded commit.

`create` writes `approval.status = "pending"` and `signature.status =
"not_signed"`; it cannot record an approval. `approve` refuses to run when `CI`
is set, so approval stays a local human action. `verify` exits `1` on a
mismatch and `2` on a check it could not perform; both are release-blocking.
The tag is created only after `verify` passes with `--require-tag` at the
release commit.

## Formal Buck/Boost demo generation

Generate official demonstration packages separately, before this gate:

```powershell
python scripts/generate_formal_demos.py D:\release-evidence\v0.2.1\formal-demos
```

The destination must be new or empty and outside the source checkout. The
generator refuses an unknown or dirty Git worktree and refuses any toolchain other than the exact versions in
`docs/28_release_toolchain_policy_v0.md`. It runs the source-controlled Buck
and Boost configurations in `formal_comparison` mode, then reopens each
published package through the integrity-checked workbench reader. The result
contains `buck/`, `boost/`, and `formal-demo-evidence.json`; each run records
its `source_commit`, `manifest_sha256`, and `package_sha256`.

The built-in reference backend is identified as
`pe_sim.ideal_averaged_python` with RK4 and its declared substep. This is a
known provenance identity for a deterministic, project-owned Python reference,
not an Ngspice or physical-solver version. Example contract hashes are derived
from the reviewable definitions in `pe_sim.default_contracts`, not placeholders.

Pass the resulting directory to `release_check.ps1` using
`-FormalDemoRunsRoot` to bind hashes to release-check evidence. Neither
command commits, tags, publishes, or grants human approval.

# Local Release Check (v0)

`scripts/release_check.ps1` is the local, fail-closed implementation of the
automation gate required by the release scope and toolchain policy:

- `docs/27_minimum_local_release_scope_v0.md` defines what a passing gate may
  support, and the required human approval after the gate;
- `docs/28_release_toolchain_policy_v0.md` defines the recorded toolchain and
  clean-install policy.

Run it from a clean checkout in PowerShell:

```powershell
pwsh -File scripts/release_check.ps1
```

On Windows PowerShell, use:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/release_check.ps1
```

It writes a new evidence directory by default under
`.tmp/release-evidence/<timestamp>-<commit>/`, which is intentionally ignored
by Git.  To retain evidence elsewhere, supply an explicit empty or
release-specific destination:

```powershell
pwsh -File scripts/release_check.ps1 -EvidenceDirectory D:\release-evidence\v0.2.1
```

The resulting `release-evidence.json` has schema
`pe-sim.local-release-evidence.v1`.  It records the exact source commit, Git
diagnostic, observed Python/Node/npm versions, pip freeze log, each gate's
status/exit code/log path, and the non-claim limitations.  A non-zero script
exit, `outcome: "fail"`, or any skipped gate is release-blocking.

The gate checks a clean Git worktree, Python environment policy, the complete
pytest suite, an isolated-copy `npm ci`, and `npm run build`.  It does not
create official Buck/Boost demonstrations: pass their already-reviewed,
clean-commit run root with `-FormalDemoRunsRoot` to verify and record its
integrity, source commit, and `formal_comparison` mode.  It starts the
loopback read-only visualization service against a checked-in contract fixture
and uses the independent standard-library consumer against that route.  It
stops only the service process it created before exit.

`npm ci` intentionally replaces `workbench/node_modules` according to the
committed lockfile.  If Windows reports a locked file, close Vite/dev servers,
Explorer windows, antivirus/scanner handles, and other Node processes that
touch `workbench/node_modules`, then rerun.  The script records this as a
failure; it does not substitute `npm install` or claim success.

The script does not commit, stash, reset, tag, publish, create, or modify a
source run package.  It will preserve its diagnostics even when the working
tree is dirty, but it cannot pass or verify formal demonstration evidence in
that state.  A passing result remains automation evidence only: a human
 reviewer must inspect it, record approval, and create any release tag.

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

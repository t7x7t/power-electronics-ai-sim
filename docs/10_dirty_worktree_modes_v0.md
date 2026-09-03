# Dirty Worktree Analysis and Run Modes (v0)

The runtime supports two explicit modes. Both are useful; they answer different
questions and must not be confused.

## Exploratory mode

Exploratory mode is the default. It permits a dirty or unavailable Git
worktree so that an agent or developer can iterate quickly. The run still
records the source status and a read-only worktree analysis in
`manifest.json`. Such a run is evidence for debugging and exploration, not a
formal comparison baseline.

```powershell
pe-sim psfb-step --mode exploratory --output-dir runs --run-id trial-001
```

## Formal comparison mode

Formal comparison is fail-closed. The run is refused before any run directory
is created unless Git reports a known, clean worktree. This prevents an
uncommitted source or configuration change from being silently compared with
another baseline.

```powershell
pe-sim psfb-step --mode formal_comparison --output-dir runs --run-id baseline-001
```

After making or reviewing changes, create a commit (or otherwise remove the
unintended changes), then verify:

```powershell
pe-sim git-status
```

The command is read-only and emits JSON containing `status`, commit and branch,
all changed paths, categories, reasons, and actionable suggestions. Categories
are `source`, `config`, `tests`, `docs`, `generated`, and `other`.

Generated outputs are reported separately so they can be deleted or ignored;
they are never silently treated as a clean baseline. Intentional source,
configuration, test, or documentation changes should be committed and then
checked again. A repository outside Git, a missing Git executable, or an
unreadable `HEAD` is `unknown` and is also rejected by formal mode.

This policy does not claim that a clean commit is physically correct. It only
establishes a stable source identity for comparison. Model validity, parameter
appropriateness, safety, and engineering conclusions remain the user's
responsibility.

## Python API

```python
from pe_sim.dirty import analyze_git_worktree, require_formal_comparison

analysis = analyze_git_worktree()
print(analysis.to_dict())
require_formal_comparison(analysis)  # raises FormalComparisonError unless clean
```

The analyzer never stages, commits, deletes, edits, or ignores files.

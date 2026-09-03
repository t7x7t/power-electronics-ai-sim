# Workspace Layout and Temporary Output Policy (v0)

The root contains only project entry points and stable top-level areas. Runtime
and test output must not accumulate as peer directories beside source code.

## Stable areas

```text
src/       reusable Python package and runtime code
tests/     automated tests
schemas/   machine-readable experiment/Manifest schemas
examples/  small reference configurations and launch scripts
docs/      architecture, governance, operations, and backlog documents
agent/     Agent operating policy
.github/   lightweight CI workflow
```

Top-level files (`README.md`, `LICENSE`, `pyproject.toml`, and the tested
requirements record) are project metadata and entry points.

## Temporary areas

```text
.tmp/
  verification-archive-YYYYMMDD/  archived pytest/CI/smoke outputs
  <new per-task scratch directories>
```

`runs/`, `.pytest_*`, `.ci-runs-*`, `tmp*`, caches, and build output are
generated artifacts. They are ignored by Git and should be created under
`.tmp/` (or a caller-provided output directory) during local work. The current
archive `verification-archive-20260903` contains only historical validation
outputs moved from the root; delete the archive after the outstanding human
acceptance is complete.

As of 2026-09-03, all previously root-level pytest, CI, run, and temporary
directories have been moved into that archive. The root now contains no such
generated peer directories. The archive itself is intentionally untracked and
may be removed after human acceptance.

## Cleanup rule

Before a baseline or release check:

1. Inspect `pe-sim git-status` and confirm no generated output is mixed with
   intentional source changes.
2. Preserve any run needed as evidence by copying it to an explicitly named
   evidence location; do not commit transient caches.
3. Delete `.tmp/verification-archive-*` and other scratch directories once the
   corresponding verification is no longer needed.
4. Re-run the read-only dirty analysis. Formal comparison is allowed only when
   it reports `status=clean`.

No source, schema, example, test, or documentation file should be moved to the
temporary area merely because it was produced during an Agent task.

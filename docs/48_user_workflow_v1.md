# User Workflow v1 (Phase D)

**Status:** delivered in 0.5.12. This document describes the workflow a user who
does not read source can follow from an installed wheel, and states exactly what
authority each step holds.

Roadmap: Phase D, test T6 ("a user who does not read source can run, view, and
verify"), gate G-D.

## 1. The workflow

All three steps use only versioned, documented surfaces. None of them requires a
checkout, a Python import, or reading the source.

### Run

```powershell
pe-sim demo .\my-demo
```

`pe-sim demo` performs four things:

1. reads a bundled experiment document from `pe_sim.templates`;
2. writes it to `<directory>\config.json` with a generated `run_id` and
   `output.directory` pointing at `<directory>\runs`;
3. runs it through the same code path as `pe-sim run` (same validation, same
   preflight gates, same evidence derivation);
4. prints a `pe-sim.run-demo.v1` document containing the normal
   `pe-sim.run-result.v1` payload plus the next commands.

The bundled documents are the averaged Buck and Boost reference experiments
(`--name buck|boost`). They are project-owned reference models, not physical or
hardware claims. `--run-id` overrides the generated identifier. An unknown
template name is rejected with the branchable code `invalid_request`.

### View

```powershell
pe-sim visualization-serve --runs-root .\my-demo\runs
```

The service is read-only and loopback-only. It serves the three published
visualization contracts for verified packages and rejects every non-GET request.
It cannot create, modify, repair, or delete a run.

### Verify

```powershell
pe-sim inspect-run .\my-demo\runs\<run_id> --compact
pe-sim attest-run .\my-demo\runs\<run_id>
pe-sim verify-attestation .\my-demo\runs\<run_id> --attestation <path>
```

`inspect-run` recomputes the artifact index and package digest and reports
`status: ok` only when they match. `attest-run` writes a detached attestation
outside the package; `verify-attestation` reports `L1_anchored` when the
attestation is anchored in a clean Git commit. None of these commands rewrites a
published package.

## 2. What the consumer may do

| Step | Authority |
| --- | --- |
| `demo` | create a configuration file in a directory the caller names; start one experimental run |
| `visualization-serve` | read verified packages under the given root; no mutation |
| `inspect-run` | read; recompute integrity hashes; no mutation |
| `attest-run` / `verify-attestation` | write a detached attestation outside the package; no mutation of the package |

A consumer receives no Runner, Plant, Controller, plugin, or mutation authority.
The demo path exists so that "run, view, verify" does not require reading source;
it is not a second implementation of the runtime.

## 3. Evidence level of a demo run

A demo is an exploratory run of a reference model. It records
`evidence_level: functional` and whatever qualification level the rule pack
derives (typically `limited` with `duty_varying` for a short step). It is **not**
a formal comparison, a physical validation, or a hardware approval.

## 4. Distribution

The templates ship as package data (`[tool.setuptools.package-data]`
`"pe_sim.templates" = ["*.json"]`), and
`tests/test_packaging.py::test_demo_templates_are_packaged_and_match_the_examples`
keeps the packaged copies byte-identical to `examples/`.

## 5. What is not delivered

- No browser workbench. The TypeScript/Vite UI was withdrawn on 2026-10-23 and
  remains out of scope.
- No remote API, authentication, multi-user operation, or cloud sync.
- No interactive parameter editor. Editing `config.json` and re-running
  `pe-sim run <config.json>` is the supported path.

## 6. Acceptance evidence

```text
pytest -q tests/test_user_workflow.py tests/test_packaging.py `
  tests/test_cli_run_interaction.py tests/test_plant_extension_gate.py `
  --basetemp .tmp/pytest-workflow
pe-sim demo .tmp\demo-check
pe-sim inspect-run .tmp\demo-check\runs\<run_id> --compact
```

## 7. Related documents

| Document | Relationship |
| --- | --- |
| `docs/43_kernel_roadmap_v1.md` | Phase D, gate G-D, T6 |
| `docs/44_run_interaction_contract_v1.md` | run/progress/terminal contracts |
| `docs/26_stage11_consumer_and_service_v1.md` | read-only service (§3) |
| `docs/27_minimum_local_release_scope_v0.md` | what a release may claim |

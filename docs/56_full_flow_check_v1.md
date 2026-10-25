# End-to-End Flow Check (v1)

**Status:** product-phase smoke check. It exercises the documented user and agent
flow in one run and emits a machine-readable record. It is not a release gate,
not a physical validation, and not a substitute for the local release check.

## Purpose

Individual commands are covered by tests, but a user or an agent needs one
answer to "does the whole path still work end to end?". This check walks the
documented path in order and records where it stops:

```text
environment → capabilities → agent surface → configure →
run → debug → inspect → review → export → service → audit → compare
```

## Contract `pe-sim.full-flow-check.v1`

```json
{
  "schema_version": "pe-sim.full-flow-check.v1",
  "rule_version": "full-flow-check-v1",
  "outcome": "pass",
  "started_at_utc": "2026-10-25T00:00:00Z",
  "environment": {"python": "3.13.2", "executable": "<path>", "source_root": "<path>"},
  "workspace": {"output_dir": "<path>", "run_dir": "<path>"},
  "steps": [
    {"id": "environment", "title": "Python environment and verified pins",
     "required": true, "status": "pass", "detail": "...", "evidence": ["..."]}
  ],
  "limitations": ["..."]
}
```

Step ids, in execution order:

| `id` | Required | What it proves |
| --- | --- | --- |
| `environment` | yes | `environment-check` reports a supported environment and records the observed versions |
| `capabilities` | yes | the capability document parses and names the registered backends |
| `agent-surface` | yes | the non-interactive commands an agent uses (`--help`, `capabilities --json`, `backend list --json`, `backend guide`) all succeed |
| `configure` | yes | a Buck + PI + timebase experiment document can be written and is accepted by the CLI validator |
| `run` | yes | the experiment runs to a published package with a clean worktree status |
| `debug` | yes | an invalid document is rejected with a typed `CONFIG_REJECTED` envelope, and a tampered package is reported as not intact |
| `inspect` | yes | `inspect-run`, `parameter-view`, and `review-run` return their documented schema tags |
| `export` | yes | the three visualization contracts are exported and revalidated |
| `service` | no | the loopback service answers health, discovery, topology, and review, then the process started by the check is stopped |
| `audit` | no | `attest-run` writes a detached attestation and `verify-attestation` reads it back (skipped without Git). The call is anchored (`--repo` plus `--attestations-dir`) when the output directory is inside the Git repository, and runs with `--no-anchor` (verification with `--no-git`) plus a recorded limitation when it is outside |
| `compare` | yes | a second run of the same document compares as `exact_match` or `tolerance_match` |

## Outcome and exit codes

- `pass` — every required step passed and no optional step was skipped;
- `partial` — every required step passed, but at least one optional step was
  skipped, or a step recorded a limitation;
- `fail` — at least one required step failed.

Exit codes: `0` pass, `2` partial, `1` fail. `partial` is not a pass.

Several limitations are expected on a normal development machine; each makes the
outcome `partial` without failing a step:

| Limitation | Why it is not a failure |
| --- | --- |
| `environment` supports the interpreter but does not reproduce the verified pin set | the declared range is supported; only the exact release toolchain is a narrower statement (`docs/28`) |
| `compare` returns `unknown` because the source worktree is dirty | `compare-runs` refuses a reproducibility verdict from an uncommitted tree; the two runs still completed |
| `audit` reports `L1_pending` | the attestation record is written but not yet committed, so no anchor exists until a human commits it; anchoring is a separate act |
| `audit` runs `attest-run --no-anchor` because the output directory is outside the repository | the anchoring rule requires the attestations directory inside the repository, while `--output` may live anywhere; the step records `anchor=disabled reason=attestations_dir_outside_repository` and a limitation, so the outcome is `partial` |

Run the check from a clean tree with the exact toolchain to remove the first two.
The audit limitations are inherent to a check that must not write into Git: an
in-repository output directory leaves the attestation unanchored until a human
commits it, and an output directory outside the repository is written with
`--no-anchor` by design. A full `pass` therefore requires the operator to commit
the attestation and re-verify.

A `fail` in `audit` now means `attest-run` or `verify-attestation` genuinely
errored (a rejected or conflicting attestation, an unreadable package, a
verification status other than `ok`). The location of the output directory is
no longer a reason to fail: it selects the anchored or the unanchored call.

## Boundaries

- Writes only under the output directory (`--output`, default
  `.tmp/full-flow/<timestamp>`), which is Git-ignored. It never edits source.
- The output directory may live anywhere. Inside the repository the audit step
  anchors its attestation; outside it the step calls `attest-run --no-anchor`
  with an explicit limitation instead of failing.
- Installs nothing, contacts no network, and never modifies a published package.
- Runs `formal_comparison` nowhere: every run is `exploratory`, so the check is
  usable from a dirty development tree.
- ngspice is not exercised unless `--ngspice <path>` is supplied. When it is, the
  backend environment step becomes required and the exact executable version and
  digest are recorded.
- Passing this check says the documented flow works. It says nothing about
  physics, numerics, safety, hardware, or release readiness.

## Acceptance

```powershell
python scripts/full_flow_check.py
python scripts/full_flow_check.py --output .tmp/flow-1 --json
```

Covered by `tests/test_full_flow_check.py`, which runs the check in temporary
directories and asserts the outcome, the step order, and the exit-code mapping.
The audit step is exercised from both sides of the Git boundary: an output
directory created with `tempfile.mkdtemp()` outside the repository (the
attestation must be written with `--no-anchor` and the evidence must say
`anchor=disabled`), and one under `.tmp/` inside the repository (the anchored
call must be used). Neither run may fail the audit step.

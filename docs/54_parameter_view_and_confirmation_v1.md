# Parameter View and Confirmation Record (v1)

**Status:** product-phase contract (P2). Read-only projection of an already
published, integrity-verified run package, plus a detached confirmation record
bound to the parameter-set digest.

This document adds no simulation, physical, or engineering claim. A
confirmation recorded here is accountability for "these are the parameters I
reviewed", not an approval of results, not a safety review, and not a
signature.

## Problem

A user who does not read source could already run, view, and verify
(`docs/48`), and could read a single claim conclusion (`docs/51`). What was
missing was the middle step: **seeing the numbers that produced the result**.
The manifest records them, but only as a nested structure that assumes the
reader knows which field means what. And nothing recorded that a human had
looked at those numbers at all.

`docs/52` §E3 named this gap for the change-control phase. This slice delivers
the read-only view and the confirmation record; the change-control report (E1)
and the formal-run gate (E2) remain future work and are independent.

## Three documents

| Document | Schema | Written by | Written where |
| --- | --- | --- | --- |
| parameter view | `pe-sim.parameter-view.v1` | the reader, on demand | never written to disk by the package |
| parameter confirmation | `pe-sim.parameter-confirmation.v1` | the human, via `confirm-parameters` | outside the run package |
| confirmation verification | `pe-sim.parameter-confirmation-verification.v1` | the reader, on demand | never written |

The confinement is deliberate: a run package is immutable after publication
(`docs/02`), so the view is a projection and the confirmation is a detached
sibling artifact, exactly like an attestation (`docs/40`).

## Where the numbers come from

Every value in the view is read from the published `manifest.json`. The reader
never imports a Plant, never re-executes a model, and never re-derives a
physical quantity.

| Group | Manifest source |
| --- | --- |
| `plant` | `plant.identity.parameters`, `plant.identity` (id, kind, level, hash) |
| `controller` | `controller.identity.parameters`, plus `controller.run_configuration` or `controller.identity.run_configuration` (the recorded effective configuration wins on a name collision) |
| `timebase` | `timebase` |
| `solver:<backend name>` | one group per `environment.backends[]`: `name`, `version`, `status`, `solver_settings` |
| `action_policy` | `action_policy.identity` (name, module, minimum, maximum) |

Per-parameter `unit`, `source`, and `review_status` are filled in **only** when
a recorded applicability finding names the parameter
(`applicability.checks.findings[*].evidence` with a `parameter` key). Otherwise
the field is `null` and `provenance_source` is `"not_recorded"`. The reader
does not guess a unit from a name suffix.

`plant.provenance` reports the recorded
`applicability.parameter_provenance_status` and
`applicability.parameter_provenance_sha256`; the records themselves are not
stored in a 0.2 manifest, so `status` is reported as recorded and never
recomputed.

## `pe-sim.parameter-view.v1`

```json
{
  "schema_version": "pe-sim.parameter-view.v1",
  "rule_version": "parameter-view-v1",
  "run": {
    "run_id": "buck",
    "run_mode": "formal_comparison",
    "status": "QUALIFIED",
    "evidence_level": "limited",
    "qualification_level": "partial"
  },
  "package": {
    "manifest_sha256": "<computed>",
    "package_sha256": "<computed>",
    "source_commit": "<40 hex>",
    "working_tree_status": "clean",
    "compatibility": {"status": "current", "reader_schema": "0.2", "package_schema": "0.2"}
  },
  "parameter_set": {
    "digest": "<sha256>",
    "groups": [
      {
        "id": "plant",
        "component": {"id": "buck", "kind": "ideal_averaged_reference", "level": "L1",
                      "identity_sha256": "<hex>"},
        "provenance": {"status": "declared", "sha256": "<hex>"},
        "parameters": [
          {"name": "input_voltage_v", "value": 12.0, "unit": "V",
           "source": "assumption", "review_status": "unreviewed",
           "provenance_source": "applicability_finding", "range": null}
        ]
      },
      {
        "id": "solver:pe_sim.ideal_averaged_python",
        "component": {"name": "pe_sim.ideal_averaged_python", "version": "reference-v1",
                      "status": "known"},
        "provenance": null,
        "parameters": [
          {"name": "integrator", "value": "rk4", "unit": null,
           "source": null, "review_status": null,
           "provenance_source": "not_recorded", "range": null}
        ]
      }
    ],
    "bounds": [{"name": "duty", "minimum": 0.0, "maximum": 1.0, "source": "action_policy"}],
    "counts": {"groups": 5, "parameters": 17, "without_unit": 6, "without_source": 6}
  },
  "qualification": {"level": "partial", "finding_count": 3,
                    "categories": ["duty_varying", "steady_state_unassessed"]},
  "claim": {"schema": "pe-sim.claim.v1", "conclusion": "bounded", "...": "..."},
  "limitations": ["..."]
}
```

Rules:

- `groups` appears in the fixed order `plant`, `controller`, `timebase`,
  `action_policy`, followed by one `solver:<backend name>` group per recorded
  backend, sorted by backend name. A group whose source is absent from the
  manifest is **omitted**, not emitted empty.
- `parameters` inside a group are sorted by `name`. Values are copied exactly as
  recorded, without rounding. A recorded value that is not a JSON scalar is a
  read error, not a silently dropped parameter.
- `range` is `null` for every plant parameter in this release. No parameter
  range is declared anywhere in a 0.2 manifest, so the view must not invent one.
  `bounds` lists only declared bounds, which today come from the action policy.
- `claim` is the `pe-sim.claim.v1` payload computed by the same code path as
  `inspect-run`, embedded verbatim so that "parameters" and "what may I claim"
  are read together and cannot drift.
- `limitations` is non-empty and names the projection boundary.

### `parameter_set.digest`

The digest identifies **the parameter set of one recorded run**:

```text
sha256( canonical_json( {
  "groups": [ {"id", "component_id", "parameters": [{"name","value","unit"}]} ],
  "bounds": [ {"name", "minimum", "maximum"} ]
} ) )
```

`component_id` is the group component's `id` for `plant`/`controller`, the
backend `name` for a `solver:<name>` group, the policy `name` for
`action_policy`, and `null` for `timebase`. Sorting follows the group and
parameter order above. Review status, source, and provenance are excluded: they
describe where a number came from, not which number was used.

## `pe-sim.parameter-confirmation.v1`

```json
{
  "schema_version": "pe-sim.parameter-confirmation.v1",
  "rule_version": "parameter-confirmation-v1",
  "run": {"run_id": "buck", "manifest_sha256": "<hex>", "package_sha256": "<hex>"},
  "parameter_set": {
    "digest": "<hex>",
    "rows": [{"group": "plant", "name": "input_voltage_v", "value": 12.0, "unit": "V"}],
    "bounds": [{"name": "duty", "minimum": 0.0, "maximum": 1.0}]
  },
  "review": {
    "reviewer": "<name>",
    "confirmed_at_utc": "2026-10-25T00:00:00Z",
    "statement": "<what was reviewed>",
    "scope": ["plant", "controller", "timebase", "solver", "action_policy"],
    "acknowledged_limitations": ["<limitations that were on screen>"]
  },
  "claim_conclusion": "bounded",
  "limitations": ["..."],
  "confirmation_digest": "<sha256 of the canonical document without this field>"
}
```

Rules:

- The full parameter rows are embedded. That is what makes a later `stale`
  verdict able to name **which value changed** instead of only reporting a hash
  mismatch.
- `confirmed_at_utc` defaults to the current UTC time; `--timestamp` overrides
  it for reproducible tests.
- `scope` must cover every group present in the view at confirmation time.
- `confirm-parameters` **refuses** when:
  - the `CI` environment variable is set to anything other than empty, `0`, or
    `false` — a confirmation is a human action;
  - `--reviewer` is empty or `--statement` is shorter than 10 characters;
  - `--output` resolves inside the run package directory — publication is
    immutable;
  - the run package fails integrity verification.
- `claim_conclusion` copies the conclusion that was on screen. It is a record of
  what the reviewer saw, not evidence that the conclusion is correct.

## Verification

```json
{
  "schema_version": "pe-sim.parameter-confirmation-verification.v1",
  "run_id": "buck",
  "confirmation_path": "<path>",
  "status": "current",
  "checks": [{"id": "schema", "status": "pass", "detail": "..."}],
  "parameter_set_digest": {"confirmed": "<hex>", "current": "<hex>"},
  "differences": [],
  "limitations": ["..."]
}
```

Checks: `schema`, `confirmation-digest`, `run-identity` (`run_id` and both
package hashes), `parameter-set-digest`, `scope-coverage`, `claim-recorded`.

| `status` | Meaning |
| --- | --- |
| `current` | every check passed; the confirmation describes this package |
| `stale` | the same run, but its package bytes or its parameter set differ from what was confirmed |
| `mismatched_run` | the confirmation names a different `run_id` |
| `invalid` | schema or digest is broken, or required fields are missing |
| `unknown` | the package could not be verified, so no verdict is possible |

`stale` is decided by the parameter-set digest **or** the recorded package
hashes. A re-run with a changed parameter necessarily changes both, and a
re-run with identical parameters changes only the package hashes; both make an
earlier confirmation stop applying. Only a different `run_id` is
`mismatched_run`.

`differences` is populated for `stale` and lists `{group, name, confirmed,
current}` for each changed, added, or removed parameter. It is empty otherwise.

Exit codes: `0` current, `1` any other status, `2` unknown. Both `1` and `2`
mean "do not treat this confirmation as covering the run".

## Read-only service route

`GET /v1/runs/<run_id>/parameters` returns the parameter view for a verified,
published package. The service remains GET-only, loopback, and read-only; the
route cannot write, and the confirmation is never stored under the runs root
that the service reads.

`GET /v1/discovery` gains an `inspection_schemas` object:

```json
"inspection_schemas": {"parameters": "pe-sim.parameter-view.v1",
                       "claim": "pe-sim.claim.v1"}
```

The three visualization contracts (`topology`, `trace`, `outputs`) and
`contract_schemas` are unchanged. The parameter view is an inspection surface,
not a fourth visualization contract.

## Non-claims

- No parameter range, physical validity, model accuracy, safety margin, or
  engineering suitability is asserted. `range` is `null` and `bounds` contains
  only declared action limits.
- A confirmation is not an approval of a result, a release, or a downstream
  use; it does not change `evidence_level`, `qualification_level`, or the claim
  conclusion.
- A confirmation is not tamper-proof and not signed. Anyone can write the same
  JSON. Its value is that it binds one reviewer's statement to one parameter-set
  digest and one package identity.
- The view does not read source, instantiate a Plant, or execute a solver.
- Units and sources are reported only where the package recorded them. The
  reader does not infer them from field names.

## Open decisions

| ID | Question | Current behaviour |
| --- | --- | --- |
| PV-1 | Should a future manifest persist the full `ParameterProvenance.records`? | Yes, that would make units and sources complete. Deferred because it changes the manifest schema and the 0.2 reader. |
| PV-2 | Should `confirm-parameters` be required before a formal run? | No. `docs/52` E2/E3 open decision 4 still governs; this slice only records confirmations. |
| PV-3 | Should the confirmation be Git-anchored like an attestation? | Not in v1. The file can be committed by the user; no anchor is claimed. |
| PV-4 | Should `stale` block `compare-runs` or `inspect-run`? | No. A stale confirmation is reported; it does not alter any other verdict. |
| PV-5 | Should the view expose raw applicability findings? | Only the per-parameter annotations. The full findings stay in the manifest and `inspect-run`. |

## Acceptance

```text
pe-sim parameter-view <run> --compact
pe-sim confirm-parameters <run> --reviewer <name> --statement <text> --output <outside path>
pe-sim verify-parameter-confirmation <run> --confirmation <path> --compact
curl http://127.0.0.1:8765/v1/runs/<run>/parameters
```

Covered by `tests/test_parameter_view.py`,
`tests/test_parameter_confirmation.py`, `tests/test_parameter_cli.py`, and the
service-route tests in `tests/test_visualization_service.py`.

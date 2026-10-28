# Error and Rejection Codes v1

**Status:** binding for the CLI surface shipped in 0.5.7. Python names are
**experimental**; the JSON schemas are versioned and additive-only within v1.

**Purpose.** Phase B item 4 of `docs/43_kernel_roadmap_v1.md`: a client must
be able to branch on a stable code instead of matching human-readable text.

## 1. Two vocabularies, two lifetimes

| Situation | What exists | Vocabulary |
| --- | --- | --- |
| The request is refused before a run starts | no package | **rejection codes** |
| A run starts and then fails | a published package | **failure categories** |

They are deliberately separate. A rejection has no evidence to price; a
failure is evidence with a recorded cause, and its category already lives in
`manifest["failure"]["category"]`.

## 2. Rejection envelope (`pe-sim.rejection.v1`)

Every CLI refusal prints exactly one JSON object on stdout:

```json
{
  "schema": "pe-sim.rejection.v1",
  "status": "CONFIG_REJECTED",
  "error_code": "unknown_plant",
  "error": "plant_id 'no-such-plant' is not registered; ..."
}
```

Rules:

- Branch on `error_code`; `error` is human text and may change.
- A structured `invalid_request` may also carry `validation` and `issues`
  (§2.1). These are additional keys, not new rejection codes.
- `status` keeps the pre-0.5.7 value (`CONFIG_REJECTED`,
  `FORMAL_COMPARISON_REJECTED`, `RUN_INSPECTION_REJECTED`,
  `UNKNOWN_BACKEND`, `USAGE`), so existing consumers keep working.
- Exit code remains `2`. No package is created.
- A run outcome is **not** a rejection: `INCOMPLETE` is exit `1` and has a
  `pe-sim.run-result.v1` payload.

| Code | Meaning |
| --- | --- |
| `invalid_request` | The request, configuration, options, or adapter shape was rejected. `error` says which. |
| `unknown_plant` | `plant_id` is not registered, including after entry-point discovery. |
| `backend_mismatch` | The requested `--backend` is not the backend declared by the plant registration. |
| `unknown_backend` | A backend name is not known to the backend registry. |
| `formal_comparison_rejected` | A formal comparison was refused for missing or dirty provenance; `worktree_analysis` is included. |
| `run_inspection_rejected` | A run package cannot be inspected at all. |

Unknown codes are forward-compatible additions: a client must treat an
unrecognized `error_code` as a refusal (never as success) and fall back to
`status`.

`TypeError` from an adapter or plugin protocol violation is part of
`invalid_request`; it no longer escapes as a traceback.

### 2.1 Structured spec issues on `invalid_request`

Since 0.19.0 a refused `run` may carry two optional keys in the same
`CONFIG_REJECTED` envelope when the experiment document itself has structured
problems:

| Key | Value |
| --- | --- |
| `validation` | the schema tag `pe-sim.spec-validation.v1` |
| `issues` | an array of `SpecIssue` records |

Each `SpecIssue` has `code`, `path`, `message`, `expected`, `suggestion`, and
`severity` (`error` or `warning`). `path` is an RFC 6901 JSON Pointer into the
submitted document; `""` is the document root, `/plant_config/vin` is one
`plant_config` field, and `/input_schedule/0/vinn_v` is one field of the first
schedule entry. `expected` and `suggestion` may be null: the first says what
the field should look like, the second says what the caller probably meant.

**`issues[].code` is not a rejection code.** It comes from the closed spec
vocabulary `pe_sim.spec_validation.SPEC_ISSUE_CODES`:
`invalid_document`, `schema_violation`, `unknown_plant`, `unknown_parameter`,
`invalid_parameter_value`, `unknown_schedule_key`, `value_suggestion`, and
`exploratory_contract_default`. Do not mix it with `REQUEST_REJECTION_CODES`.
Branch on `error_code` first; use `issues[].code` only to explain and repair a
document. `exploratory_contract_default` is a warning, not an error, and does
not make `ok` false.

**`REQUEST_REJECTION_CODES` did not change in 0.19.0.** The refusal is still
`invalid_request`, and no new rejection code was added.

`pe-sim validate` prints the `pe-sim.spec-validation.v1` envelope directly
(with `ok`, `error_count`, `warning_count`, `issues`, and `checked`) instead of
a rejection envelope; exit code 0 means "no error-level issue". The two
surfaces carry the same issue records, so an agent can repair a document with
either one. See `docs/63_agent_facing_spec_validation_v1.md`.

## 3. Failure categories

`manifest["failure"]["category"]` is the authoritative record. The CLI copies
it to `pe-sim.run-result.v1.failure_category` (null when the run did not
fail), and the `run.finished` event carries the same value as
`failure_category`.

`pe_sim.error_codes.KERNEL_FAILURE_CODES` documents the set the kernel and
its built-in checks produce. It is a documentation set, not a gate: see the
closing note in this section.

One code was added in 0.5.10: `trace_contract_violation`. A whole-run engine
declares a `TraceContract` and then returns a trace; when the trace does not
satisfy it, the kernel records that category and keeps the validator problems
verbatim under `logs/run.json` and the corresponding event. It is the only
whole-run-specific failure the kernel itself decides.

| Group | Codes |
| --- | --- |
| lifecycle | `cancelled`, `interrupted` |
| backend | `backend_timeout`, `backend_process_failure`, `simulation_execution_error` |
| numerical / model | `numerical_nonconvergence`, `model_numerical_error` |
| plant / timing | `plant_advance_failure`, `plant_execution_failure`, `timing_violation`, `timestamp_violation` |
| whole-run route | `trace_contract_violation` |
| observation / action | `nonfinite_observation`, `invalid_observation_age`, `unit_mismatch`, `action_invalid` |
| built-in validity findings | `missing_measurement`, `invalid_measurement`, `measurement_out_of_range`, `future_observation`, `stale_observation` |
| built-in applicability findings | `envelope_undeclared`, `envelope_declaration_invalid`, `external_input_changed`, `actuator_undeclared`, `actuator_outside_envelope`, `invalid_schedule_value`, `control_period_too_short`, `insufficient_sample_resolution`, `integration_step_too_large`, `insufficient_integration_substeps`, `load_too_close_to_switch_off_resistance`, `parameter_provenance_invalid`, `parameter_provenance_unknown`, `parameter_unreviewed`, `ccm_boundary_violated`, `pulse_width_below_template_resolution` |
| checks | `check_failed`, `safety_violation`, `data_invalid`, `qualification_failure`, `checker_error` |
| applicability | `out_of_envelope` |
| capabilities | `capability_mismatch`, `missing_capability`, `unsupported_capability`, `capability_configuration_mismatch`, `capability_runtime_unavailable` |
| fallback | `runtime_error` |

**This is not a whitelist.** An adapter or plugin may declare its own
exception or finding `category`, and the value is recorded unchanged. A client
must treat a category outside this set as *unverified*: recorded as given, not
classified by the kernel. The kernel never rejects a run merely because an
adapter used an unfamiliar category.

When a check report contains both advisory and blocking findings, the failure
category is taken from the **first blocking** finding, not from an earlier
warning. The full finding list is still recorded under `failure.findings`.

## 4. Evidence-reading views

The three read-only views now carry their own schema tags, so "read evidence"
does not require guessing a payload shape:

| Command | Schema |
| --- | --- |
| `inspect-run` | `pe-sim.run-inspection.v1` |
| `verify-attestation` | `pe-sim.attestation-verification.v1` |
| `attest-run` | `pe-sim.attestation-write.v1` |

These tags label the **view**. The authoritative evidence remains the
published package plus its manifest; a view can be regenerated at any time and
is never written into the package.

Since 0.5.9, `inspect-run` also reports `compatibility` (top level in the CLI
JSON, `summary["compatibility"]` on the Python object)
(`current` / `read_only` / `incompatible`, with `may_rewrite: false`). That is a
read-with-policy verdict, not an integrity claim; see
`docs/47_compatibility_policy_v1.md`.

Since 0.5.13, `inspect-run` also copies the recorded `evidence_level` and
`qualification_level` from the package. Both are read from the manifest, not
recomputed, so a reader can compare the view with the terminal result the
producer printed. A view never upgrades a level. Since schema `0.2` the
qualification level is presented in the current vocabulary
(`assessed` / `partial` / `failed`) and an older package also reports a
`migration` block naming the reader-side mapping
(`docs/50_evidence_vocabulary_v2.md`).

## 5. Versioning

- A new rejection code or failure category may be added within v1; clients
  must tolerate unknown values as described above.
- Renaming or removing a code, or changing its meaning, requires a v2 tag.
- The envelope keys `schema`, `status`, `error_code`, `error` are fixed for
  v1. Additional keys may appear; `validation` and `issues` on
  `invalid_request` (§2.1) are such additions.

## 6. Non-goals

No HTTP status mapping, no stack traces, no exception class names as codes,
no localization, and no attempt to replace `inspect-run`/`verify-attestation`
as the authority for evidence.

## 7. Acceptance evidence

`tests/test_error_codes.py` covers every rejection code, the terminal
`failure_category`, the `TypeError` boundary, and vocabulary invariants.
`tests/test_inspection.py` and `tests/test_attestation.py` cover the three
view schema tags.

## 8. Related documents

| Document | Relationship |
| --- | --- |
| `docs/44_run_interaction_contract_v1.md` | request / progress / terminal-result contract |
| `docs/43_kernel_roadmap_v1.md` | Phase B and T3 |
| `docs/02_run_artifacts_and_state_machine_v0.md` | failure records and `INCOMPLETE` |

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

## 3. Failure categories

`manifest["failure"]["category"]` is the authoritative record. The CLI copies
it to `pe-sim.run-result.v1.failure_category` (null when the run did not
fail), and the `run.finished` event carries the same value as
`failure_category`.

`pe_sim.error_codes.KERNEL_FAILURE_CODES` documents the set the kernel and
its built-in checks produce:

| Group | Codes |
| --- | --- |
| lifecycle | `cancelled`, `interrupted` |
| backend | `backend_timeout`, `backend_process_failure`, `simulation_execution_error` |
| numerical / model | `numerical_nonconvergence`, `model_numerical_error` |
| plant / timing | `plant_advance_failure`, `plant_execution_failure`, `timing_violation`, `timestamp_violation` |
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

## 5. Versioning

- A new rejection code or failure category may be added within v1; clients
  must tolerate unknown values as described above.
- Renaming or removing a code, or changing its meaning, requires a v2 tag.
- The envelope keys `schema`, `status`, `error_code`, `error` are fixed for
  v1. Additional keys may appear.

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

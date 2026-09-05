# Stage 8: Failure and Recovery Semantics (v1)

Stage 8 makes a failed or interrupted run impossible to mistake for a
successful run. It covers the Python and file-transaction boundary only; it
does not claim operating-system crash recovery or physical-model recovery.

## Lifecycle contract

The public `LifecycleStateMachine.allowed_transitions()` matrix is the single
source of truth:

```text
CREATED -> RUNNING | RUN_FAILED | INCOMPLETE
RUNNING -> RUN_OK | RUN_FAILED | INCOMPLETE
RUN_OK -> QUALIFIED | DISQUALIFIED
QUALIFIED -> COMPARABLE | LEARNING_ELIGIBLE
LEARNING_ELIGIBLE -> LEARNING_UPDATED | LEARNING_REJECTED
```

The terminal states are `RUN_FAILED`, `INCOMPLETE`, `DISQUALIFIED`,
`COMPARABLE`, `LEARNING_UPDATED`, and `LEARNING_REJECTED`. Any other transition
raises `LifecycleTransitionError`; a caller cannot bypass the matrix by
writing a status string. Every accepted transition records sequence, reason,
step/time context, and the lifecycle rule version. The same matrix is copied
into `manifest.json` as `state_transition_matrix`, alongside the observed
`state_transitions` list.

`RUN_FAILED` means execution or validation failed and should not be used as a
complete result. `INCOMPLETE` means execution was interrupted, cancelled, or
the writer was abandoned before a normal terminal result; it is eligible for
resume only when a validated checkpoint exists. `DISQUALIFIED` means execution
completed but qualification rules rejected the samples. None of these states
is a successful or learning-eligible result.

## Transaction and abandoned-run handling

`ArtifactWriter` writes each file through a flush/fsync and atomic replacement
inside a hidden temporary directory. Publication is a directory-level atomic
rename after required-artifact validation. A `.run-state.json` marker records
the writer phase. `scan_abandoned_runs(output_dir)` reports leftover writer
directories as `INCOMPLETE`, lists missing required artifacts, and leaves them
untouched for evidence review. It never promotes partial files to `RUN_OK`.

The scan is included in the next run's `manifest.json` and `events.json`. The
helper `recover_abandoned_runs()` is intentionally non-destructive; cleanup is
a deliberate operator action after evidence preservation.

## Failure categories and recovery evidence

The runner distinguishes `interrupted` (Python interruption), `cancelled`
(cooperative cancellation), `backend_timeout`, `backend_process_failure`,
`numerical_nonconvergence`, and other execution categories. Interruption and
cancellation are published as `INCOMPLETE`; backend and validation failures
remain `RUN_FAILED`. A checkpoint is attempted for recoverable interruption or
cancellation, and checkpoint failure is itself recorded in the failure/event
evidence.

`resume_from` accepts an interrupted/failed run directory or checkpoint. The
checkpoint hash, component hashes, contract hashes, bounds, and basic sample
shape are validated before restore. A source directory with `RUN_OK` or another
terminal success state is rejected. Resume always publishes a new run and
records source run name, source status, checkpoint hash, and a resume event;
the old run is never rewritten as successful.

## Deliberate limits

This increment does not monitor external processes, recover machine power loss,
or infer a simulator's internal solver state. A backend adapter must provide
its own logs and checkpointable state for stronger recovery guarantees. The
`result_retention` field remains an explicit configuration contract, but
automatic deletion/archiving is deferred until an operator-approved retention
policy is defined; failed and incomplete evidence is never removed by this
increment.

## Verification

```text
pytest -q --basetemp .tmp/pytest-stage8
python -m compileall -q src tests
git diff --check
```

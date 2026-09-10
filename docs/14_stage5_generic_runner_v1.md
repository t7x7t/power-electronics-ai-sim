# Stage 5: Generic Runner v1 (audited lifecycle)

This increment strengthens the minimal Runner without claiming a physical
simulation engine or process-level recovery service.

## Implemented contract

- Every run records `CREATED -> RUNNING -> RUN_OK -> QUALIFIED` (or
  `DISQUALIFIED`), `RUN_FAILED`, or `INCOMPLETE` in `state_transitions.json`;
  `RUN_OK` means execution ended normally, while `QUALIFIED` is the normal
  successful result after post-run checks. Each transition has a reason,
  logical step, timestamp, and rule version.
- `audit.json` records Plant, Controller, Safety, Qualification, and checkpoint
  calls with component, method, step, time, order, success, and exception data.
- `checkpoint.json` stores experiment/component identity, logical time, next
  step, Plant snapshot, Controller state, samples, events, and the last action.
  `Runner.run(..., interrupt_after_steps=N)` provides a deterministic test hook;
  an interrupted run is `INCOMPLETE`, never `RUN_OK` or `RUN_FAILED`.
- `resume_from=<run directory or checkpoint>` restores the checkpoint and
  continues at the recorded step. Component identity is checked before resume.
- Capability negotiation failures retain the `ValueError` compatibility but
  expose `category=missing_capability` and a structured `failure` Manifest
  object.
- The primary Controller measurement is configurable through the `measurement_key`
  argument or `plant_config.primary_measurement`; the legacy `vout` default
  remains for existing fixtures.
- `sample_offset_s` is implemented as a pre-sample hold using the previous
  action. It must satisfy `0 <= sample_offset_s < control_period_s` so every
  control window retains time for an action. `plant_step_s` segments each
  plant advance. Action target timestamps are validated and applied within the
  current control window. If `duration_s` is not an integer number of control
  periods, the final window is shortened to end exactly at the configured
  duration; the runner never advances beyond that deadline.

## Deliberate limits

This is deterministic Python-level evidence, not a claim of OS/process crash
recovery, real-time guarantees, complete event scheduling, or hardware safety.
Controllers without `snapshot()` are reset from the serialized state on resume;
controllers with richer internal state should implement and test explicit
snapshot/restore methods. Full event queues, unit registries, configurable
action contracts, and cross-process recovery remain later work.

## Verification

The repository test suite covers the legacy behavior plus state transitions,
call counts/order, checkpoint artifacts, interrupted/resumed runs, and a Plant
whose primary measurement is not named `vout`.

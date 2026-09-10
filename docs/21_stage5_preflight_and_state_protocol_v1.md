# Stage 5 Runtime Preflight and State Protocol (v1)

This increment closes the most important simulation-before and simulation-during
boundaries without turning the generic runner into a domain-specific simulator.

## Preflight

`run_id` and `experiment_id` are identifiers, not arbitrary filesystem paths.
They use the safe token grammar from the experiment schema and reject path
separators, absolute paths, `.`/`..`, and parent traversal. The requested output
directory is checked before Plant or Controller reset. An existing final run
directory or stale temporary directory for the same `run_id` is a conflict;
the runner never waits until publication to discover it.

Exploratory runs retain the existing dirty-worktree behavior. Formal comparison
additionally requires:

- an explicit random seed;
- non-placeholder safety and qualification contract hashes;
- a complete source commit and clean worktree;
- non-empty Plant and Controller identity hashes;
- known environment and backend provenance, including solver settings.

These are evidence gates, not claims that a model is physically correct.

## Timing boundary

Every Plant advance segment is checked against its requested endpoint. A Plant
that advances only part of a requested window is rejected as
`plant_advance_failure`; a result cannot silently acquire a drifting time axis.
Fractional final control windows remain supported and end at the configured
experiment deadline.

## Checkpoint identity

Each checkpoint records an execution-semantic `experiment_spec_hash`. The hash
binds schema, experiment identity, component/configuration references, timebase,
initial state, input schedule, seed, contracts, and required capabilities while
excluding run naming and output retention metadata. A checkpoint from a changed
timebase, schedule, seed, contract, or initial-state definition is rejected.

Controller state uses one of two explicit protocols:

1. `snapshot_restore`: the Controller supplies both `snapshot()` and
   `restore(mapping)`; the snapshot is serialized into the checkpoint.
2. `reset_serialized_state`: the Controller returns a serializable mapping from
   `reset()`, and that mapping is passed back to `reset()` on resume. Controllers
   with hidden mutable state must implement the first protocol. An ActionRequest
   may provide `state_update` to advance the serialized mapping after `observe()`.

The protocol and current experiment hash are validated before a resume can call
Plant or Controller restore/reset.

## Diagnostics versus blocking checks

Check findings retain severity and evidence. A warning or informational finding
with `stop_requested=false` is recorded but does not stop execution. Error and
fatal findings remain fail-closed; a plugin may also request a stop explicitly.
This separates useful diagnostics from safety, validity, and qualification
failures that must terminate a run.

These mechanisms are Python-level evidence controls. They do not provide
operating-system crash recovery, external Ngspice supervision, hardware safety,
or product-level model validation.

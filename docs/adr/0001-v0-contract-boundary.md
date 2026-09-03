# ADR 0001: v0 Contract Boundary

## Status

Accepted for the first implementation slice.

## Decision

The public runtime uses serializable `ExperimentSpec`, `PlantObservation`, and
`ActionRequest` objects. Plant truth is retained for audit only and is never
passed to the controller. Runs are published atomically and carry separate
qualification, safety, status, and artifact hashes in `manifest.json`.

The v0 schemas require `run_id`, explicit `contracts` references, and explicit
output retention policy. The old draft fields remain available only where the
schema already permits extension; incompatible future changes require a major
schema version and migration note.

## Consequences

The fake backend is the acceptance fixture, not a physical PSFB model. Real
PySpice migration starts only after Gate 1/2 contract and artifact tests pass.

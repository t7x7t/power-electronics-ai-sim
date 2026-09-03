# Stage 3 public contracts (incremental v1)

This document records the small extension built on the v0 contracts. It is
an interface and data-integrity contract, not a claim that a plant model is
physically valid.

## Capability declaration and negotiation

`PlantAdapter.capabilities()` may continue returning the v0 `set[str]`. A
component may instead return `CapabilitySet` or a mapping with `names` (or
`features`), optional `event_types`, `control_period_s`, and
`observation_period_s`. `negotiate_capabilities()` normalizes either form.

Requirements are explicit in `ExperimentSpec.required_capabilities`:

- `plant:name` and `controller:name` target one component;
- an unqualified name may be provided by either component;
- an input schedule containing `event` requires Plant `events` or
  `event_driven`;
- a distinct `plant_step_s` requires Plant `continuous_time` or
  `multirate_control`.

Missing capabilities cause a deterministic, fail-closed error. A legacy
controller without `capabilities()` remains valid when no controller
capability is required.

## Time and visibility

All times remain seconds. `event_tolerance_s` documents the comparison
tolerance for future event work. Runner rejects Plant observations that regress
and rejects an advance that exceeds the requested control window. Measurement
values remain the only data passed to a controller; truth is audit-only.

## Contract versions

The reader currently supports schema `0.1` and older `0.x` minor versions.
Future minor versions and every new major version are rejected until an
explicit migration is implemented. This prevents silently ignoring fields
that could change experiment meaning.

## Snapshots

`qualified_snapshot` now verifies the SHA-256 of the referenced file before
calling `PlantAdapter.restore()`. A changed or malformed snapshot therefore
fails before simulation steps are executed. Snapshot contents and model
compatibility remain the responsibility of the model owner; cross-version
physical validity is not inferred by this runtime.

## Migration notes

Existing v0 specs need no changes. To opt into a requirement, add
`required_capabilities` to the spec and implement the corresponding capability
on the component. Existing `set[str]` declarations and controllers without a
capability method remain supported. A future schema requires a deliberate
reader update and tests before it can run.

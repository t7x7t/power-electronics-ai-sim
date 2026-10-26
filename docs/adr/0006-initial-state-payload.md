# ADR 0006: Initial State Carries a Bounded, Verifiable Model State

## Status

Proposed, 2026-10-26. Implements `docs/60` row A6 (pre-biased start). Sequenced as
S3 in `docs/61_gap_closure_plan_v1.md`. Does not change `PlantAdapter`,
`ControllerAdapter`, or the Runner loop.

## Context

A converter that starts with a charged output is an ordinary case: a hot-plug, a
restart without discharge, a multi-rail sequence. The reference plants and the
ngspice netlist can already express it (`reset()` reads `output_voltage_v`, the
netlist takes `IC={VC0}`), but the experiment document cannot carry the value.

`InitialState` has three fields — `mode`, `snapshot_path`, `snapshot_hash` — and
`to_dict()` returns exactly those. The JSON schema allows additional properties,
so a document with `initial_state: {"mode": "warm_start", "vout": 5.0}` passes
schema validation. `ExperimentSpec.from_dict` then calls
`InitialState(**dict(initial))` and raises `TypeError`, or — if the field were
silently dropped instead — the run would start cold and look normal. That is the
silent-gap class `docs/60` §3 exists to eliminate.

## Decision

### 1. Add one optional payload

`InitialState` gains `state: Mapping[str, float] | None = None`.

- Keys must be non-empty, at most 64 characters, and match the existing safe
  identifier grammar (no path separators, no whitespace).
- Values must be finite real numbers; `bool` is rejected (it is an `int` in
  Python and would encode 0/1 by accident).
- At most 16 entries. This is a startup condition, not a state dump; a larger
  payload is a snapshot, which already has its own route.
- The mapping is frozen into a sorted tuple at construction so two documents
  that differ only in key order produce the same digest.

### 2. The plant must echo what it applied

The kernel does not know plant state names (I5). It therefore passes the payload
to `plant.reset()` and requires the returned mapping to contain **every
requested key with the same value**. A key the plant ignores makes the run fail
before the first advance, instead of producing a cold start that looks qualified.

Plants may still accept aliases internally, but the experiment payload must use
the canonical key the plant reports back. `BuckPlant.reset` already returns
`output_voltage_v` / `inductor_current_a`; those are the canonical names.

### 3. Boundaries

- When `state` is absent, `to_dict()` output is byte-identical to today's
  (no `"state": null`), so existing manifests and checkpoint comparisons do not
  change.
- When present, the manifest records the payload and its SHA-256. The value is
  data, not a path: no absolute path can enter the manifest through it.
- The schema gains a documented `state` property. The change is additive; the
  existing schema version remains valid because the property was already
  permitted by `additionalProperties`.
- `mode` keeps its meaning. `state` is legal with `cold_start` and
  `warm_start`; combining it with `qualified_snapshot` is rejected, because a
  snapshot restore and an explicit payload would be two sources of truth.

### 4. Evidence

A pre-biased start does not weaken in-run enforcement: the kernel still owns the
clock, still observes and acts per window. The row is therefore expected at the
route's normal cap (`comparable` for `stepped + kernel`). What changes is only
the initial condition, and it is now recorded rather than assumed.

## Rejected alternatives

- **A new `mode` value (`pre_biased`).** A mode is a branch; the payload is
  data. A mode would also force every consumer that switches on `mode` to learn
  a fourth value.
- **Reuse `snapshot_path`.** A snapshot is a checkpoint produced by this
  project's own run, with a hash that is verified and a resume semantics. A
  startup condition is neither; overloading it would let an arbitrary file be
  mistaken for a qualified checkpoint.
- **Let the plant read `plant_config`.** That would mix a per-run initial
  condition into the model's static configuration, so two runs with different
  starts would look like different plants in the manifest.

## Consequences

- `docs/60` A6 moves from `expressible` to a contract row and closes when the
  run-level test shows the initial voltage in the first observation and rejects
  an ignored key.
- `tests/test_stage3_contracts.py` and the schema tests gain the payload cases;
  documents without the payload keep their current behaviour.
- The payload bounded at 16 entries keeps the run package small and makes the
  "use a snapshot instead" boundary explicit.

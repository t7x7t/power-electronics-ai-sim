# ADR 0004: Solve-Route Capabilities Are Declarations, the Evidence Cap Is Derived

## Status

Accepted, 2026-10-26. Step 1 (declarations, derived cap, manifest reporting) is
implemented in 0.10.1; step 2 (carrier phase and change-point solve segments) is
implemented in 0.13.0 with a maintainer review record for the reviewed solve
path; step 3 is sequenced by `docs/61_gap_closure_plan_v1.md` as S9.

## Context

Two power-electronics requirements that are common in practice are currently not
*expressible*, only avoidable by leaving the tool:

1. give up in-run interception in exchange for one long, high-resolution
   continuous transient;
2. put the compensator inside the model or the netlist instead of in a kernel
   `ControllerAdapter`.

The route axis today is one field, `time_model ∈ {stepped, whole_run}`, and
`pe_sim/time_model.py` already derives everything else from it: `evidence_cap`,
`cap_evidence_level`, `route_limitations`, `formal_comparison_allowed`. That
derivation point is the right seam, but one field cannot express two independent
facts.

Two of the current obstacles are implementation choices, not physics:

- `control_period_s` must be an integer multiple of the switching period,
  because every window netlist restarts the carrier at `t=0`
  (`PULSE(0 1 0 ...)` in `backends/netlist.py`);
- `vin`/`load`/`vref` are resolved once per control window
  (`runtime.py`, `_command_at(spec.input_schedule, current_time)`), so a load
  step inside a window cannot be represented.

The tempting fix is to add `time_model="whole_run_switching"`,
`time_model="stepped_in_netlist_control"`, a `--whole-run` flag on the ngspice
adapter, and a special case in the Runner. That is patch-on-patch: the route
axis becomes the product of independent facts, and every consumer needs one more
branch.

## Decision

### 1. Decompose the route into three orthogonal declarations

On `PlantRegistration`:

| Declaration | Values | Meaning |
| --- | --- | --- |
| `time_model` | `stepped`, `whole_run` (unchanged) | who owns the clock |
| `control_location` | `kernel`, `model` | where the closed loop closes |
| `event_granularity` | `window`, `continuous` | may an external input change between observation points? |

`time_model` keeps its meaning and its values. The two new facts are independent
of it and of each other.

### 2. The evidence cap is a function, not a declaration

| time_model | control_location | cap | what the kernel can still do |
| --- | --- | --- | --- |
| `stepped` | `kernel` | `comparable` | enforce action and observation, per window |
| `stepped` | `model` | `limited` | enforce observation, per window; cannot shape the actuator |
| `whole_run` | `model` | `limited` | post-hoc validation only |
| `whole_run` | `kernel` | **rejected at construction** | contradictory: if the model owns the clock, no per-window actuator exists |

`cap_evidence_level(level, time_model)` and `route_limitations(time_model)` keep
their call shapes for compatibility; the derived form takes the whole capability
record. An adapter still may not certify its own level (invariant I2).

`route_limitations` becomes a function of the record and reports the new codes:

| Combination | Added limitation code |
| --- | --- |
| `control_location = model` | `actuator_control_in_model` |
| `event_granularity = window` | `events_quantized_to_control_windows` |

### 3. Remove the two over-constraints by making the assumption a parameter

- **Carrier phase.** The window netlist takes the segment-start carrier phase,
  `phase = segment_start mod Tsw`, and renders the PULSE delay as
  `(-phase) mod Tsw`.  The delay is the time to the **next** absolute carrier
  start, not the phase itself; using the phase directly only happens to be
  correct when the segment starts at 0 or half a period.  The integer-multiple
  restriction and the `uncheckable_rules` entry that states it are deleted,
  because the assumption is no longer implicit.
- **Solve-segment boundary.** A solve segment ends at the next *change point* of
  `(duty, vin, load, vref)`, not at `max(duration, control_period_s)`.
  `_advance_segments` resolves the external input per segment instead of once
  per control window. This also removes a latent correctness issue: today
  `_solve_window` uses `horizon = max(duration, control_period_s)`, which
  overruns the window end and is only masked by the next cache miss.

Neither change adds a mode: they delete an assumption, so "continuous events" is
a declared capability of a model rather than a special path in the kernel.

### 4. New capabilities are new adapters, never modes of existing ones

A switching whole-run adapter and an in-model-control adapter are separate
registrations that declare their capabilities. `ngspice-buck` gains no flags.
This keeps T1 ("new engine, zero kernel edits") true.

## Anti-patch rules

These are the operative part of this ADR:

1. The route axis is closed. A new capability must be expressible as a
   combination of existing declarations; otherwise it needs its own ADR and an
   argument for why it is not a combination.
2. Exactly one function derives the evidence cap. No consumer may compute a cap
   locally, and no adapter may declare one.
3. Contradictory combinations are rejected at construction, not at run time.
4. Every declaration is reported in the manifest. An undeclared capability is
   treated as absent, not as "probably fine".
5. A capability that costs more is declared, not hidden: number of solves per
   control window is derivable from `event_granularity`.

## Consequences

- `stepped + model` becomes expressible for the first time. It is the only
  combination that keeps *some* interception while letting the model hold the
  compensator, and it is honestly capped at `limited`.
- `whole_run + model` covers the long/high-resolution case at `limited`; the
  route already exists for the averaged model, so only a switching-level adapter
  is missing.
- Removing the integer-multiple restriction changes the reviewed solve path
  (`backends/netlist.py`, `backends/ngspice_plants.py`). The `ngspice-buck` pin
  therefore requires a maintainer review record; a code change without that
  record must fail the identity test.
- `event_granularity = continuous` raises the number of solves proportionally to
  the number of events. That cost becomes visible in the declaration instead of
  being discovered as a runtime surprise.
- After step 2, input resolution happens at change points for every stepped
  route. `event_granularity` therefore no longer means "does the plant see an
  in-window input"; it means the controller observation cadence and the declared
  solve cost. Registrations that still declare `window` keep the limitation code
  as a controller-side description.

## Implementation order

Each step is independently verifiable and each leaves the suite green.

1. **Declarations and derivation, no behaviour change.** Implemented in 0.10.1.
2. **Remove the over-constraints.** Implemented in 0.13.0; the reviewed
   `ngspice-buck` pin carries the review record.  D3's comparator/device events
   remain outside this step and are handled with B3.
3. **New adapters for `control_location = model`**, one per time model, both
   capped at `limited`. Zero kernel edits, per T1.

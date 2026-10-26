# ADR 0007: Controllers Are Registered, Not Enumerated

## Status

Accepted, 2026-10-26. B2 is implemented in 0.14.0; B9 (protection state
machine) and A7 (short-circuit hiccup recovery) are implemented in 0.14.1 as a
controller wrapper over the same registry. Does not change `PlantAdapter`,
`ControllerAdapter`, or the Runner loop.

## Context

The experiment document enumerates two controller ids:

```json
"controller_id": {"enum": ["pi", "pid"]}
```

and fixes the configuration key set in the schema. The CLI mirrors that with a
hardcoded binding table. Adding a compensator therefore means editing the schema,
the CLI, and the controller factory together — the same patch-on-patch pattern
that ADR 0004 removed from the plant side. It also means a third party cannot
ship a controller without editing this repository.

B2 asks for a Type II/III compensator "on the kernel side, discretised per
control period". The compensator is not the hard part; the closed enumeration is.

## Decision

### 1. `controller_id` is an open identifier, resolved by a registry

`controller_id` follows the same safe-identifier grammar as `plant_id`
(letters, digits, `_`, `.`, `-`; no path separators, no `..`). The schema no
longer enumerates ids or owns the configuration key set; `controller_config` is
an object, like `plant_config`.

`pe_sim.controller_registry` mirrors `pe_sim.plant_registry`:

- `ControllerRegistration(controller_id, factory, config_keys, class_name)`;
- `register_controller`, `resolve_controller`, `registered_controllers`,
  `clear_controllers`;
- built-ins are registered by the project composition root
  (`builtin_controllers.py`), not by the registry.

Resolution validates two things before a run directory exists: the id is
registered, and every `controller_config` key is declared by that registration.
An unknown id or an unknown key fails closed. Value validation stays in the
factory (`from_config`), where the semantics live.

### 2. Type II/III are designs over one explicit difference equation

There is one runtime implementation, `DiscreteCompensator` (order up to 3):

```text
y[n] = b0*x[n] + b1*x[n-1] + ... - a1*y[n-1] - a2*y[n-2] - a3*y[n-3]
```

where `x` is `vref - measurement`. The difference equation is the control law;
there is no hidden integrator. Type II is second order; Type III is third order.

`type2` and `type3` compute their coefficients from corner frequencies with a
Tustin mapping at the control period:

- Type II: `G(s) = K (1 + s/wz) / (s (1 + s/wp))`
- Type III: `G(s) = K (1 + s/wz1)(1 + s/wz2) / (s (1 + s/wp1)(1 + s/wp2))`

The computed coefficients are recorded in the controller identity, so a run
states the exact discrete law it executed. `biquad` exposes the coefficients
directly for users who design them elsewhere.

Output limits are optional and paired. When the output saturates, the delay line
stores the applied (clamped) output, so the compensator's memory cannot keep
accumulating an action the actuator never applied.

### 3. Compatibility is a property of the registration, not a special case

`pi` and `pid` remain the same implementation and the same key sets; their
numeric behaviour is pinned by the existing tests. A PID configuration copied
onto `pi` still fails, because the registration declares the keys it accepts.

### 4. The kernel names no controller

`ControllerAdapter` is unchanged. The registry is data; the Runner receives an
object and calls `observe()`. T1-style extension holds for controllers: a new
compensator is a registration plus a factory, not a branch in the kernel.

## Rejected alternatives

- **Add `type2`/`type3` to the enum.** That is the patch this ADR exists to
  avoid: every new compensator edits the schema, the CLI and the factory.
- **A `mode` field on the PI controller.** A compensator is a different control
  law, not a flag on an existing one.
- **Put the coefficients in the schema.** The schema would then enumerate every
  compensator's parameters; the registration already knows them, and the factory
  is the only place that can validate values.

## Consequences

- The schema change is backward-compatible: every document that was valid
  before is still valid. Documents that relied on the schema to reject an
  unknown id now fail at resolution, with a message that lists the registered
  ids.
- Two tests that asserted the closed enum are replaced: one checks the
  identifier grammar, the other checks that resolution (not the schema) rejects
  an unknown id and that the factory still rejects a negative filter constant.
- `docs/60` B2's closure class becomes `contract`, because the schema and the
  resolution rule change; the row cites this ADR.
- B9/A7 build on the same registry: a protection wrapper is another controller
  registration, and fault injection is an `input_schedule` change point, which
  ADR 0004 step 2 already honours.

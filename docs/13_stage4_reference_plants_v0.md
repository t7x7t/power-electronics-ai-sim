# Stage 4 reference Plant adapters

Stage 4 adds two self-owned L1 reference adapters:

- `pe_sim.reference_plants.BuckPlant`
- `pe_sim.reference_plants.BoostPlant`

They are deterministic, parameterized, ideal averaged continuous-conduction
models.  Both implement the public `PlantAdapter` lifecycle (`reset`,
`observe`, `advance`, `snapshot`, and `restore`) and declare support for
`continuous_time`, `external_input`, and `snapshot` capabilities.

## Model contract

For these Buck/Boost models, the control action is a normalized duty ratio in
`[0, 1]`; this is a model-specific declaration, not a universal
`PlantAdapter` requirement.  The exposed
controller measurements are `vout` in volts and `inductor_current` in amps;
internal values such as topology, input voltage, duty, and load resistance are
truth-only audit fields.  `advance()` accepts optional `vin_v`/`input_voltage_v`
and `load_ohm`/`load_resistance_ohm` updates.  Invalid, non-finite, or
non-positive parameters are rejected.

The state equations are the standard ideal averaged equations:

- Buck: `di/dt = (d*Vin - Vout)/L`,
  `dv/dt = (iL - Vout/R)/C`.
- Boost: `di/dt = (Vin - (1-d)*Vout)/L`,
  `dv/dt = ((1-d)*iL - Vout/R)/C`.

A fixed-step RK4 integrator makes results deterministic for a given parameter
set and run timebase.  The integration step is a model parameter and is not an
assertion about a physical solver's required timestep.

## Intended level and operating point

These are L1 reference models for adapter, timing, safety, artifact, and
controller integration tests.  They use a named operating point supplied by
the caller (for example, 12 V input, 100 uH, 470 uF, and 10 ohm load in the
tests).  The values are reproducible test parameters, not recommended product
ratings or design values.

They omit switching waveforms, semiconductor conduction and switching losses,
ESR/DCR, parasitics, magnetics, thermal states, device tolerances, startup
nonlinearities, and experimental calibration.  Passing the conformance tests
does not establish physical validity, hardware safety, efficiency, or product
performance.  A future L2/L3 model must have an independent source,
license/provenance record, technical validation, and a separately stated
applicability range.

## Conformance harness

`pe_sim.conformance.check_plant_adapter()` and
`check_controller_adapter()` provide reusable checks for plugin factories.
They cover lifecycle methods, finite observations, measurement projection,
monotonic time, deterministic snapshot restore, and rejection of non-finite or
out-of-range actions when an adapter-specific `action_bounds` argument is
explicitly supplied.  The harness does not assume normalized actions for
arbitrary PlantAdapters.  They are contract checks only; they do not replace
topology-specific physics validation.

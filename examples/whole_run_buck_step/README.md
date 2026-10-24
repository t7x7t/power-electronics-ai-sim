# Whole-run averaged Buck step

This example declares a second simulation engine whose time ownership differs
from the stepped adapters: the engine integrates its own horizon and returns one
complete trace. It is a project-owned ideal averaged Buck reference, not a
switching-level or device model.

```powershell
pe-sim list-plants
pe-sim run examples/whole_run_buck_step/config.json
pe-sim inspect-run runs/whole-run-buck-step --compact
```

The whole-run execution route is implemented and registered by the default
composition root. The run publishes a normal package, and the manifest records
`time_model: "whole_run"`, `enforcement: "post_hoc_only"`, and the route
limitations `in_run_enforcement_unavailable` / `post_hoc_validation_only`.

Two things are deliberately refused on this route: `--mode formal_comparison`
(the kernel cannot enforce constraints while the engine owns the clock, so the
request is rejected with `formal_comparison_rejected` before a run directory
exists) and the Python-API recovery options
(`checkpoint_interval_steps`, `resume_from`, `interrupt_after_steps`,
`cancel_after_steps`), which have no kernel-owned steps to act on.

`controller_id: "pi"` is declared because the experiment schema requires a
controller identity, but **the kernel-provided controller is not driven on this
route**. The engine owns a discrete PI loop on `vout`; its gains are the
`kp`/`ki` values in `plant_config`. The matching `controller_config` block is
recorded as an intention only and does not affect the simulation.

The input schedule steps the voltage reference from 3 V to 5 V at 10 ms with a
12 V input and a 1 ohm load. The trace cadence and the engine's control period
are both 100 us, over a 20 ms horizon.

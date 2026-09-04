# Stage 5 Runner modular API (v1 increment)

The runner now has a low-friction facade and an advanced policy surface while
retaining the legacy ``Runner().run(...)`` call. The default path is:

```python
from pe_sim import run_experiment

result = run_experiment(spec, plant, controller)
```

This uses deterministic fixed-window timing, audited calls, standard safety
checks, and qualification. Ordinary users do not need policy configuration.

Advanced callers can compose narrowly scoped policies:

```python
from pe_sim import BoundedActionPolicy, RecoveryPolicy, RunOptions, TimingPolicy, run_experiment

options = RunOptions(
    timing=TimingPolicy(engine="fixed_window", allow_sample_offset=True),
    recovery=RecoveryPolicy(checkpoint_interval_steps=10),
    action_policy=BoundedActionPolicy(0.0, 1.0),
)
result = run_experiment(spec, plant, controller, options)
```

`AuditPolicy` is enabled by default. Exploratory callers may explicitly disable
call recording when evidence overhead is not wanted; `formal_comparison` rejects
that combination before execution. Unsupported timing engines, invalid
recovery intervals, and conflicting legacy arguments fail before execution.
The facade forwards to
the compatibility Runner, so existing `Runner().run` code can migrate
incrementally.

### Action semantics

The default action policy is finite-only: it rejects NaN and infinity and
otherwise preserves the controller value. The Runner does not assume that
every actuator is a normalized duty ratio. A Plant may explicitly declare an
`action_policy()` (the Buck and Boost reference plants declare duty in
`[0, 1]`), or an advanced caller may pass `RunOptions(action_policy=...)`.
`BoundedActionPolicy` clamps to declared limits and records `lower`/`upper` in
the sample when projection occurs. Custom policies implement
`project(value) -> (value, reason)`. Non-finite values and invalid bounds
always fail closed.

## Responsibility boundaries

- **Runner/coordinator**: lifecycle, ordering, timing windows, safety hooks,
  recovery, audit, and publication orchestration.
- **PlantAdapter**: circuit dynamics and plant-specific state.
- **ControllerAdapter**: control law and controller state.
- **Policies**: optional timing, recovery, audit, measurement, safety, and
  qualification choices.

Buck/Boost equations, PI/PID/MPC algorithms, plotting, metrics, thermal
models, and engineering conclusions must not be added to the Runner. They
belong in adapters or later analysis layers. The policy modules are an
incremental extraction point; timing, checkpoint, and publication helpers
remain in `runtime.py` until independently stabilized.

This increment does not claim process-crash recovery, complete event queues,
unit conversion, physical validity, or hardware readiness.

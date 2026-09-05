# Stage 6: Safety, Validity, and Qualification (v1)

Stage 6 makes machine checks explicit and extensible while preserving the
simple `run_experiment(spec, plant, controller)` entry point. It is a runtime
integrity boundary, not a claim of hardware safety or physical model validity.

## Three plugin phases

* **SafetyPlugin** runs before an observation is given to the Controller and
  before an action is applied. It may implement domain stop conditions. A
  failed finding stops the run and produces `RUN_FAILED`.
* **ValidityPlugin** checks observation data and timing. The default
  `ObservationValidityPlugin` checks finite numeric values, required fields,
  timestamp monotonicity/visibility, age, declared units, and configured
  numeric ranges. A failure is treated like a simulation execution failure;
  it is never silently converted into a usable sample.
* **QualificationPlugin** runs after sample collection. It determines whether
  the captured data is structurally usable for later metrics or comparison.
  A failed qualification retains artifacts and changes the terminal state to
  `DISQUALIFIED`; it does not make a physical or engineering approval.

Plugins are ordinary Python objects. They may provide only the relevant
method. Methods receive `(subject, CheckContext)` and return `CheckReport`, a
single `CheckFinding`, `bool`, or the legacy `(passed, reasons)` tuple. Plugin
exceptions fail closed as `checker_error`.

The default observation plugin requires the configured primary measurement and
fresh (`age_steps == 0`) data. Supplying an `ObservationValidityPlugin` in
`validity_plugins` replaces that default instance, so a model with a declared
latency can explicitly set `max_age_steps` or its own required fields.

```python
from pe_sim import ObservationValidityPlugin, RunOptions, run_experiment

validity = ObservationValidityPlugin(
    required_measurements=("vout",),
    expected_units={"vout": "V"},
    ranges={"vout": (0.0, 2.0)},
    max_age_steps=0,
)
result = run_experiment(spec, plant, controller,
                        RunOptions(validity_plugins=(validity,)))
```

## Structured evidence

`CheckReport` contains `passed`, `rule_version`, `plugin_id`, and findings.
Each finding records `rule_id`, `category`, `severity`, `message`, evidence,
and `stop_requested`. Reports are written to `safety.json`,
`qualification.json`, and the corresponding `checks` section of
`manifest.json`. A failed run also stores the findings in `failure.findings`.
Each plugin invocation is included in the ordered `audit.json` call stream.

Common data categories are `nonfinite_observation`, `invalid_measurement`,
`timestamp_violation`, `future_observation`, `stale_observation`,
`missing_measurement`, `unit_mismatch`, and `measurement_out_of_range`.
Common execution categories are `numerical_nonconvergence`,
`backend_timeout`, `backend_process_failure`, `plant_advance_failure`,
`plant_execution_failure`, and `model_numerical_error`. Adapters may raise
the corresponding structured exception classes from `pe_sim.contracts`; the
Runner also classifies common third-party exception names and messages.

## Deliberate boundary

This stage does not inject sensor faults and does not model physical sensor
damage. In the current pure-simulation scope, a "sensor" issue means an
invalid observation/measurement channel. HIL or real-hardware integrations
may add their own domain plugin later and must keep those claims separate
from these runtime checks.

The built-in rules do not establish convergence, calibration, thermal safety,
or product compliance. They only prevent invalid execution evidence from
being mistaken for a valid result.

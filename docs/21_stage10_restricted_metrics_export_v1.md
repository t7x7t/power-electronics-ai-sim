# Stage 10: Restricted Metrics and Export Slice (v1)

Machine-readable output schemas are provided at
`schemas/stage10-metrics.schema.json` and
`schemas/stage10-evidence-report.schema.json`.

This is the first, deliberately small Stage 10 capability. It reads an
already-published run package and produces basic time-series summaries or an
external export. It does not run a simulation, modify a run package, perform
physical validation, compare controller quality, generate plots, or establish
an engineering conclusion.

## Why the input gate is strict

Metrics are easy to calculate from a partial or altered JSON file, but such a
result can be mistaken for evidence from the original simulation. Therefore
this slice accepts only project-owned reference runs with all of the following
properties:

- Manifest status is exactly `QUALIFIED`, and its qualification and safety
  records are passed.
- The directory has a `PUBLISHED` run-state marker, every required artifact,
  a complete artifact index, and independently recomputable
  `manifest_sha256` and `package_sha256` values.
- Source provenance has a complete Git object ID and records a clean working
  tree. Unknown, dirty, failed, incomplete, disqualified, or tampered runs
  are rejected.
- Python/environment provenance is present and non-unknown. The built-in
  references may retain their explicit `not_declared` backend limitation;
  unknown, unavailable, or partial external backend records are rejected.
- The Plant identity is one of `FakePlant`, `FakeLoadPlant`, `BuckPlant`, or
  `BoostPlant`. Real simulator adapters, PSFB, LLC, learned controllers, and
  user-defined Plant families remain outside this slice.

The gate does not upgrade the reference model into a physical-model claim. A
qualified, hash-complete package means the captured data passed the currently
declared runtime checks and has not changed since publication.

## API

```python
from pe_sim import export_metrics, summarize_run

summary = summarize_run("runs/buck-reference")
export_metrics("runs/buck-reference", "exports/buck-summary.json")
export_metrics("runs/buck-reference", "exports/buck-samples.csv")
```

`summarize_run()` returns `stage10-metrics-v1` JSON-shaped data containing
the sample count, audited call count, sampled time span, and `count`, `min`,
`max`, `mean`, `first`, `last`, and (where a time series exists) trapezoidal
integral for each complete numeric sample column. It also includes a
versioned `metrics` object from the default registry. The reviewed metric IDs
are `samples.count`, `audit.calls`, `time.duration`, `series.vout.mean`,
`series.vout.final`, `series.action.mean`, and `series.action.final`.

The summary also records the input data contract (`samples-v1` and
`run-metrics-v1` for packages produced by the current Runner), the sorted
sample field set, and a deterministic data fingerprint. Runtime packages
written by older v1 revisions without explicit version fields are read as
those legacy defaults; newly written packages declare both versions in
`metrics.json`. This makes a later schema change visible instead of silently
mixing incompatible sample layouts.

Custom metrics must be registered explicitly with `MetricRegistry` and carry a
stable ID, version, unit, input-field declaration, and numeric calculator.
Unknown IDs and non-numeric calculator results are rejected. Every registry has
an explicit `registry_id`, version, sorted metric contract list, and SHA-256
fingerprint. The fingerprint is included in summaries, comparisons, and
reports so that the same metric definitions can be reproduced during review.

Every summary carries `run_id`, `manifest_sha256`, and `package_sha256` in its
`provenance` object. Every CSV row repeats these three fields. Exports must be
outside the source run directory: writing inside it would change the package
and invalidate the evidence hash.

`load_run_evidence()` and `summarize_run()` raise
`PostprocessEligibilityError` with concrete rejection reasons. This is
intentional: a rejected package must be repaired, rerun, or inspected rather
than silently summarized.

## Numeric comparison and evidence report

`compare_metric_runs()` compares two or more eligible packages using selected
registered metrics and an explicit absolute tolerance. It reports each run's
`run_id`, experiment, Plant/Controller identity, `manifest_sha256`, and
`package_sha256`, each metric's version/unit, pairwise values, absolute
differences, and `exact_match`, `tolerance_match`, or `mismatch` status. Input
runs must use the same declared sample and metrics data schema versions;
otherwise comparison fails closed. It does not require the experiments to be
identical and does not infer controller quality or physical equivalence.

`build_evidence_report()` and `write_evidence_report()` wrap that comparison in
the `stage10-evidence-report-v1` JSON schema. Report destinations must be
outside every source run package; source packages are never modified.

## Boundaries and next work

This slice is a traceable source for later Stage 11 visualizations and a
foundation for broader Stage 12 evidence levels. The registry remains limited
to numeric metrics and does not define domain-specific acceptance thresholds.
Plotting, richer units, statistical methods, and reviewed adapters are later
work; real Ngspice data remains outside this restricted slice. Stage 15 final
acceptance remains a separate final activity.

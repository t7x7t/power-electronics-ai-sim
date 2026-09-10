# Stage 12: Restricted Evidence Classification (v1)

This slice classifies Stage 10 summaries and comparison reports. It records
what the machine-checked evidence can support and what remains blocked; it does
not generate engineering conclusions.

## Levels

- `mechanism`: a qualified, traceable runtime artifact exists.
- `functional`: a qualified project-owned reference run was summarized.
- `comparable`: registered numeric metrics from multiple qualified packages
  matched within the declared tolerance and shared data/provenance contracts.
- `limited`: a comparison report exists but has a mismatch, incomplete evidence,
  or another declared limitation.
- `diagnostic_only`: a structurally valid but failed, incomplete, or explicitly
  diagnostic result. Dirty, unknown, malformed, or hash-incomplete provenance
  is rejected at the classifier boundary and cannot receive any level.

The classifier never emits `physical-performance`, `research-safety`, or
`hardware-readiness`. Those require separate reviewed procedures and evidence.
The output always includes `limitations`, `blocked_conclusions`,
`review_required`, and a `review_record`. A default `not_recorded` review record
does not imply maintainer or downstream engineering approval.

## API

```python
from pe_sim import classify_stage10, classify_summary, classify_report

classification = classify_summary(summary)
comparison_classification = classify_report(report)
```

Inputs must retain complete run provenance (`run_id`, Manifest SHA-256, package
SHA-256, `QUALIFIED` status, `working_tree_status=clean`, known source
provenance, and known/partial environment provenance). Comparison reports must
have matching source and comparison provenance, metric registry/data
descriptors, a valid tolerance, and an exact or tolerance match outcome before
they receive `comparable`.

The machine-readable output contract is
`schemas/stage12-evidence-classification.schema.json`.

This is an evidence-boundary tool only. It does not validate physical model
correctness, hardware safety, thermal limits, or product compliance.

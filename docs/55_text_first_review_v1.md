# Text-First Run Review (v1)

**Status:** contract for the first review surface. It is deliberately text-only.
No chart, canvas, topology drawing, or browser UI is added.

## Why text first

The withdrawn browser workbench and the three JSON contracts solved "move data"
but not "let a human or an agent read a run in order". A graphical first version
made the hierarchy ambiguous: the reader had to infer which number mattered,
which check failed, and which claim was allowed. This version reverses the
priority:

1. the **hierarchy** is the product: identity → claim → parameters → checks →
   signals → provenance → artifacts → limitations;
2. every value is a short, labelled text row, not a plot;
3. graphics are deferred until the reading model is stable, and a future UI must
   render this envelope instead of inventing a parallel model.

The three visualization contracts (`TopologyDescriptor`, `TraceDataset`,
`OutputDataset`) remain the data plane. The review is a **reading surface** over
already-recorded data; it adds no evidence and changes no verdict.

## `pe-sim.run-review.v1`

```json
{
  "schema_version": "pe-sim.run-review.v1",
  "rule_version": "review-v1",
  "run_id": "buck",
  "summary": {
    "integrity": "ok",
    "claim": "bounded",
    "qualification": "partial",
    "evidence": "limited"
  },
  "sections": [
    {
      "id": "integrity",
      "title": "Package identity and integrity",
      "level": 1,
      "status": "ok",
      "rows": [{"label": "run id", "value": "buck", "status": "ok"}],
      "notes": [],
      "children": []
    }
  ],
  "limitations": ["..."]
}
```

### Section order and content

| # | `id` | Rows | Children |
| --- | --- | --- | --- |
| 1 | `integrity` | run id, run mode, status, package integrity, artifact count, compatibility, manifest/package digest (first 16 hex) | — |
| 2 | `claim` | conclusion, statement; then one row per `may_claim`, `may_not_claim`, `narrowed_by` entry | — |
| 3 | `parameters` | total parameter count, groups count, parameters without a recorded unit/source | one child per parameter group: rows `name = value unit`, note `source` / `review_status` |
| 4 | `checks` | applicability / safety / qualification pass counts and finding counts | one child per finding group: rows `rule id`, `passed`, `severity`, `message` |
| 5 | `signals` | sample count, first/last time, monotonicity, non-finite count | one child per signal: `n`, `min`, `max`, `mean`, `last` |
| 6 | `provenance` | source commit (first 12), worktree status, evidence level, qualification level, credibility status, environment status | one child per recorded backend: `version`, `status`, then one row per solver setting |
| 7 | `artifacts` | count | one child per artifact: `size`, `sha256` (first 16) |
| 8 | `limitations` | one row per recorded limitation | — |

### Rules

- Every section has a `status` of `ok`, `attention`, `fail`, or `unavailable`.
  `attention` marks a recorded non-blocking finding; `fail` marks a failed check
  or a redacted/unreadable value; `unavailable` marks a source that the package
  does not record.
- Rows carry `status: null` unless a row is itself a verdict (`ok`, `attention`,
  `fail`, `not_recorded`).
- Values are pre-formatted strings. Numbers keep the recorded precision; no
  rounding beyond the digest shortening above. Booleans render as `yes`/`no`.
- `checks` truncates a finding group at 20 rows and adds a note
  `"N more finding(s) not shown"`. The full findings stay in the manifest and in
  `inspect-run`; truncation exists so that the hierarchy stays readable.
- Sections are always all eight, in this order. A section with no data still
  appears with `status: "unavailable"` and an explanatory note.
- The review never executes a model, never re-derives a physical quantity, and
  never writes into the package.

### Rendered text

`render_text(review)` produces ASCII-only text so that a Windows console with a
legacy code page cannot mangle it:

```text
pe-sim run review: buck
claim: bounded | qualification: partial | evidence: limited | integrity: ok

== 1. Package identity and integrity ==  [ok]
  - run id: buck
  - status: QUALIFIED
  ...

== 3. Parameters ==  [ok]
  - plant parameter count: 5
  -- plant
     - input_voltage_v = 12 V   (source: assumption, review: unreviewed)
```

- Level-1 header: `== <n>. <title> ==  [<status>]`, the status marker omitted
  when `ok`.
- Children indent by two spaces per level, prefixed `-- ` at level 2 and `- `
  deeper.
- A row whose status is not `ok` gets a trailing `  [!] <status>`; a `notes`
  entry is printed as `  (note: ...)`.
- No box-drawing characters, no Unicode arrows, no colour codes.

`render_markdown(review)` produces the same hierarchy with `##`/`###` headings
and `- **label**: value` bullets. It is UTF-8 and may contain the same ASCII
content only.

## CLI

```powershell
pe-sim review-run <run_dir> [--format text|markdown] [--json]
```

- Default output is the rendered text report.
- `--format markdown` emits markdown.
- `--json` emits the envelope and ignores `--format`.
- Exit codes: `0` when the package is readable, `2` when it is not (a
  `RUN_INSPECTION_REJECTED` envelope with `scope: "run_review"`).

## Service

`GET /v1/runs/{run_id}/review` is read-only and revalidates the package per
request.

| Query | Content-Type | Body |
| --- | --- | --- |
| none | `text/plain; charset=utf-8` | rendered text |
| `?format=markdown` | `text/markdown; charset=utf-8` | rendered markdown |
| `?format=json` | `application/json` | the envelope |

An unknown `format` value is a `400` with `error.code = "invalid_request"`.
Discovery adds `inspection_schemas.review = "pe-sim.run-review.v1"`.

This is the whole "UI" for this version: one readable document per run. It is
not a dashboard, not a chart, and not interactive.

## Non-claims

- A review is not evidence, not a verdict, and not an approval. It reorders and
  labels recorded facts.
- `attention`/`fail` in the review do not change `evidence_level`,
  `qualification_level`, `credibility`, or the claim conclusion.
- No graphical element, topology rendering, waveform plot, or interactive
  control is delivered. Trace samples remain available through the
  `TraceDataset` contract for a later UI.
- The review does not replace `inspect-run`, `parameter-view`, or the manifest.

## Open decisions

| ID | Question | Current behaviour |
| --- | --- | --- |
| RV-1 | Should the review include raw finding evidence payloads? | No. Only rule id, severity, pass state, and message, capped at 20 per group. |
| RV-2 | Should the review be localised? | No. English only, matching the rest of the CLI and docs. |
| RV-3 | Should the review embed the trace samples? | No. Only per-signal summary statistics. |
| RV-4 | Should a future UI consume the envelope or call `inspect-run` directly? | The envelope. A UI must not invent a parallel reading model. |

## Acceptance

```text
pe-sim review-run <run> --format text
pe-sim review-run <run> --format markdown
pe-sim review-run <run> --json
curl "http://127.0.0.1:8765/v1/runs/<run>/review"
```

Covered by `tests/test_review.py`, `tests/test_review_cli.py`, and the service
tests in `tests/test_visualization_service_review.py`.

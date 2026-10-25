# Validity and Authenticity v1

**Status:** implemented in 0.5.0. This document defines the applicability
(validity) layer and authenticity Level 1 (Git anchor). It is an engineering
evidence contract, not a physical, thermal, hardware, or product claim.

**Related documents:** `docs/06_constraint_assurance_v0.md`,
`docs/16_stage6_safety_validity_qualification_v1.md`,
`docs/22_stage12_evidence_levels_v1.md`,
`docs/39_credibility_assurance_v0.md`.

## 1. Why two separate layers

A finite simulation is not automatically a meaningful one, and an unchanged
package is not automatically an authentic one.

| Question | Layer | Answer |
| --- | --- | --- |
| Is the model declared to apply at this operating point? | Applicability | `validity_envelope()` + policy-driven checks + qualification level |
| Is the package unchanged since publication? | Integrity | Manifest/package hashes, artifact index, `inspect-run` |
| Was the package identity recorded outside the package? | Authenticity L1 | Detached attestation + Git anchor index |
| Was it signed by a trusted key? | Authenticity L2 | Signature array and verifier registry (future) |

Validity and authenticity are independent. A physically valid run can be
unattested; an attested run can be physically inapplicable.

## 2. Applicability layer

### 2.1 Declared model claim

Adapters may implement two optional methods:

```python
def validity_envelope(self) -> ModelClaim: ...
def parameter_provenance(self) -> ParameterProvenance: ...
```

Missing methods are represented as `unknown`, never invented. A malformed
declaration becomes `declaration_invalid` with a bounded error string; it does
not crash the run.

`ModelClaim` records:

- `claim_level` (`L1-averaged-reference`, `L2-switching-reference`, ...);
- `topology`, `model_form`, `assumptions`, `limitations`;
- `operating_ranges` (`duty`, `input_voltage_v`, `load_resistance_ohm`, ...);
- `numerical_limits` (`integration_step_s.max`,
  `control_period.min_switching_periods`, `samples_per_switching_period.min`,
  ...);
- `uncheckable_rules` with an explicit reason;
- `actuator` and `static_parameters`.

The declaration has a **stable SHA-256** that excludes status/error fields and
sort-normalizes lists. It is deliberately **not** part of
`plant.manifest_identity()`: a changed declaration is a changed applicability
statement, not a changed model equation.

`ParameterProvenance` records `name`, `value`, `unit`, `source`
(`datasheet`, `measurement`, `assumption`, `default`, `reviewed_reference`,
`undeclared`), `uncertainty`, `reference` (or `reference_sha256` for a
machine-local path), and `review_status`. The aggregate digest is
order-independent.

### 2.2 Policy

`ApplicabilityPolicy` lives in `RunOptions.applicability`:

| Mode | Finding severity | Blocks? | Intended use |
| --- | --- | --- | --- |
| `record` | `info` | No | Exploratory runs (default) |
| `warn` | `warning` | No | Exploratory runs with visible warnings |
| `enforce` | `error` for `hard` limits | Yes | Formal comparison (default) |
| `off` | — | No | Explicit opt-out |

`default` resolves to `record` for exploratory runs and `enforce` for
`formal_comparison`. Only `hard` strengths block; `advisory` limits (CCM
estimate, printed sample resolution, pulse-edge resolution) remain warnings.

### 2.3 Check phases

| Phase | Examples |
| --- | --- |
| Preflight (before the run directory exists) | declaration status, actuator bounds subset, schedule Vin/load, timebase limits, parameter review state |
| Initial observation | truth Vin/load/duty ranges, CCM estimate |
| Each observation | same, plus external-input change detection |
| Each action | duty domain, pulse-width resolution |
| Post-run | qualification rules and numerical health |

Applicability findings are written to `preflight.json`, `safety.json`, and
`manifest.json` under `applicability`. They are not mixed into the model hash.
The runtime checks are not added to the ordered adapter audit stream, so
existing audit-order contracts remain unchanged.

### 2.4 Shipped envelopes

- **BuckPlant / BoostPlant (L1 averaged):** duty domain, positive Vin/load,
  integration-step stability limit, minimum substeps. CCM cannot be checked
  because the averaged model has no switching frequency; it is listed in
  `uncheckable_rules`.
- **NgspiceBuckPlant (L2 switching):** duty domain, positive Vin/load, integer
  switching periods per control window, minimum printed samples per switching
  period, load-to-off-resistance ratio, pulse-edge resolution. Solver
  `max_step_s`, thermal state, and bit-identical restore are explicitly
  uncheckable.

### 2.5 Qualification levels

`CheckReport` and `qualification.json` now carry a level: since schema `0.2`
the vocabulary is `assessed` / `partial` / `failed` (it used to be
`full` / `limited` / `failed`, which shared a token with the evidence ladder):

- `assessed`: no findings;
- `partial`: warning-only findings, still `passed: true`;
- `failed`: blocking findings, `passed: false`.

Readers map the legacy words; see `docs/50_evidence_vocabulary_v2.md`.

The baseline `BasicQualificationPlugin` is unchanged. The opt-in standard pack
adds:

- `SteadyStateRule`: tail-window slope and ripple relative thresholds; short
  runs and windows shorter than two switching periods are `not_applicable`.
- `AnalyticalEquilibriumRule`: tail-window mean versus
  `analytical_steady_state(duty)`; varying duty is explicitly `duty_varying`.
- `NumericalHealthRule`: consumes bounded solver classification.

The CLI enables the standard pack by default; use
`--no-standard-qualification` to keep only the baseline check. Programmatic
callers opt in with `RunOptions(qualification_plugins=(QualificationRulePack.standard(plant=plant),))`.

### 2.6 Numerical health

`numerical_health.parse_log()` classifies up to three bounded excerpts per
window. Fatal categories (for example `timestep too small`, `no convergence`,
`gmin stepping failed`) always take precedence over warning categories on the
same line. Warnings (`singular_matrix`, `gmin_stepping`, `source_stepping`,
`timestep_reduction`, ...) produce `level="limited"`; fatal categories produce
`level="failed"`. The netlist echo region is ignored. The full log is never
stored; only categories, counts, excerpts, and the log digest are recorded in
`plant.evidence.numerical_health`.

### 2.7 Credibility assessment

`pe_sim.credibility` is a read-only consumer of a published package. It never
imports a Plant or executes a solver. Factors:

`integrity`, `source_state`, `run_mode`, `environment`, `backend`,
`plant_identity`, `implementation_review`, `qualification`, `applicability`,
`numerical_health`, `safety`, `evidence_level`, and optional `authenticity`.

Each factor is `pass`, `limited`, `fail`, or `unknown`. Overall precedence:

```text
not_credible > limited > unknown > credible
```

There is no numeric score. `inspect-run` adds
`summary.credibility` and `summary.qualification_level`; the package
`status` remains an integrity-only field.

`manifest.evidence_level` is now derived: failed runs are
`diagnostic_only`, exploratory/limited-evidence runs are `functional` or
`limited`, and a clean formal comparison with known provenance and a reviewed
implementation is `comparable`.

## 3. Authenticity Level 1

### 3.1 Exact claim

`L1_anchored` means all of the following were verified:

1. the attestation payload hashes to its recorded `payload_sha256`;
2. the package's manifest, package, and artifact-index digests match the
   payload;
3. the payload fields match `manifest.json` and `.run-state.json`;
4. a reachable Git commit contains the exact anchor record in
   `attestations/anchor-index.jsonl`;
5. the same commit contains the attestation file whose payload hash matches
   the anchor record.

It does **not** prove author identity, reviewer approval, a trusted remote, or
reproducibility from `source_commit`. A local writer with write access can
rewrite the working tree, the index, or Git history. L1 makes the record
visible and comparable in Git history; it does not make modification
impossible.

### 3.2 Files

```text
<repo>/attestations/
  anchor-index.jsonl                 # append-only, canonical JSON lines
  <run_id>.attestation.json          # detached payload + signatures
  superseded/<run_id>.<oldhash>.attestation.json
```

The attestation is deliberately outside the run package. Writing it does not
change `package_sha256`. `recorded_at_utc` is human-review metadata and is
excluded from `record_sha256`, so re-attestation is idempotent and a verifier
can reconstruct the anchor digest from the payload.

### 3.3 Commands

```powershell
# Write the detached attestation and append the anchor record (no git write)
pe-sim attest-run runs/<run_id> --repo .

# Verify locally; 1 means "locally consistent, not yet anchored"
pe-sim verify-attestation runs/<run_id> `
  --attestation attestations/<run_id>.attestation.json --repo .

git add attestations
git commit -m "attest run <run_id>"

# Verify the committed anchor; exit 0 means L1_anchored
pe-sim verify-attestation runs/<run_id> `
  --attestation attestations/<run_id>.attestation.json --repo .

# Optional in inspect-run
pe-sim inspect-run runs/<run_id> `
  --attestation attestations/<run_id>.attestation.json --require-anchored
```

### 3.4 L2 compatibility

- `payload` and `payload_sha256` are L1; `signatures` is a separate array.
- A signature signs the 64-character `payload_sha256` text.
- Appending a signature does not change the payload, the anchor digest, or the
  L1 trust level.
- `register_signature_verifier(format, verifier)` accepts a verifier returning
  `valid`, `invalid`, or `unknown`. Unregistered formats are `unknown`, never
  `valid`.
- No cryptographic dependency is imported by the L1 module.

## 4. Acceptance evidence

| Area | Positive | Negative |
| --- | --- | --- |
| Envelope | declared Buck/Boost/ngspice envelopes hash stably | malformed declaration is non-blocking and visible |
| Policy | record/warn continue with findings | enforce blocks before a run directory exists |
| Schedule | valid Vin/load sequence | negative Vin or non-numeric schedule value is reported |
| Qualification | warning-only is `limited`, `passed: true` | blocking finding is `failed`, `passed: false` |
| Numerical health | recovered warning is `limited` | `timestep too small` is `fatal`, not a warning |
| Credibility | clean formal reviewed package is `credible` | tampered/failed package is `not_credible` |
| Attestation | re-attestation is idempotent; committed anchor is `L1_anchored` | tampered samples/hashes are `none`/invalid |
| L2 slot | appended unknown signature preserves L1 | invalid signature is reported, L1 remains |

Verification commands:

```powershell
python -m pytest -q
python -m compileall -q src tests
git diff --check
```

## 5. Residual gaps (not closed by this increment)

- **CRED-04:** comparison still uses one scalar tolerance and index alignment;
  `TolerancePolicy` is not implemented.
- **CRED-06:** `reviewed_identities.json` now records `reviewed_at`,
  `reviewer`, `acceptance_ids`, and `evidence_refs`, but conformance reports are
  not yet content-addressed into the pin.
- **CRED-07:** run lineage for resume/derived products is not implemented.
- **COMPAT-01/02/03:** schema migration registry, frozen kernel surface, and
  unit registry remain open.
- **Authenticity L2/L3:** signing, release signatures, and service co-signing
  are not implemented. L1 is a visibility anchor, not a security boundary.
- Stage 12 classification still has its own policy; the new `credibility`
  section is advisory evidence and does not replace Stage 12.

## 6. Non-claims

A `credible` assessment is a statement about recorded evidence and declared
limits, not about the real world. `QUALIFIED` remains an automated per-run
state. Neither the envelope nor the attestation establishes physical model
validity, calibration, thermal safety, hardware readiness, compliance, or
product approval.

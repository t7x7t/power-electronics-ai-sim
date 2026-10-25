# Claim Conclusion v1

**Status:** current since release 0.6.1. `pe-sim inspect-run` emits one
reader-side conclusion that combines the evidence level, the qualification
level, and the credibility assessment. This document defines what that
conclusion means and what it may never be used to say.

## 1. The question this answers

A published package records three facts that answer different questions:

| Field | Question | Vocabulary |
| --- | --- | --- |
| `evidence_level` | How strong is the recorded evidence? | `diagnostic_only` < `functional` < `limited` < `comparable` |
| `qualification_level` | How completely did the configured checks run? | `assessed` / `partial` / `failed` |
| `credibility.status` | Is the recorded evidence intact, attributable, and reviewed? | `credible` / `limited` / `unknown` / `not_credible` |

Reading three fields and combining them by intuition invites over-claiming:
`comparable` evidence with `partial` qualification is not eligible for formal
comparison, and a `credible` assessment cannot repair a failed run. The claim
conclusion answers the single user question - **what may I claim from this
package?** - without a fourth interpretation step.

## 2. The conclusion ladder

```
no_claim < exploratory < bounded < comparison_eligible
```

| Conclusion | Meaning | May claim | Must not claim |
| --- | --- | --- | --- |
| `comparison_eligible` | Every input is at its strongest; formal comparison is allowed at the recorded schema version and source commit. | The configured checks passed; the evidence is comparable at this schema; the credibility factors passed under the declared policy. | Comparison across a different schema version or source commit; physical, thermal, safety, or hardware validity. |
| `bounded` | Usable only within the recorded limitations. | The result within those limitations; the package is internally consistent. | Formal or numeric comparison; the absence of limitations; physical validity. |
| `exploratory` | The routine executed and the record is internally complete, but the evidence cannot support a bounded claim. | Exploratory inspection of the recorded signals. | Formal or numeric comparison; physical validity; behaviour outside the recorded operating conditions. |
| `no_claim` | The recorded evidence is not usable for any statement. | Nothing. | The run succeeded; any numerical result; comparison with another run. |

## 3. Derivation rules

The derivation is ordered. A **hard stop** returns `no_claim` immediately:

1. package integrity is not `ok`;
2. the recorded run status is not `RUN_OK`, `QUALIFIED`, or `COMPARABLE`;
3. the evidence level is missing or outside the evidence ladder;
4. qualification is `failed`;
5. credibility is `not_credible`;
6. the package schema is `incompatible`.

Otherwise the conclusion starts at the strongest level the evidence supports:

| Evidence level | Starting conclusion |
| --- | --- |
| `comparable` | `comparison_eligible` |
| `limited` | `bounded` |
| `functional` | `exploratory` |
| `diagnostic_only` | `no_claim` |

Then each input can only lower it:

| Input | Ceiling | `narrowed_by` reason |
| --- | --- | --- |
| qualification `partial` | `bounded` | at least one configured check was not evaluated and passed |
| qualification missing / unrecognised | `exploratory` | qualification evidence is missing or unrecognised |
| credibility `limited` | `bounded` | the credibility assessment is limited |
| credibility missing / `unknown` / unrecognised | `exploratory` | the credibility assessment is unknown |
| compatibility not `current` | `bounded` | cross-version comparison is not assessed |

`narrowed_by` names **every** input below the `comparison_eligible` threshold,
not only the cap that changed the final value. It is empty only when every
input is at its strongest. `inputs` copies the six values the derivation read.

## 4. Output shape

`inspect-run` adds one `claim` object to its summary. It is additive: existing
fields are unchanged and the inspection schema tag remains
`pe-sim.run-inspection.v1`.

```json
{
  "claim": {
    "schema": "pe-sim.claim.v1",
    "rule_version": "claim-v1",
    "conclusion": "bounded",
    "statement": "The recorded evidence supports a bounded claim only; use the result with the recorded limitations and do not compare it formally.",
    "inputs": {
      "integrity": "ok",
      "run_status": "QUALIFIED",
      "evidence_level": "limited",
      "qualification_level": "partial",
      "credibility": "limited",
      "compatibility": "current"
    },
    "narrowed_by": [
      "evidence level 'limited' is below comparable",
      "qualification is partial: at least one configured check was not evaluated and passed",
      "the credibility assessment is limited"
    ],
    "may_claim": ["..."],
    "may_not_claim": ["..."],
    "limitations": ["..."]
  }
}
```

## 5. Reading rules

- **One direction only.** The conclusion is never stronger than any input
  allows. It never upgrades a level and never recomputes a level from samples.
- **Reader-side.** The conclusion is not written into the package and does not
  change the manifest. A package produced before this view existed gains a
  conclusion when a current release reads it; the package itself is unchanged.
- **Discovery is not evidence.** `pe-sim capabilities` says whether an optional
  capability is installed; it contributes nothing to a claim. A run that used
  an installed capability still gets its conclusion from recorded evidence.
- **Compatibility is a reading policy.** An older package read as `read_only`
  is capped at `bounded`. The registered migration renames vocabulary; it does
  not add evidence.
- **No aggregate score.** The conclusion is a label with reasons, not a
  percentage or ranking of quality.

## 6. What this does not claim

- A higher conclusion is not a statement about physical, thermal, safety, or
  hardware validity.
- `comparison_eligible` is scoped to the recorded schema version and source
  commit; the comparator is responsible for checking that the other package
  shares them.
- The conclusion does not replace `compare-runs`, tolerance policy, or human
  review.

## 7. Acceptance evidence

```text
pytest -q tests/test_claim_conclusion.py tests/test_inspection.py `
  tests/test_credibility.py tests/test_evidence_vocabulary.py `
  --basetemp .tmp/pytest-claim
```

## 8. Related documents

| Document | Relationship |
| --- | --- |
| `docs/22_stage12_evidence_levels_v1.md` | Stage 12 classification contract |
| `docs/39_credibility_assurance_v0.md` | Credibility factors and precedence |
| `docs/47_compatibility_policy_v1.md` | Read-with-policy verdict and migration |
| `docs/50_evidence_vocabulary_v2.md` | The two disjoint vocabularies |
| `docs/28_release_toolchain_policy_v0.md` | Extras policy referenced by capabilities |

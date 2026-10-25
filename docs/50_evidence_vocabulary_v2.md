# Evidence Vocabulary v2

**Status:** current, since manifest schema `0.2` (release 0.6.0). This document
replaces the two-ladder vocabulary described in earlier revisions of
`docs/22_stage12_evidence_levels_v1.md` and states the rule that now holds
across the runtime, the evidence views, and the Stage 12 classifier.

## 1. The problem this removes

Schema `0.1` recorded two different ladders that shared three words:

| Ladder | Values | Answered |
| --- | --- | --- |
| evidence (`manifest.evidence_level`) | `diagnostic_only`, `functional`, `limited`, `comparable` | how strong is the claim? |
| qualification (`manifest.qualification.level`) | `full`, `limited`, `failed` | how completely were the configured checks evaluated? |

`limited` therefore meant both "the claim is bounded but usable" and "some
checks could not be evaluated". A user reading `limited` had to know which
field it came from to know what it meant. Three of the five Stage 12 levels
(`mechanism`, `functional`, `comparable`) repeated tokens from the evidence
ladder with yet another meaning.

## 2. The rule

**One token, one meaning.** The two ladders are disjoint, and the evidence
ladder has exactly one definition in code (`pe_sim.time_model`).

### Evidence ladder (unchanged names)

```
diagnostic_only < functional < limited < comparable
```

- `diagnostic_only` — the run produced a record but no usable claim.
- `functional` — the routine executed and the recorded evidence is internally
  complete; this is exploratory strength.
- `limited` — stronger than functional, usable only with declared limitations.
- `comparable` — eligible for formal comparison at the same schema version.

The ladder is ordered; `cap_evidence_level` can only lower a level, never raise
it. Whole-run engines are structurally capped at `limited`.

### Qualification ladder (renamed)

```
assessed | partial | failed
```

- `assessed` — every configured check was evaluated and passed.
- `partial` — usable, but at least one check was inconclusive or warned.
- `failed` — a blocking finding stopped the run.

`passed` remains a derived boolean (`level != "failed"`). The qualification
ladder is **not** ordered against the evidence ladder; it describes check
completeness, not claim strength.

### Stage 12 scope (was a level)

`mechanism` is now `scope: "mechanism"` on a `functional` classification. It
described *what the evidence was about* (a qualified, traceable runtime
artifact), not how strong it was. The Stage 12 output schema is
`stage12-evidence-classification-v2`; the classifier never emits a level
outside the evidence ladder.

## 3. Reading schema 0.1 packages

Published packages are immutable (decision D4). A reader maps the old words on
a **reader-side copy** and says what it translated:

| Recorded (0.1) | Presented (0.2) |
| --- | --- |
| `qualification.level: full` | `assessed` |
| `qualification.level: limited` | `partial` |
| `qualification.level: failed` | `failed` |
| Stage 12 `evidence_level: mechanism` | `functional` + `scope: mechanism` |

`pe_sim.schema_registry` holds the hop `0.1 -> 0.2`. `assess_package_compatibility`
returns `read_only` with a `migration` block naming the path. An older package
with **no** registered path is `incompatible` — the registry is what makes the
compatibility verdict meaningful rather than optimistic.

`inspect-run` exposes the same fact:

```json
{
  "compatibility": {"status": "read_only", "migration": {"from": "0.1", "to": "0.2", "path": ["0.1", "0.2"]}},
  "migration": {"from": "0.1", "to": "0.2", "path": ["0.1", "0.2"]},
  "qualification_level": "partial",
  "evidence_level": "functional"
}
```

## 4. What did not change

- Evidence level names and their ordering.
- The manifest and package digest rules.
- The rule that a view never upgrades a level.
- The rule that cross-version comparability is `not_assessed` unless the
  packages share a schema version and a source commit.

## 5. What this does not claim

- A renamed token is not new evidence. A 0.1 package read as `partial` is no
  stronger than it was as `limited`.
- The migration registry is a reader aid, not a repair tool: nothing writes,
  re-signs, or migrates a published package.
- The ladder still says nothing about physical, thermal, safety, or hardware
  validity.

## 6. Acceptance evidence

```text
pytest -q tests/test_evidence_vocabulary.py tests/test_schema_registry.py `
  tests/test_compatibility.py tests/test_inspection.py `
  tests/test_stage12_evidence.py tests/test_qualification_rules.py `
  --basetemp .tmp/pytest-vocabulary
```

## 7. Related documents

| Document | Relationship |
| --- | --- |
| `docs/22_stage12_evidence_levels_v1.md` | Stage 12 classification contract |
| `docs/43_kernel_roadmap_v1.md` | evidence pricing table and gates |
| `docs/45_error_and_rejection_codes_v1.md` | evidence-reading views |
| `docs/46_time_model_and_engine_boundary_v1.md` | route evidence caps |
| `docs/47_compatibility_policy_v1.md` | read-with-policy and the registry |

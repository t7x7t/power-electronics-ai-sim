# Kernel Compatibility Surface v1

**Status:** implemented in 0.5.1. This document defines three kernel-surface
increments that share one property: they constrain how future work is allowed
to change, rather than adding new simulation capability.

**Related documents:** `docs/39_credibility_assurance_v0.md` (COMPAT-02,
CRED-04), `docs/38_kernel_review_v0.md` (reviewed identity),
`docs/40_validity_and_authenticity_v1.md` (validity and authenticity).

## 1. Why these three belong together

| Increment | Question it settles | Finding |
| --- | --- | --- |
| Public API tiers | May an increment rename or remove this name? | COMPAT-02 |
| Reference-plant identity pin | Is the default example path reviewed with the same strength as the simulator path? | docs/38 residual |
| Tolerance policy | Is a numeric comparison meaningful for heterogeneous signals? | CRED-04 (tolerance half) |

Each was previously decided ad hoc per change. All three are now machine
checked, so a change that crosses one of these lines fails a test instead of
silently weakening a claim.

## 2. Public API tiers

### 2.1 Classification rule

`src/pe_sim/public_api.py` holds one rule:

> A name is **stable** when an external adapter author or an evidence verifier
> needs it to (a) implement a Plant/Controller/check plugin against the
> documented contracts, (b) run an experiment through the public entry point,
> or (c) verify a published run package and its authenticity record.
> Everything else is **experimental**.

The classification is a statement about *who depends on the name*, not about
code quality.

### 2.2 Shapes

```python
STABLE_API: frozenset[str]        # 81 names at 0.5.1
EXPERIMENTAL_API: frozenset[str]  # 141 names at 0.5.1
__all__ = sorted(STABLE_API | EXPERIMENTAL_API)  # 222 names
```

`pe_sim/__init__.py` raises at import time if a name in `__all__` is not
actually bound, so the surface cannot drift into a list of dangling promises.

Experimental names stay reachable from the top level on purpose: nothing is
hidden, but nothing is promised either.

### 2.3 Enforcement

`tests/fixtures/public_api.json` freezes the stable list plus a SHA-256 over
the sorted names. `tests/test_public_api.py` fails when:

- a stable name is added or removed without updating the baseline;
- the baseline list is unsorted, duplicated, or its `sha256` does not match;
- a classified name is missing from `pe_sim` or bound to `None`;
- the two sets overlap or either is empty.

Updating the baseline in the same commit is what makes a breaking change
reviewable.

### 2.4 Deliberately absent

There is no deprecation schedule, no `DeprecationWarning` pipeline, and no
version negotiation layered on top. Those belong to a 1.0 surface with real
external consumers; adding them now would be process without a beneficiary.
Adding a name to `STABLE_API` is a review decision, not a refactor.

## 3. Reviewed identity for the native reference plants

### 3.1 One mechanism, two paths

The reviewed-identity module moved from `pe_sim/backends/identity.py` to
`pe_sim/identity.py`. It is a general review mechanism, not an adapter detail:
core modules need it and must not import the external-simulator package to get
it.

Solve paths are declared relative to the package root:

| Backend | Declared solve path |
| --- | --- |
| `ngspice-buck` | `backends/ngspice_plants.py`, `netlist.py`, `netlist_builder.py`, `process.py`, `settings.py`, `wrdata.py` |
| `buck` | `reference_plants.py` |
| `boost` | `reference_plants.py` |

`buck` and `boost` intentionally share one digest: they are two classes in one
module, and both directions are covered by the same reviewed averaged equations
and RK4 integrator.

### 3.2 What changed in the manifest

`_AveragedConverter.manifest_identity()` previously recorded
`implementation.source_sha256`, computed with `inspect.getsource()`. That value
depended on how the module was loaded and could not be compared with the
simulator pin. It now records:

```json
{
  "module": "pe_sim.reference_plants",
  "class": "BuckPlant",
  "solve_path_modules": ["reference_plants.py"],
  "sha256": "<aggregate solve-path digest>",
  "review": {"status": "reviewed", "backend_id": "buck", "...": "..."}
}
```

The source digest normalizes CRLF to LF, so a Windows checkout and a Linux
checkout produce the same identity.

### 3.3 Explicit boundary: the run loop

`runtime.py` contains the Runner stepping/observation loop and changes far more
often than the model equations. It is **outside** the pin, and both reference
plants declare that boundary rather than leaving it implicit:

```text
rule_id: implementation.runner_loop
reason: runtime.py (Runner stepping/observation loop) is outside the reviewed
        solve path; the pin covers reference_plants.py only
```

The consequence is honest: `implementation.review.status == "reviewed"` means
the model implementation matches review, not that the entire execution path
does.

### 3.4 Effect on verification

Before 0.5.1 a Buck run reported `implementation_review = unknown` in the
credibility assessment, because no pin existed for the default example path.
It now reports `pass` when the digest matches, and `limited`/`fail` (per policy)
when the checkout has drifted from the pin.

## 4. Tolerance policy

### 4.1 Problem

A single absolute tolerance shared by every signal is meaningless for a sample
that contains a timestamp in seconds, a voltage in volts, and a duty ratio near
0.5. `1e-9` is unreachable for a voltage and trivially satisfied for a duty.

### 4.2 Shape

```python
ToleranceSpec(absolute=0.0, relative=0.0,
              per_signal=(("output_voltage_v", 1e-6, 1e-4),))
```

For each signal:

```text
limit = max(absolute, relative * |left_value|)      # per_signal overrides both
```

The left-hand run is the reference magnitude. No interpolation and no unit
conversion happen here.

### 4.3 Backward compatibility

When `relative == 0` and no override applies, `limit_for()` returns `absolute`
unchanged. `compare_runs(tolerance=1e-9)` therefore behaves exactly as before,
and the report omits `tolerance_policy` unless a policy was supplied.

Each recorded difference carries the `limit` it was judged against, so a
consumer can audit the decision without reconstructing the policy.

### 4.4 Provenance

When a policy is supplied, the report includes:

```json
"tolerance_policy": {
  "absolute": 0.0,
  "relative": 0.0001,
  "per_signal": {"output_voltage_v": {"absolute": 1e-06, "relative": 0.0001}},
  "signals": ["output_voltage_v"],
  "sha256": "<canonical digest>"
}
```

Two comparisons are only comparable if their policies match; the digest makes
that checkable.

### 4.5 CLI

```powershell
pe-sim compare-runs runs/a runs/b --relative-tolerance 1e-4
pe-sim compare-runs runs/a runs/b --signal-tolerance output_voltage_v:1e-6:1e-4
pe-sim audit-runs runs/a runs/b runs/c --relative-tolerance 1e-4
```

`--signal-tolerance NAME:ABSOLUTE:RELATIVE` is repeatable. Malformed,
non-numeric, negative, and non-finite values are rejected before any file is
read.

### 4.6 What is still open

Row alignment is still positional (`zip` over sample lists). Two runs with
different step counts, or with an adaptive-step solver, will compare as a
length mismatch rather than being resampled. That is the remaining half of
CRED-04 and the concrete prerequisite for UD-04 (engine-neutral adapters):
a second solver will not reproduce the first solver's time grid.

Resampling was deliberately not added here. A grid policy (interpolation
method, tolerance for extrapolation, treatment of switching edges) is a model
decision that needs its own review, and would otherwise be buried inside an
increment that only intended to fix tolerance semantics.

## 5. Acceptance evidence

| Area | Positive | Negative |
| --- | --- | --- |
| Public surface | `__all__` equals the classified union | adding/removing a stable name fails without a baseline update |
| Surface integrity | every exported name is bound | a name declared but unbound raises at import |
| Reference pin | `buck`/`boost` digests match the checked-in pin and report `reviewed` | changing `reference_plants.py` without updating the pin fails |
| Source identity | CRLF and LF checkouts produce the same digest | — |
| Boundary | `implementation.runner_loop` is declared uncheckable | the pin is not described as covering `runtime.py` |
| Tolerance scale | relative limit scales with the reference value | negative/non-finite values rejected |
| Tolerance override | per-signal override wins over the global policy | duplicate or malformed entries rejected |
| Backward compatibility | `relative == 0` reproduces the legacy absolute limit | default output has no `tolerance_policy` key |
| Provenance | report records the policy and its digest | policy digest changes when the policy changes |

Verification commands:

```powershell
python -m pytest -q
python -m compileall -q src tests
git diff --check
```

## 6. Non-claims

- A stable name is a compatibility promise, not a correctness guarantee.
- `reviewed` means the solve-path source matches the pin; it does not mean the
  model is physically valid, calibrated, or safe.
- A tolerance policy states what was accepted as equal; it does not establish
  that the difference is physically insignificant.
- None of these mechanisms resist a local administrator. They make changes
  visible in review; they do not prevent them.

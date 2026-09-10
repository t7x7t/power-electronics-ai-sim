# Stage 15: L1 Buck/Boost Acceptance

## Purpose and boundary

This document defines the first acceptance slice for the project-owned
`BuckPlant` and `BoostPlant` ideal averaged reference adapters. It verifies
that the public contracts, deterministic integrator, Runner lifecycle, and
the Stage 10/12 evidence hand-off work together for a small, reviewable
experiment.

The acceptance target is an infrastructure result:

- a reference Plant obeys the Plant/Controller contract;
- its declared averaged equations and equilibrium helpers are internally
  consistent;
- the Runner produces complete, hash-checked, recoverable evidence;
- the same inputs produce the same sample data;
- Stage 10 can consume a published package and Stage 12 can classify its
  evidence boundary.

The target is **not** a claim about a physical converter. The models do not
include switching devices, parasitics, losses, thermal behavior, magnetic
limits, control hardware, or an Ngspice backend.

## Acceptance matrix

| ID | Area | L1 check | Status | Evidence |
| --- | --- | --- | --- | --- |
| S15-01 | Public contract | Buck and Boost pass Plant conformance; FakePI passes Controller conformance | Required | `tests/test_stage15_buck_boost_acceptance.py` |
| S15-02 | Equations | Constant-duty equilibrium agrees with `Vbuck=D Vin`, `Vboost=Vin/(1-D)` and corresponding load current | Required | `test_analytical_equilibrium_matches_declared_equations` |
| S15-03 | Numerical integration | Halving the fixed RK4 substep stays within the declared L1 tolerance | Required | `test_step_halving_is_numerically_stable` |
| S15-04 | Parameter boundary | Non-finite/non-positive component parameters, invalid duty, and Boost duty 1 are rejected | Required | `test_reference_parameter_and_duty_boundaries_fail_closed` |
| S15-05 | Sweep and long run | A small duty sweep remains finite and a bounded cold-start run reaches finite observations | Required | `test_small_duty_sweep_and_long_run_remain_finite` |
| S15-06 | Runner lifecycle | Qualified completion records the expected state path and audited calls | Required | `test_runner_lifecycle_checkpoint_and_package_hashes` |
| S15-07 | Recovery | An interrupted Buck/Boost run resumes from a checkpoint and reaches `QUALIFIED` | Required | `test_interrupted_reference_run_resumes` |
| S15-08 | Repetition | Identical model/configuration/seed produces identical sample data and component identities | Required | `test_identical_reference_runs_have_identical_samples` |
| S15-09 | Post-processing chain | Published package -> Stage 10 summary -> Stage 12 functional classification retains provenance links | Required | `test_stage10_to_stage12_evidence_chain` |
| S15-10 | CLI | Buck and Boost JSON configuration smoke runs return success and publish a manifest | Required | `test_cli_buck_and_boost_smoke` |

The matrix intentionally uses exploratory runs in this worktree. Formal
comparison remains subject to the clean-worktree and complete-provenance gate.
The package hash and manifest hash are independently recomputed in the
Runner/hash and Stage 10 tests; they exclude mutable transaction metadata by
design.

## Test entry points

Focused acceptance:

```text
pytest -q tests/test_stage15_buck_boost_acceptance.py --basetemp .tmp/pytest-stage15
```

Supporting contract and integration tests:

```text
pytest -q tests/test_stage4_reference_plants.py tests/test_stage5_runner.py \
  tests/test_stage10_restricted_postprocess.py tests/test_stage12_evidence.py \
  tests/test_stage15_buck_boost_acceptance.py --basetemp .tmp/pytest-stage15-integration
```

The full repository command remains the final project acceptance gate and is
deliberately not replaced by this L1 slice.

## Explicit exclusions

This acceptance does not test or certify:

- real Ngspice execution, solver convergence, or backend process recovery;
- switching waveforms, PWM timing, dead time, device models, or topology
  connectivity;
- losses, thermal parameters, SOA, magnetic saturation, or rated power;
- hardware sensors, hardware safety, electromagnetic behavior, or product
  compliance;
- controller quality, optimality, superiority, or deployment readiness;
- cross-machine numerical equivalence;
- Stage 11 plots, Stage 14 migration, or Stage 13 learning algorithms.

Any Stage 10/12 output from this matrix remains `functional` reference
evidence at most. It must not be promoted to a physical-performance,
research-safety, hardware-readiness, or product conclusion.


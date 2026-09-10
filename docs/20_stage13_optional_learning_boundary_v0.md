# Stage 13: optional learning/adaptation integration boundary

**Status:** Optional extension; not a required core-infrastructure stage.

## Objective

Stage 13 defines the boundary at which a user may attach a reviewed,
project-specific learning or adaptation component to the simulation workflow.
The objective is interoperability and evidence control, not implementation of a
general machine-learning system. A user who only needs deterministic
simulation, qualification, comparison, artifacts, or visualization can complete
the core workflow without enabling this stage.

## What the core provides

The core infrastructure provides the conditions that an optional adapter must
respect:

- learning is explicitly opt-in; importing or installing an adapter does not
  enable it implicitly;
- only evidence that passed the applicable safety, validity, qualification, and
  provenance checks may be handed to an adapter;
- a run must be explicitly marked `LEARNING_ELIGIBLE` before it is used as
  learning input; exploratory, dirty, incomplete, failed, unknown-provenance,
  or non-comparable runs remain diagnostic-only;
- Plant state, Controller state, and any learner candidate state are kept
  separate, with the learner unable to silently mutate the source run,
  baseline, Plant, or Controller;
- every hand-off records the source run, configuration/contract/model hashes,
  adapter or learner identity and version, and the resulting artifact hashes;
- proposed updates are reviewable and reversible. A failed or rejected update
  must leave the prior model/controller and the prior baseline unchanged.

These are integration and evidence requirements. They do not prescribe a
learning algorithm, dataset format, update cadence, or domain-specific
acceptance rule.

## Explicit non-goals

The core project does not provide or require:

- a built-in trainer, optimizer, replay store, or online-learning loop;
- an automatic parameter-tuning or controller-replacement workflow;
- automatic promotion of a learned result to a baseline, engineering claim,
  hardware setting, or product conclusion;
- a generic policy for selecting algorithms, hyperparameters, or training data.

Those concerns belong to a downstream user or to a separately reviewed adapter.

## Adapter responsibility

An adapter that opts into this boundary must publish its own versioned contract,
declare its input and output schema, and supply the qualification and human
approval evidence required by the host project. The adapter is responsible for
domain-specific semantics and must fail closed when required evidence is
missing. Adapter output is an experiment artifact until an explicit review
accepts it; it is never an implicit change to the shared infrastructure.

## Acceptance boundary

Stage 13 is considered covered for the core project when the following are
documented and enforceable at the integration boundary:

1. opt-in and non-goals are visible to users;
2. eligibility, provenance, and approval gates are explicit;
3. state and artifact isolation prevents silent overwrite;
4. hand-off and rollback evidence is retained; and
5. the absence of a learning adapter does not block ordinary simulation runs.

Concrete learning implementations remain optional follow-up work and must be
reviewed independently from the core simulation infrastructure.

# License and Git Baseline Scope (v0)

This document records the approved scope for the first repository baseline.
It is a project-governance record, not a statement that the simulation models
are physically validated.

## Approved license

The repository `LICENSE` is the MIT License. It applies to project-owned
infrastructure and to later project-owned implementations that are added to
this repository, including small reference circuits and controller examples
such as PID examples.

The MIT License does not grant rights that the project does not own. A file
copied from an external project, model library, paper, vendor package, or
other third-party source keeps its original license and attribution
requirements unless a separate review records compatible terms.

## Migration exclusion

No code, model, result, hardware document, or other material from
`D:\\PySpice` is included in the v0 baseline. The old project remains a
reference-only source until each candidate file has been reviewed for:

- origin and exact source revision;
- copyright and license terms;
- redistribution permission;
- secrets, private paths, and generated data;
- compatibility with the public contracts and tests in this repository.

Migration approval must be recorded before a reviewed file is committed.
Unreviewed material must not be represented as MIT-licensed repository work.

## Baseline contents

The first clean baseline may contain only the current repository's:

- public Python package and CLI under `src/`;
- JSON Schemas under `schemas/`;
- tests under `tests/`;
- reference configuration and runner under `examples/`;
- architecture, operating-policy, CI, and ADR documentation under `docs/`;
- agent instructions under `agent/`;
- packaging metadata, README, CI workflow, and this license.

Generated output and machine-local state are excluded by `.gitignore`,
including `runs/`, `.ci-runs/`, `.tmp/`, `tmp*/`, pytest caches, bytecode,
build output, and egg-info metadata.

## Baseline identity

The baseline is the exact Git commit and optional tag reported by the
maintainer after review. A clean working tree is required when creating it.
The commit identifies the source snapshot; it does not prove model validity,
hardware safety, or research significance. Those claims require separate
qualification and human review.

## Future changes

Future project-owned infrastructure and reviewed examples remain covered by
the MIT License. New third-party material must be accompanied by source and
license metadata and must not silently inherit the repository license.

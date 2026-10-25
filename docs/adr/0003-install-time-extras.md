# ADR 0003: Install-time Extras Policy for Optional Simulator Frontends

## Status

Accepted on 2026-10-25. Closes Phase C item 1 of `docs/43_kernel_roadmap_v1.md`
as a recorded policy decision. Supersedes only the "behind a separate extra"
mechanism clause in ADR 0002 decision 2; the ngspice process boundary itself is
unchanged.

## Context

The kernel roadmap asked for optional capabilities to be declared as
install-time extras, so a packaged user could enable them without reading the
repository. Discovery already ships as `pe_sim.capabilities` /
`pe-sim capabilities` (`docs/49_optional_capabilities_v1.md`), and
`requirements-pyspice-tested.txt` is the verified installation handoff. What
was missing was a recorded decision about whether PySpice becomes an extra such
as `power-electronics-ai-sim[pyspice]`.

Three facts make the naive extra unsound **for this project**:

1. **License boundary.** PySpice is GPLv3 and this repository is MIT. An extra
   is packaging metadata, not a license change, but it presents a GPLv3
   runtime as a supported variant of an MIT package. The project deliberately
   keeps that boundary manual (ADR 0002, `docs/34`).
2. **Supported Python range.** `requires-python` is `>=3.10`, which includes
   Python 3.13. The verified PySpice pin set keeps `numpy==1.26.4`, which has
   no Python 3.13 wheel. An extra that resolves to that pin set would fail to
   install on an interpreter the package claims to support; an extra that does
   not pin would resolve to a combination known to be broken (`pip check`
   fails).
3. **Environment verdict.** `environment-check` treats declared extras as
   expected packages. Declaring PySpice would turn every correct MIT-only
   installation into a `missing` package report, weakening the one check whose
   job is to say whether the expected environment is installed.

The roadmap's intent - a packaged user can discover and enable the capability
without reading source - is already satisfied by a versioned capability report
plus a pinned requirements file. The extra is one implementation of that
intent, not the requirement.

## Decision

1. The **only** declared extra is `test`. Optional simulator frontends are not
   package extras.
2. PySpice remains a separate, manual installation through
   `requirements-pyspice-tested.txt` in a dedicated Python 3.10-3.12
   environment, followed by `python -m pip check`. The capability report names
   this path, the license boundary, and the missing Python 3.13 lock.
3. A future extra must satisfy all of the following before it may be declared:
   - it installs cleanly across the declared `requires-python` range;
   - `pip check` passes for every supported Python version;
   - it does not add a copyleft runtime to the MIT package's supported
     variants without a recorded license review;
   - it does not make `environment-check` report a missing expected package for
     a correct base installation;
   - the capability report and this policy are updated in the same commit.
4. `tests/test_release_policy.py` enforces the mechanical half of this
   decision: the extra table is exactly `test`, no simulator package appears in
   any dependency table, and the capability report does not advertise an
   extra.

## Consequences

- A user who wants PySpice follows two documented commands instead of one
  `pip install` with an extra. The trade is deliberate: the supported Python
  range stays honest and `environment-check` stays meaningful.
- `docs/43` Phase C item 1 is closed as "delivered as discovery plus a
  recorded policy decision"; it must not be reopened as an unexplained gap.
- Adding an extra later is a policy change, not a packaging detail: it needs a
  new ADR, a capability-report update, and the policy test update in one
  commit.

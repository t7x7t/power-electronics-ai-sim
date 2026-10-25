# Release Governance (v1): Identity, Evidence, Accountability

**Status:** contract for the P1 release-and-trust slice. This document defines
three artifacts and one verification procedure. It adds no simulation,
physical, or engineering claim, and it does not make a release signed,
reproducible, or approved.

## Why this exists

`docs/27_minimum_local_release_scope_v0.md` requires that a version tag is
created only after (a) a local gate has produced evidence for the exact clean
commit and (b) a human has reviewed that evidence and recorded the commit,
versions, test result, package hashes, date, scope, and non-claims.

Before this slice, (a) existed as `release-evidence.json` but (b) had no
machine-checkable form, and nothing tied the recorded version to a Git tag or
to a built distribution. Three concrete gaps were observed:

1. `pyproject.toml` declared `0.6.1` while Git tags stopped at `v0.2.1`, so
   "which commit is v0.6.1?" had no answer in the repository.
2. `src/pe_sim/cli.py` carried a hard-coded version string that was a second,
   independently editable copy of the release identity.
3. Gate evidence lived in an operator-chosen directory with no binding to any
   built wheel, so the hash of the wheel a reviewer approved was not recorded
   anywhere.

## Three questions, three artifacts

| Question | Artifact | Schema |
| --- | --- | --- |
| What exactly is in the artifact I ship? | distribution inventory | `pe-sim.distribution-inventory.v1` |
| Which commit, version, tag, and evidence does this release claim? | release record | `pe-sim.release-record.v1` |
| Who accepted responsibility for it? | approval block inside the release record | `pe-sim.release-record.v1` |

The artifacts are deliberately separate because they are produced by different
actors: the inventory and the record body are produced by automation; the
approval is a human edit. Automation must never be able to produce a record
that claims approval.

## Artifact 1: distribution inventory

`scripts/distribution_inventory.py` reads a built wheel and writes a
deterministic JSON inventory.

```powershell
python scripts/distribution_inventory.py dist/power_electronics_ai_sim-0.7.0-py3-none-any.whl `
    --license-file LICENSE --output dist/distribution-inventory.json
```

Requirements:

- standard library only, no network, no dependency resolution;
- contents are listed sorted by path and exclude `*.dist-info/RECORD`;
- every member hash is read from the archive, never inferred;
- the wheel's own SHA-256 is recorded, so the inventory identifies one file,
  not "a build of some commit";
- dependency entries come from `Requires-Dist` metadata only;
- dependency licenses are recorded as `not_verified` with
  `source = "not_recorded"`. The tool does not claim to have read, resolved, or
  classified any third-party license;
- the project license is recorded as `declared` only when the wheel metadata
  declares it; the `LICENSE` file hash is recorded separately and no
  equivalence between the two is claimed;
- `generated_at_utc` is `null` unless `--timestamp` is supplied, so the same
  wheel yields byte-identical inventory.

### What the inventory proves and does not prove

It proves that a specific wheel file had exactly this membership, these member
hashes, and these declared requirements at the time of generation. It does not
prove that two builds of the same source are byte-identical (they are not
claimed to be), that dependencies were downloaded or audited, that any license
is compatible with a downstream use, or that the wheel is safe to install.

## Artifact 2: release record

`scripts/release_record.py create` combines the gate evidence, the built
artifacts, the inventory, and the scope documents into one record.

```powershell
python scripts/release_record.py create `
    --evidence D:\release-evidence\v0.7.0\gate `
    --wheel dist/power_electronics_ai_sim-0.7.0-py3-none-any.whl `
    --sdist dist/power_electronics_ai_sim-0.7.0.tar.gz `
    --inventory dist/distribution-inventory.json `
    --output D:\release-evidence\v0.7.0\release-record.json
```

`create` fails closed unless all of the following hold:

- the gate evidence is `pe-sim.local-release-evidence.v1` with
  `outcome = "pass"` for a 40-character commit, and the commit did not change
  during the gate;
- `toolchain-observed.json` next to the evidence exists and contains the four
  recorded tool versions;
- the wheel filename version, the wheel `METADATA` version, and the inventory
  `distribution.version` all equal `--version`;
- the inventory's recorded wheel SHA-256 equals the SHA-256 of the supplied
  wheel. This is the binding that was missing before: evidence now points at
  one exact distribution;
- `docs/27_minimum_local_release_scope_v0.md` and
  `docs/28_release_toolchain_policy_v0.md` exist and their hashes are recorded;
- a release note `docs/releases/v<version>.md` exists and its hash is recorded.

The record always starts with:

```json
"approval": {"status": "pending", "reviewer": null, "reviewed_at_utc": null,
             "decision": null, "statement": null,
             "reviewed_record_digest": null},
"tag": {"status": "not_created", "name": "v0.7.0", "commit": null},
"signature": {"status": "not_signed", "method": null}
```

`record_digest` is the SHA-256 of the canonical JSON of the record **excluding**
`approval` and `record_digest` itself. The approval block therefore binds to
the exact record body a reviewer read.

## Artifact 3: approval

```powershell
python scripts/release_record.py approve D:\release-evidence\v0.7.0\release-record.json `
    --reviewer "reviewer-name" --statement "I reviewed the gate evidence, the inventory, and the non-claims."
```

`approve` refuses to run when a `CI` environment variable is set to anything
other than `0` or `false`. It records the reviewer, the UTC time, the decision
(`approve` or `reject`), the statement, and the digest of the record body that
was reviewed. Re-approving replaces the block and appends the previous block to
`approval_history`.

Approval is accountability, not cryptography. It records who accepted the
scope and the non-claims for one commit and one artifact set. It cannot stop a
person from writing the same fields by hand, and it is not a signature.

## Verification

```powershell
python scripts/release_record.py verify D:\release-evidence\v0.7.0\release-record.json `
    --dist dist --evidence-dir D:\release-evidence\v0.7.0\gate `
    --require-approved --require-tag --json
```

`verify` recomputes every hash, re-derives the record digest, re-checks the
approval binding, and — only when asked — checks that the Git tag points at the
recorded commit.

Lookup is split by artifact role, because build output and evidence live in
different directories and must not be copied together to satisfy a checker:

| `artifacts[].role` | Directory | Flag |
| --- | --- | --- |
| `release_artifact` (wheel, sdist, inventory) | build output | `--dist` |
| `supporting_evidence` (gate, formal demo, upgrade) | evidence record | `--evidence-dir` |

Check status vocabulary:

- `pass` — the check ran and agreed;
- `fail` — the check ran and disagreed;
- `not_checked` — the check was **not requested** (no `--dist`, no
  `--require-approved`, no `--require-tag`), or a directory was not supplied for
  that artifact role. `not_checked` never means "verified": it does not
  participate in the verdict and is always named in the detail. The release
  procedure requests every check.

Exit codes: `0` pass, `1` failure, `2` partial. `partial` means a check was
**requested but could not be performed**, for example `--require-tag` outside a
Git repository. Both `1` and `2` are release-blocking.

`check-identity` guards repository-level drift and is cheap enough for every
run:

```powershell
python scripts/release_record.py check-identity --json
```

It requires the `pyproject.toml` version, `pe_sim.__version__`, and the release
note filename to agree, and it fails if `src/pe_sim/cli.py` contains a
hard-coded copy of the current version string. Tag presence is reported, and
fails only with `--require-tag`, because a tag is created after the commit
exists.

## Single source of version truth

`src/pe_sim/_version.py` holds `__version__`. `pe_sim.__version__` and
`pe-sim --version` both read it. `pyproject.toml` keeps a static `version`
because a build-isolated metadata read of `pe_sim` would import the package and
its dependencies; the duplication is closed by `check-identity`, which fails on
any disagreement. `version` must not become dynamic without replacing that
check.

## Tagging policy

A tag `v<version>` is created with `git tag -a` only after `verify` passes with
`--require-approved`. The tag message must contain the `record_digest` and the
commit. Tags are not signed in v1 (see non-goals). Annotated tags are used so
that the tag object records a message and a tagger instead of only a commit
pointer.

## Non-goals

- **Signing.** No signed tag, no signed artifact, no key custody, no
  transparency log. `signature.status` stays `not_signed`, and `verify` accepts
  that value as policy-consistent rather than as a missing check.
- **Distribution.** No PyPI, conda, container, or index publication.
- **Reproducible builds.** No claim that rebuilding the same commit yields
  byte-identical wheels.
- **SBOM standards conformance.** The inventory is project-owned. It is not
  claimed to be CycloneDX, SPDX, or SWID, and no third-party SBOM tool is added
  as a dependency.
- **CI as approver.** CI may build and inventory artifacts. It may not approve,
  tag, or publish.
- **License verification.** The inventory records declared metadata only.

## Open decisions

| ID | Question | Current behaviour |
| --- | --- | --- |
| RG-1 | Should evidence and records be committed to the repository? | No. They live outside the checkout, because generating them dirties or post-dates the release commit. Only the *format* and the release note are versioned. |
| RG-2 | What happens when a record must be corrected after approval? | `create` a new record, approve again; `approval_history` preserves the earlier block. No in-place field edits are supported. |
| RG-3 | Should `verify` require the tag by default? | No. Default off, because verification must be possible before the tag exists. Release procedure always passes `--require-tag`. |
| RG-4 | Should intermediate untagged versions be back-filled? | No. Versions `0.3.0`–`0.6.1` were never tagged; `docs/releases/` records this instead of inventing tags. |
| RG-5 | Should the record include a CI run id? | Not in v1. The local gate is the authoritative evidence; CI run identity is recorded inside the gate evidence only if a future job supplies it. |
| RG-6 | Should approval require a second reviewer? | No. v1 records one accountable reviewer. A second-reviewer rule needs an identity and conflict-of-interest model that does not exist yet. |

## Enforcement

- `tests/test_distribution_inventory.py` — determinism, member hashes, declared
  dependency parsing, license non-claims, and fail-closed input handling.
- `tests/test_release_record.py` — pending-by-default, CI refusal, digest and
  approval binding, wheel↔inventory binding, tamper detection, tag requirement.
- `tests/test_version_consistency.py` — version single-source and release-note
  presence.
- The `release-artifacts` CI job builds the wheel and sdist, writes the
  inventory and a checksum file, and uploads them as a workflow artifact. It
  does not approve, tag, publish, or claim a release.

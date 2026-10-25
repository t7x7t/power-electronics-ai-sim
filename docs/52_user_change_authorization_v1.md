# User Change Authorization and Visibility v1 (planned)

**Status:** design and sequencing plan. **Not implemented.** No runtime
behavior changes yet. This document defines Phase E of
`docs/43_kernel_roadmap_v1.md`.

## 1. Intent

The user must be able to answer three questions about an agent-assisted
simulation:

1. **When** did the agent change the Plant, Runner, or Controller?
2. **Which parts** changed?
3. Was the change **authorized by the user**?

The kernel requirement is deliberately narrow:

- unauthorized changes are **visible**;
- unauthorized changes **cannot produce formal evidence**;
- exploratory work remains possible.

Preventing file writes at the tool level (IDE, MCP server, agent runtime) is a
product-phase concern. The kernel does not try to be a sandbox.

## 2. Scope and definitions

| Term | Meaning |
| --- | --- |
| Component | `plant:<id>`, `runner:<route>`, or `controller:<id>` |
| Tracked change | A Git-tracked file change under a component's declared path scope, or a configuration change visible in the run's config snapshot |
| Baseline | The commit the user names when granting permission, or the last accepted commit |
| Grant | A user-issued, scoped record that says a component may be changed |
| Locked | No valid grant covers the current change set |

A component is **not** locked by a filesystem permission in the kernel. It is
locked in the sense that changes outside a grant cannot support formal
evidence.

## 3. Records

### 3.1 Change report (`pe-sim.change-control.v1`)

Each run records a read-only change report, derived from Git state and the
config snapshot:

```json
{
  "schema": "pe-sim.change-control.v1",
  "baseline": "<commit or null>",
  "source_commit": "<commit or null>",
  "working_tree_status": "clean",
  "components": {
    "plant:buck": {
      "changed": true,
      "files": ["src/pe_sim/reference_plants.py"],
      "status": "authorized",
      "grant_id": "grant-2026-10-25-1"
    },
    "runner:stepped": {
      "changed": false,
      "files": [],
      "status": "unchanged"
    },
    "controller:pi": {
      "changed": true,
      "files": ["adapters/pi_controller.py"],
      "status": "unauthorized"
    }
  },
  "status": "unauthorized",
  "reasons": ["controller:pi changed without a valid grant"],
  "formal_eligible": false
}
```

The paths in this example are illustrative; the project's component map declares
the actual scope. The report appears in `manifest.json` and in `inspect-run`.
It never changes a published package.

### 3.2 User grant (`pe-sim.user-grant.v1`)

One envelope, two kinds:

| Kind | Purpose |
| --- | --- |
| `modification` | The user allows the agent to change a scoped component |
| `parameter_review` | The user confirms the physical parameters of a component (Phase E3) |

Fields:

```json
{
  "schema": "pe-sim.user-grant.v1",
  "kind": "modification",
  "grant_id": "grant-2026-10-25-1",
  "granted_by": "user",
  "created_at": "2026-10-25T12:00:00Z",
  "expires_at": "2026-10-25T18:00:00Z",
  "base_commit": "<40-hex commit>",
  "one_shot": true,
  "scope": {
    "components": ["plant:buck", "controller:pi"],
    "paths": [
      "src/pe_sim/reference_plants.py",
      "adapters/pi_controller.py"
    ],
    "operations": ["edit", "configure"]
  },
  "reason": "tune the Buck reference controller",
  "nonce": "<random>",
  "signature": null
}
```

Storage rules:

- the grant lives **outside the repository** in a user-scoped directory;
- the agent may read it but must not write it;
- `PE_SIM_GRANT_FILE` names the grant file for a run (documented in tests);
- the kernel validates scope, `base_commit`, and expiry, but does not
  authenticate the user in v1;
- an optional signature/anchor is added in Phase E4.

The agent may **request** a grant. The `grant` command is user-only; an agent
must never be able to mint one.

## 4. Status vocabulary

Closed vocabulary for each component and for the overall report:

| Status | Meaning |
| --- | --- |
| `authorized` | The component changed within the grant scope, the base commit matches, and the grant has not expired |
| `unauthorized` | The component changed outside the grant, the base commit differs, or the grant expired |
| `unchanged` | The component did not change |
| `not_declared` | The component changed, but no baseline or grant was declared |
| `unknown` | Git state or baseline data is unavailable |

The overall status is `unauthorized` if any component is unauthorized; otherwise
`unknown` if any is unknown; otherwise `not_declared` if any is not declared;
otherwise `authorized`. `unchanged` components never weaken the overall status.

## 5. Evidence rules

| Run mode | Change status | Result |
| --- | --- | --- |
| `exploratory` | any | Allowed; `change_control` recorded; claim capped at `exploratory` |
| `formal_comparison` | `authorized` | Allowed; formal evidence may be produced |
| `formal_comparison` | `unauthorized` / `not_declared` / `unknown` | Rejected before any run directory is created |

`claim` gains a `change_control` input but no new conclusion level. A formal
claim is never stronger than the change report allows. With no Git repository,
formal mode remains rejected (existing rule) and the report is `unknown`.

## 6. What the user sees

`inspect-run` gains a top-level `change_control` block. A compact view answers:

```text
baseline: <commit>
plant:buck        changed  authorized    grant-2026-10-25-1
runner:stepped    unchanged
controller:pi     changed  unauthorized
formal_eligible: false
reason: controller:pi changed without a valid grant
```

`pe-sim changes <run-dir>` is an optional convenience command; the manifest and
`inspect-run` remain authoritative.

## 7. Phases

### E1 - Visibility only (first increment)

- derive `change_control` from Git state and the config snapshot;
- require an explicit baseline (for example `PE_SIM_CHANGE_BASELINE` or a
  recorded last-accepted commit); without one the report is `not_declared`,
  not a guess;
- write it to the manifest and expose it in `inspect-run`;
- no rejection and no new command;
- acceptance: a user can see which component changed and which files changed.

### E2 - Formal gate

- add `pe-sim.user-grant.v1` and scope matching;
- reject `formal_comparison` when the report is not `authorized`;
- keep exploratory runs available;
- acceptance: an unauthorized change cannot reach formal evidence; a valid
  grant can.

### E3 - Parameter review and visual confirmation

- add `kind: parameter_review`, bound to the parameter-set digest;
- any parameter change makes an earlier review `stale`;
- extend the read-only visualization service with the parameter table,
  provenance, ranges, solver settings, qualification, and claim;
- acceptance: the user can review the key parameters without reading source,
  and the confirmation is recorded and anchored.

### E4 - Outer enforcement and signing (product phase)

- IDE/MCP/agent-runtime write locks;
- signed grants and user authentication;
- branch protection and kernel/release identity (UD-06);
- this phase is outside the kernel's narrow requirement.

**Gate G-E:** a user can see every Plant/Runner/Controller change and the files
behind it; an unauthorized change cannot produce formal evidence; exploratory
runs remain usable with a capped claim.

## 8. Relationship to existing contracts

- `docs/03_agent_operating_policy_v0.md` keeps its current rules until E1/E2
  ship; the future model is described here, not implied by today's policy.
- `docs/48_user_workflow_v1.md` consumer boundary is unchanged: a consumer is
  read-only and never receives Runner/Plant/Controller authority.
- `docs/50_evidence_vocabulary_v2.md` vocabulary is unchanged. No new evidence
  level is introduced.
- `docs/40_validity_and_authenticity_v1.md` credibility factors are unchanged;
  `change_control` is a new input to the reader summary, not a credibility
  factor.
- The existing kernel-identity gap remains: `reviewed_identities.json` pins
  Plant solve paths, not `runtime.py` or `whole_run_route.py`. E4 closes it.

## 9. Open decisions

1. **Baseline choice:** merge-base, last accepted commit, or an explicit
   user-declared commit.
2. **Path-to-component map:** static declaration in the repository or explicit
   scope paths in the grant. The first is simpler; the second is harder to
   spoof.
3. **Grant storage:** user-scoped file (v1) or signed token (E4).
4. **Parameter review:** required for all formal runs, or only for claims that
   assert physical validity.
5. **Git-less environments:** `unknown`, formal rejected, exploratory allowed.
6. **Runner changes:** whether they additionally require a kernel release
   review, not only a user grant.
7. **E2 rollout:** hard default immediately, or one release behind an opt-in
   flag with a migration note for existing formal users.

## 10. Non-goals

- Sandboxing, malware defense, or preventing a malicious process from writing
  files.
- User authentication in v1.
- Deciding whether a model is physically correct.
- Adding a new evidence ladder or redefining `claim`.

# Run Interaction Contract v1

**Status:** binding for the CLI surface shipped in 0.5.6-0.5.7. The Python
names are classified **experimental** (`docs/12_public_contracts_stage3_v1.md`
rules); the wire schemas below are versioned and additive-only within v1.

**Purpose.** Define how a client that did not write the simulation code -
an agent, a script, a workbench - starts a run, observes it, cancels it, and
reads the terminal outcome without reading the run directory or importing
`pe_sim` internals.

Roadmap context: Phase B items 1 and 2 of
`docs/43_kernel_roadmap_v1.md`. Item 3 (enumeration) is `pe_sim.catalog` and
`pe-sim list-runs`. Item 4 (stable error codes) is specified in
`docs/45_error_and_rejection_codes_v1.md`.

## 1. The request

A run request is the existing experiment document plus the invocation that
starts it:

```powershell
pe-sim run <config.json> [--mode ...] [--progress] [--cancel-file PATH]
```

The document contract is unchanged (`schemas/experiment.schema.json`,
`schema_version: "0.1"`). This document does not add a second request format:
a wrapper would duplicate validation and create a second place for the
meaning of a run to drift.

`--progress` and `--cancel-file` are interaction options. They change what the
client can observe, never what the run computes or how it is priced.

### 1.1 Discovery before the request

A client that does not already know a `plant_id` asks:

```powershell
pe-sim list-plants
```

The answer carries `"schema": "pe-sim.plant-catalog.v1"` and one entry per
registered plant: `plant_id`, `backend_id`, `class_name`, `time_model`,
`control_location`, `event_granularity`, `schedule_keys`, `config_keys`,
`trust_level`, `export_eligible`, `topology_export`. `config_keys:
null` means the factory validates its own keys, not that any key is accepted.
The catalog exposes declarations only; the factory stays in-process.
`pe-sim list-runs` serves the same purpose for packages already produced. Its
root resolves as an explicit argument, then `PE_SIM_RUNS_ROOT`, then `./runs`;
see `docs/47_compatibility_policy_v1.md`.

## 2. Progress events

When `--progress` is given, the CLI writes one JSON object per line to
**stderr** and keeps **stdout** for the terminal result. Each line is:

```json
{
  "schema": "pe-sim.run-event.v1",
  "sequence": 0,
  "event": "run.started",
  "run_id": "buck-pi-step",
  "data": { "...": "event-specific" }
}
```

Rules:

1. `sequence` counts delivery attempts within one process, starting at 0.
   A sink failure consumes a number and is counted in the run's metrics, so a
   client can notice a gap instead of silently seeing a shorter stream.
2. `run.started` is the first attempt; `run.finished` is the last. A client
   must treat a stream without `run.finished` as *unknown*, not as success or
   failure, and verify the package instead.
3. Progress is **rate-limited**. The kernel emits `run.progress` only when the
   configured step interval or wall-time interval is satisfied
   (`ProgressPolicy`). Event timing is machine-dependent and is never part of
   the contract.
4. Progress is **not evidence**. Events are not written into the run package,
   are not hashed, and cannot change a status, qualification, or evidence
   level. A misbehaving sink is counted (`progress_events_dropped` in
   `metrics.json`) and the run continues.

Event payloads:

| Event | `data` fields |
| --- | --- |
| `run.started` | `plant_id`, `controller_id`, `mode`, `time_model`, `steps_total`, `start_step_index`, `duration_s`, `control_period_s` |
| `run.progress` | `step_index` (completed window), `sample_count`, `steps_total`, `time_s` (simulation time), `fraction`, `wall_time_s` |
| `run.finished` | `status`, `run_dir`, `sample_count`, `qualification_level`, `evidence_level`, `error`, `failure_category` |

`wall_time_s` is the only machine-dependent field. It exists to help a human
or scheduler, not to be compared across runs.

## 3. Terminal result

The CLI prints exactly one JSON object to stdout:

```json
{
  "schema": "pe-sim.run-result.v1",
  "status": "QUALIFIED",
  "run_id": "buck-pi-step",
  "run_dir": "runs/buck-pi-step",
  "backend": "buck",
  "plant_id": "buck",
  "plant_class": "BuckPlant",
  "evidence_level": "functional",
  "failure_category": null,
  "qualification": { "passed": true, "level": "full", "...": "..." }
}
```

- `evidence_level` is the value recorded in the published manifest; it is
  derived once and reused, so the client and the package cannot disagree.
- `failure_category` is the manifest's `failure.category` (null when the run
  did not fail). Its vocabulary and the rejection envelope used for exit-2
  refusals are in `docs/45_error_and_rejection_codes_v1.md`.
- Exit codes are unchanged: `0` for `RUN_OK`/`QUALIFIED`, `1` for any other
  completed run status, `2` for a rejected request
  (`CONFIG_REJECTED`, `FORMAL_COMPARISON_REJECTED`,
  `RUN_INSPECTION_REJECTED`). An `INCOMPLETE` run is exit `1`, not exit `2`:
  the request was valid and a package was published.
- The terminal result is a summary. The authority remains the package plus
  `pe-sim inspect-run`.

## 4. Cancellation

The kernel depends on one method:

```python
class CancelToken(Protocol):
    def cancelled(self) -> bool: ...
```

It is polled once at each control-window boundary, before any work in that
window. Consequences, stated honestly:

- Cancellation granularity is one window. The kernel cannot stop an adapter
  inside `advance()`/`observe()`.
- A request that arrives after the final window has no effect; the run
  completes normally.
- A cancelled run publishes an `INCOMPLETE` package with failure category
  `cancelled`, a checkpoint when the adapter can snapshot, and
  `recoverable: true`. `resume_from` treats it like any other incomplete
  source.
- `cancelled` (client request) is distinct from `interrupted`
  (`KeyboardInterrupt`). Both end in `INCOMPLETE`; the failure category
  records who asked.

Delivery mechanisms:

| Mechanism | Use |
| --- | --- |
| `CallbackCancelToken(predicate)` | in-process clients |
| `FileCancelToken(path)` | `--cancel-file PATH`: existence is the request |
| `RunOptions.cancellation` | the only kernel-side wiring |

The older `cancel_after_steps` / `interrupt_after_steps` options remain
deterministic test hooks. They are not the client contract and are not exposed
by the CLI.

The file token only reads the path. The runner never creates, truncates, or
deletes the file, because the request belongs to the client and must remain
observable after the run.

## 5. What this surface may not do

- It may not mutate a published package. Cancellation and progress write
  nothing outside the in-progress temporary directory; the package is still
  published atomically and remains verifiable by `inspect-run`.
- It may not redefine validity. `run.finished` reports the same status,
  qualification, and evidence level that the manifest carries.
- It may not become a control channel for arbitrary code. The only input it
  accepts is the path whose existence means "stop".
- Out of scope: remote access, multi-user operation, authentication, a
  long-lived server, and server push.

## 6. Versioning

Within `pe-sim.run-event.v1` and `pe-sim.run-result.v1`:

- Fields may be **added**; clients must ignore unknown fields.
- Renaming, removing, or changing the meaning of a field requires a `v2`
  schema tag and a new document.
- New event names may be added; existing names keep their meaning.

## 7. Gate G-B evidence

`tests/test_run_events.py` and `tests/test_cli_run_interaction.py` together
exercise the gate with no access beyond documented contracts:

1. start a run and receive `run.started`;
2. observe ordered, rate-limited `run.progress` events;
3. cancel through a token (in-process) and through `--cancel-file` (CLI);
4. receive a terminal `run.finished` and a `pe-sim.run-result.v1` on stdout;
5. enumerate with `list-runs` and verify with `inspect-run` the cancelled
   package;
6. confirm a broken sink changes neither the samples nor the status, and that
   the cancellation request file is untouched;
7. receive a `pe-sim.rejection.v1` envelope with a branchable `error_code`
   when the request itself is refused (`tests/test_error_codes.py`).

## 8. Related documents

| Document | Relationship |
| --- | --- |
| `docs/02_run_artifacts_and_state_machine_v0.md` | package layout and `INCOMPLETE` handling |
| `docs/43_kernel_roadmap_v1.md` | phase ordering, invariants, gates |
| `docs/42_kernel_interface_topology_v1.md` | what an adapter and the kernel may know |
| `docs/12_public_contracts_stage3_v1.md` | stable vs experimental classification |

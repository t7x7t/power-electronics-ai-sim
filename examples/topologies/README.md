# Topology examples

Small, hand-checkable descriptors that an agent can pattern-match against. They
are **data**, not plants: nothing here is registered in the kernel, and adding a
converter to this directory adds no plant, no solve path and no evidence claim.

| File | What it demonstrates |
| --- | --- |
| `buck.json` | One commanded duty, one switch, one freewheeling diode. |
| `sync_buck.json` | Two gates derived from one duty through `metadata.control`, with dead time. |
| `inverting_buck_boost.json` | A negative output rail: the load and the output capacitor bind their positive terminal to the reference net. |
| `interleaved_buck.json` | Three gates derived from one duty and spread evenly across one switching period. |
| `two_leg_sync_buck.json` | Two legs, each complementary inside itself, half a period apart. |

Each file is a complete `topology-descriptor-v1` document with no `fingerprint`
(it is being authored, not recorded). `metadata.example.compile` carries the
duty and duration that make the example demonstrate its own operating point.

```powershell
pe-sim validate-topology .\examples\topologies\buck.json
pe-sim compile-topology .\examples\topologies\buck.json --duty 0.5 --duration 2e-3
```

`tests/test_topology_examples.py` validates and compiles every file here, so an
example that stops being expressible fails the suite rather than the reader.

What these files are **not**: they carry no review record, so a run over one of
them is an inspection result at `limited` evidence, never `comparable`.

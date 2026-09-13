# Visualization Service Contract Fixture

`buck-contract-fixture/` is a small, integrity-checked, source-controlled
fixture for service and external-consumer tests.  It is intentionally not a
release demonstration: its commit identity is a sentinel value and its
`evidence_level` is `test_fixture`.

The local release gate serves this fixture only to prove the read-only service
and independent-consumer path.  Formal Buck and Boost packages generated from
the clean release commit are separate release evidence.

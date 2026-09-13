# Verified CI Environment (v0)

The v0 test suite was verified locally with this recommended environment:

```text
Python 3.12.7
pytest 7.4.4
jsonschema 4.23.0
numpy 1.26.4
```

`requirements-tested.txt` records the package pins. These pins are a tested
reference combination, not a claim that every platform is equivalent. The
runtime package remains dependency-light; PySpice and Ngspice require a
separate environment review and lock before migration.

The optional workbench has a separate Node/npm compatibility range and an exact
locally verified Node/npm pair. The distinction between admitted ranges and
the release-gate toolchain is defined in
[`docs/28_release_toolchain_policy_v0.md`](28_release_toolchain_policy_v0.md).

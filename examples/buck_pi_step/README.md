# Buck averaged test fixture

```powershell
pe-sim run examples/buck_pi_step/config.json --backend buck
```

This is a self-owned ideal averaged test fixture. It is **not** the release
Buck reference; the referenced switching Buck is constructed with PySpice and
executed by ngspice (see `examples/ngspice_buck_step/`). This fixture is for
adapter, runtime, and comparison verification only.

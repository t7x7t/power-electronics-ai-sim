# Buck reference: PySpice construction + ngspice execution

This is the reviewed Buck reference example. PySpice 1.5 builds the ideal
switching asynchronous Buck circuit through its object API; the generated
netlist is executed by a separately installed ngspice console executable.

```powershell
# dedicated Python 3.10-3.12 environment
pip install -r requirements-pyspice-tested.txt
python -m pip check
pe-sim backend doctor --name ngspice
pe-sim run examples/ngspice_buck_step/config.json --backend ngspice-buck --model-builder pyspice
```

Or with an explicit console executable:

```powershell
pe-sim run examples/ngspice_buck_step/config.json --backend ngspice-buck --model-builder pyspice --ngspice <path-to-ngspice_con.exe>
```

The native netlist builder remains available for offline tests and CI, but it
is not the reference construction path. PySpice native solver execution is not
used; execution stays on the reviewed ngspice process boundary. The model is
one ideal switching Buck circuit and does not include device, thermal, loss, or
hardware behavior. Scope and acceptance are defined in `docs/31`; the PySpice
construction boundary is in `docs/34`.
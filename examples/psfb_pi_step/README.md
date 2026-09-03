# PSFB PI Step Reference

Install the package and run:

```powershell
python -m pip install -e .
pe-sim psfb-step --output-dir runs --run-id psfb-pi-step
```

The run directory contains a manifest, qualification and safety records, metrics, events, and samples. The configured JSON can be run with `pe-sim run examples/psfb_pi_step/config.json`.

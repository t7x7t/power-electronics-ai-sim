from __future__ import annotations
import argparse
import json
from pathlib import Path

from .contracts import ExperimentSpec, Timebase
from .runtime import FakeLoadPlant, FakePIController, FakePlant, Runner


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Auditable power-electronics simulation runtime")
    parser.add_argument("--version", action="version", version="0.1.0")
    sub = parser.add_subparsers(dest="command")
    run = sub.add_parser("run", help="run an experiment from JSON config")
    run.add_argument("config", type=Path)
    run.add_argument("--backend", choices=("fake", "fake-load"), default="fake")
    demo = sub.add_parser("psfb-step", help="run the PSFB reference idealized load-step")
    demo.add_argument("--output-dir", type=Path, default=Path("runs"))
    demo.add_argument("--run-id", default="psfb-pi-step")
    args = parser.parse_args(argv)
    if args.command == "run":
        spec = ExperimentSpec.from_dict(json.loads(args.config.read_text(encoding="utf-8")))
        plant = FakeLoadPlant() if args.backend == "fake-load" else FakePlant()
        result = Runner().run(spec, plant, FakePIController())
    elif args.command == "psfb-step":
        spec = ExperimentSpec("psfb_pi_step", args.run_id, "psfb-ideal", "pi", Timebase(duration_s=0.02, control_period_s=0.001), input_schedule=({"time_s": 0.01, "vref": 0.8},), output_dir=str(args.output_dir))
        result = Runner().run(spec, FakePlant(gain=1.0, tau_s=0.004), FakePIController(kp=1.8))
    else:
        parser.print_help()
        return 0
    print(json.dumps({"status": result.status, "run_dir": str(result.run_dir), "qualification": result.qualification}, sort_keys=True))
    return 0 if result.status == "RUN_OK" else 1


if __name__ == "__main__":
    raise SystemExit(main())

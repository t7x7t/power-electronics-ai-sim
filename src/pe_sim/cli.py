from __future__ import annotations
import argparse
import json
from pathlib import Path

from .contracts import ExperimentSpec, Timebase, validate_experiment_document
from .runtime import FakeLoadPlant, FakePIController, FakePlant, Runner
from .reference_plants import BuckPlant, BoostPlant
from .dirty import FormalComparisonError, analyze_git_worktree
from .environment import check_recommended_environment
from .provenance import ExecutableBackendAdapter
from .reproducibility import audit_repeated_runs, compare_runs, cross_machine_evidence
from .baseline import build_baseline_report
from .visualization_service import serve as serve_visualization


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Auditable power-electronics simulation runtime")
    parser.add_argument("--version", action="version", version="0.2.1")
    sub = parser.add_subparsers(dest="command")
    run = sub.add_parser("run", help="run an experiment from JSON config")
    run.add_argument("config", type=Path)
    run.add_argument("--backend", choices=("fake", "fake-load", "buck", "boost"), default="fake")
    run.add_argument("--mode", choices=("exploratory", "formal_comparison"), default="exploratory")
    demo = sub.add_parser("psfb-step", help="run the PSFB reference idealized load-step")
    demo.add_argument("--output-dir", type=Path, default=Path("runs"))
    demo.add_argument("--run-id", default="psfb-pi-step")
    demo.add_argument("--mode", choices=("exploratory", "formal_comparison"), default="exploratory")
    status = sub.add_parser("git-status", help="inspect Git worktree readiness without modifying it")
    status.add_argument("path", nargs="?", type=Path, default=Path.cwd())
    environment = sub.add_parser("environment-check", help="check the verified and supported Python environment")
    environment.add_argument("--requirements", type=Path, default=None)
    environment.add_argument("--pyproject", type=Path, default=None)
    environment.add_argument("--backend-executable", type=Path, default=None, help="optional external backend executable to query")
    environment.add_argument("--backend-name", default="external-backend")
    compare = sub.add_parser("compare-runs", help="compare two published run directories")
    compare.add_argument("left", type=Path)
    compare.add_argument("right", type=Path)
    compare.add_argument("--tolerance", type=float, default=1e-9)
    audit = sub.add_parser(
        "audit-reproducibility",
        aliases=("reproducibility-audit", "audit-runs"),
        help="audit repeated runs pairwise on one machine",
    )
    audit.add_argument("runs", type=Path, nargs="+")
    audit.add_argument("--tolerance", type=float, default=1e-9)
    cross = sub.add_parser("cross-machine-evidence", help="build cross-machine reproducibility evidence matrix")
    cross.add_argument("runs", type=Path, nargs="+")
    baseline = sub.add_parser("baseline-report", help="build a release/baseline evidence template")
    baseline.add_argument("manifest", type=Path)
    baseline.add_argument("--output", type=Path, default=None)
    service = sub.add_parser("visualization-serve", help="serve verified visualization contracts read-only")
    service.add_argument("--runs-root", type=Path, default=Path("runs"))
    service.add_argument("--host", default="127.0.0.1")
    service.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    try:
        if args.command == "run":
            document = json.loads(args.config.read_text(encoding="utf-8"))
            # Validate the serialized contract before the compatibility reader
            # constructs an ExperimentSpec.  This prevents unknown or misspelled
            # fields from being silently ignored at the CLI boundary.
            validate_experiment_document(document)
            spec = ExperimentSpec.from_dict(document)
            plant = _build_plant(args.backend, spec.plant_config)
            controller = FakePIController(kp=float(spec.controller_config.get("kp", 1.0)))
            result = Runner().run(spec, plant, controller, mode=args.mode)
        elif args.command == "psfb-step":
            spec = ExperimentSpec("psfb_pi_step", args.run_id, "psfb-ideal", "pi", Timebase(duration_s=0.02, control_period_s=0.001), input_schedule=({"time_s": 0.01, "vref": 0.8},), output_dir=str(args.output_dir))
            result = Runner().run(spec, FakePlant(gain=1.0, tau_s=0.004), FakePIController(kp=1.8), mode=args.mode)
        elif args.command == "git-status":
            print(json.dumps(analyze_git_worktree(args.path).to_dict(), sort_keys=True))
            return 0
        elif args.command == "environment-check":
            adapters = () if args.backend_executable is None else (ExecutableBackendAdapter(args.backend_executable, name=args.backend_name),)
            result = check_recommended_environment(args.requirements, args.pyproject, backend_adapters=adapters)
            print(json.dumps(result.to_dict(), sort_keys=True))
            return 0 if result.status == "pass" else 1
        elif args.command == "compare-runs":
            result = compare_runs(args.left, args.right, tolerance=args.tolerance)
            print(json.dumps(result.to_dict(), sort_keys=True))
            return 0 if result.outcome in {"exact_match", "tolerance_match"} else 1
        elif args.command in {"audit-reproducibility", "reproducibility-audit", "audit-runs"}:
            result = audit_repeated_runs(args.runs, tolerance=args.tolerance)
            print(json.dumps(result.to_dict(), sort_keys=True))
            return 0 if result.reproducible else 1
        elif args.command == "cross-machine-evidence":
            result = cross_machine_evidence(args.runs)
            print(json.dumps(result, sort_keys=True))
            return 0 if result["status"] == "ready_for_trial" else 1
        elif args.command == "baseline-report":
            report = build_baseline_report(args.manifest)
            payload = json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
            if args.output is None:
                print(payload, end="")
            else:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(payload, encoding="utf-8")
                print(json.dumps({"status": "written", "path": str(args.output)}, sort_keys=True))
            return 0
        elif args.command == "visualization-serve":
            serve_visualization(args.runs_root, args.host, args.port)
            return 0
        else:
            parser.print_help()
            return 0
    except FormalComparisonError as exc:
        print(json.dumps({"status": "FORMAL_COMPARISON_REJECTED", "error": str(exc), "worktree_analysis": exc.analysis.to_dict()}, sort_keys=True))
        return 2
    except (OSError, RuntimeError, ValueError) as exc:
        # Configuration and preflight errors are machine-readable and must not
        # start a partial run. JSONDecodeError is a ValueError subclass and is
        # therefore covered here as well.
        print(json.dumps({"status": "CONFIG_REJECTED", "error": str(exc)}, sort_keys=True))
        return 2
    print(json.dumps({"status": result.status, "run_dir": str(result.run_dir), "qualification": result.qualification}, sort_keys=True))
    # A completed and qualified run is the normal successful CLI outcome.
    # Keep RUN_OK accepted for compatibility with callers that disable or
    # defer post-run qualification in a custom integration.
    return 0 if result.status in {"RUN_OK", "QUALIFIED"} else 1


def _build_plant(backend: str, config: dict[str, object]):
    """Instantiate a named backend from JSON-safe configuration values."""
    if backend == "fake":
        return FakePlant()
    if backend == "fake-load":
        return FakeLoadPlant()
    defaults = {
        "input_voltage_v": 12.0,
        "inductance_h": 100e-6,
        "capacitance_f": 470e-6,
        "load_resistance_ohm": 10.0,
        "integration_step_s": 1e-6,
    }
    values = dict(defaults)
    unknown = set(config) - set(values)
    if unknown:
        raise ValueError(f"unsupported {backend} plant_config fields: {sorted(unknown)}")
    values.update(config)
    plant_type = BuckPlant if backend == "buck" else BoostPlant
    return plant_type(**values)


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path

from .applicability import ApplicabilityPolicy
from .attestation import attest_run, verify_attestation
from .contracts import ExperimentSpec, Timebase, validate_experiment_document
from .qualification_rules import QualificationRulePack
from .runner import RunOptions
from .runtime import FakeLoadPlant, FakePIController, FakePlant, Runner
from .reference_plants import BuckPlant, BoostPlant
from .backends import (
    BackendConfigError,
    BackendNotFoundError,
    NativeBuckNetlistBuilder,
    NgspiceBuckPlant,
    NgspiceExecutableAdapter,
    PyspiceBuckNetlistBuilder,
    PyspiceUnavailableError,
    config_template,
    current_platform,
    get_backend,
    list_backends,
    pyspice_version,
    resolve_backend_executable,
    resolve_ngspice_executable,
    scan_for_backend,
    scan_for_ngspice,
    user_config_path,
    write_user_config,
)
from .dirty import FormalComparisonError, analyze_git_worktree
from .environment import check_recommended_environment
from .inspection import RunInspectionError, inspect_run
from .provenance import ExecutableBackendAdapter
from .reproducibility import audit_repeated_runs, compare_runs, cross_machine_evidence
from .baseline import build_baseline_report
from .visualization_service import serve as serve_visualization
from .workbench_export import export_run_to_workbench_fixture


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Auditable power-electronics simulation runtime")
    parser.add_argument("--version", action="version", version="0.5.0")
    sub = parser.add_subparsers(dest="command")
    run = sub.add_parser("run", help="run an experiment from JSON config")
    run.add_argument("config", type=Path)
    run.add_argument(
        "--backend",
        choices=("fake", "fake-load", "buck", "boost", "ngspice-buck"),
        default=None,
        help="backend adapter; defaults to the plant_id mapping and must match it",
    )
    run.add_argument("--mode", choices=("exploratory", "formal_comparison"), default="exploratory")
    run.add_argument(
        "--applicability",
        choices=("default", "record", "warn", "enforce", "off"),
        default="default",
        help="model-envelope policy; default records exploratory runs and enforces formal comparisons",
    )
    run.add_argument(
        "--no-standard-qualification",
        action="store_true",
        help="use only the baseline structural qualification checks",
    )
    run.add_argument(
        "--ngspice",
        default=None,
        help="reviewed ngspice executable; defaults to PE_SIM_NGSPICE or PATH",
    )
    run.add_argument(
        "--model-builder",
        choices=("native", "pyspice"),
        default="native",
        help="netlist builder for the ngspice-buck backend; 'pyspice' requires the optional PySpice install",
    )
    status = sub.add_parser("git-status", help="inspect Git worktree readiness without modifying it")
    status.add_argument("path", nargs="?", type=Path, default=Path.cwd())
    environment = sub.add_parser("environment-check", help="check the verified and supported Python environment")
    environment.add_argument("--requirements", type=Path, default=None)
    environment.add_argument("--pyproject", type=Path, default=None)
    environment.add_argument("--backend-executable", type=Path, default=None, help="optional external backend executable to query")
    environment.add_argument("--backend-name", default="external-backend")
    environment.add_argument(
        "--backend-profile",
        choices=("executable", "ngspice"),
        default="executable",
        help="provenance profile used with --backend-executable",
    )
    backend = sub.add_parser("backend", help="inspect external simulator discovery and configuration")
    backend_sub = backend.add_subparsers(dest="backend_command")
    backend_sub.add_parser("list", help="list registered backends and their status")
    guide = backend_sub.add_parser("guide", help="print installation guidance for a backend")
    guide.add_argument("--name", default="ngspice")
    guide.add_argument("--platform", choices=("windows", "linux", "macos"), default=None)
    setup = backend_sub.add_parser("setup", help="detect a backend, verify it, and optionally save its path")
    setup.add_argument("--name", default="ngspice")
    setup.add_argument("--executable", default=None, help="explicit executable path or command name")
    setup.add_argument("--config", type=Path, default=None, help="explicit user-level backends.toml path")
    setup.add_argument("--write-config", action="store_true", help="write the resolved path to the user-level config")
    doctor = backend_sub.add_parser("doctor", help="resolve and inspect an external simulator executable")
    doctor.add_argument("--name", default="ngspice")
    doctor.add_argument("--executable", default=None, help="explicit executable path or command name")
    doctor.add_argument("--config", type=Path, default=None, help="explicit user-level backends.toml path")
    scan = backend_sub.add_parser("scan", help="search local directories for simulator executables")
    scan.add_argument("--name", default="ngspice")
    scan.add_argument("--root", action="append", type=Path, required=True, help="directory to search; repeatable")
    scan.add_argument("--max-depth", type=int, default=4)
    backend_sub.add_parser("config-path", help="print the user-level configuration path")
    template = backend_sub.add_parser("template", help="print a copyable user configuration template")
    template.add_argument("--executable", default="<path-to-ngspice-console-executable>")
    template.add_argument("--name", default="ngspice")
    template.add_argument("--minimum-major", type=int, default=40)
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
    inspect = sub.add_parser("inspect-run", help="verify and summarize a published run package")
    inspect.add_argument("run_dir", type=Path)
    inspect.add_argument("--compact", action="store_true", help="emit single-line JSON")
    inspect.add_argument("--attestation", type=Path, default=None, help="optional detached attestation to verify")
    inspect.add_argument("--repo", type=Path, default=None, help="Git repository used for anchor verification")
    inspect.add_argument("--require-anchored", action="store_true", help="require trust level L1_anchored")
    attest = sub.add_parser("attest-run", help="write a detached attestation and Git anchor record")
    attest.add_argument("run_dir", type=Path)
    attest.add_argument("--repo", type=Path, default=None)
    attest.add_argument("--attestations-dir", type=Path, default=None)
    attest.add_argument("--no-anchor", action="store_true", help="write the attestation without a Git anchor")
    attest.add_argument("--replace", action="store_true", dest="replace_existing", help="supersede a different existing attestation")
    attest.add_argument("--compact", action="store_true", help="emit single-line JSON")
    verify_att = sub.add_parser("verify-attestation", help="verify a detached attestation and its Git anchor")
    verify_att.add_argument("run_dir", type=Path)
    verify_att.add_argument("--attestation", type=Path, required=True)
    verify_att.add_argument("--repo", type=Path, default=None)
    verify_att.add_argument("--no-git", action="store_true", help="skip the Git anchor check")
    verify_att.add_argument("--compact", action="store_true", help="emit single-line JSON")
    preview = sub.add_parser(
        "preview-netlist",
        help="render the reviewed circuit netlist without executing a simulation",
    )
    preview.add_argument("config", type=Path)
    preview.add_argument("--duty", type=float, default=0.5)
    preview.add_argument("--duration", type=float, default=None)
    preview.add_argument("--model-builder", choices=("native", "pyspice"), default="native")
    preview.add_argument("--ngspice", default=None)
    preview.add_argument("--output", type=Path, default=None)
    preview.add_argument("--json", action="store_true", dest="as_json")
    export = sub.add_parser(
        "export-workbench",
        help="export a verified run as the three read-only visualization contracts",
    )
    export.add_argument("run_dir", type=Path, help="published run-package directory")
    export.add_argument("destination", type=Path, help="derived fixture directory outside the run package")
    export.add_argument("--overwrite", action="store_true", help="replace existing derived fixture files")
    service = sub.add_parser("visualization-serve", help="serve verified visualization contracts read-only")
    service.add_argument("--runs-root", type=Path, default=Path("runs"))
    service.add_argument("--host", default="127.0.0.1")
    service.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    try:
        if args.command == "backend":
            return _run_backend_command(args)
        if args.command == "run":
            document = json.loads(args.config.read_text(encoding="utf-8"))
            # Validate the serialized contract before the compatibility reader
            # constructs an ExperimentSpec.  This prevents unknown or misspelled
            # fields from being silently ignored at the CLI boundary.
            validate_experiment_document(document)
            backend = _select_backend(document, args.backend)
            _validate_schedule_keys(document, backend)
            spec = ExperimentSpec.from_dict(document)
            plant = _build_plant(backend, spec.plant_config, spec.timebase, args.ngspice, args.model_builder)
            _validate_plant_adapter(backend, plant)
            controller = _build_controller(spec)
            qualification_plugins = (
                ()
                if args.no_standard_qualification
                else (QualificationRulePack.standard(plant=plant),)
            )
            options = RunOptions(
                mode=args.mode,
                applicability=ApplicabilityPolicy(mode=args.applicability),
                qualification_plugins=qualification_plugins,
            )
            result = Runner().run(spec, plant, controller, options=options)
        elif args.command == "git-status":
            print(json.dumps(analyze_git_worktree(args.path).to_dict(), sort_keys=True))
            return 0
        elif args.command == "environment-check":
            if args.backend_executable is None:
                adapters = ()
            elif args.backend_profile == "ngspice":
                adapters = (NgspiceExecutableAdapter(args.backend_executable),)
            else:
                adapters = (ExecutableBackendAdapter(args.backend_executable, name=args.backend_name),)
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
        elif args.command == "inspect-run":
            if args.require_anchored and args.attestation is None:
                raise ValueError("--require-anchored requires --attestation")
            inspection = inspect_run(
                args.run_dir,
                attestation_path=args.attestation,
                repo_root=args.repo,
                verify_anchor=not args.no_git if hasattr(args, "no_git") else True,
            )
            print(
                json.dumps(
                    inspection.to_dict(),
                    sort_keys=True,
                    indent=None if args.compact else 2,
                )
            )
            if inspection.status != "ok":
                return 2
            if args.attestation is not None:
                attestation = inspection.summary.get("attestation") or {}
                if attestation.get("status") != "ok":
                    return 2
                if args.require_anchored and attestation.get("trust_level") != "L1_anchored":
                    return 2
            return 0
        elif args.command == "attest-run":
            result = attest_run(
                args.run_dir,
                repo_root=args.repo,
                attestations_dir=args.attestations_dir,
                anchor=not args.no_anchor,
                replace_existing=args.replace_existing,
            )
            print(
                json.dumps(
                    result.to_dict(),
                    sort_keys=True,
                    indent=None if args.compact else 2,
                )
            )
            return 0 if result.status == "ATTESTED" else 2
        elif args.command == "verify-attestation":
            verification = verify_attestation(
                args.run_dir,
                args.attestation,
                repo_root=args.repo,
                check_git=not args.no_git,
            )
            print(
                json.dumps(
                    verification.to_dict(),
                    sort_keys=True,
                    indent=None if args.compact else 2,
                )
            )
            if verification.status != "ok":
                return 2
            return 0 if verification.trust_level == "L1_anchored" else 1
        elif args.command == "preview-netlist":
            return _run_preview_netlist(args)
        elif args.command == "export-workbench":
            exported = export_run_to_workbench_fixture(
                args.run_dir,
                args.destination,
                overwrite=args.overwrite,
            )
            print(json.dumps(exported.to_dict(), ensure_ascii=True, sort_keys=True, indent=2))
            return 0
        elif args.command == "visualization-serve":
            serve_visualization(args.runs_root, args.host, args.port)
            return 0
        else:
            parser.print_help()
            return 0
    except RunInspectionError as exc:
        print(json.dumps({"status": "RUN_INSPECTION_REJECTED", "error": str(exc)}, sort_keys=True))
        return 2
    except FormalComparisonError as exc:
        print(json.dumps({"status": "FORMAL_COMPARISON_REJECTED", "error": str(exc), "worktree_analysis": exc.analysis.to_dict()}, sort_keys=True))
        return 2
    except (OSError, RuntimeError, ValueError) as exc:
        # Configuration and preflight errors are machine-readable and must not
        # start a partial run. JSONDecodeError is a ValueError subclass and is
        # therefore covered here as well.
        print(json.dumps({"status": "CONFIG_REJECTED", "error": str(exc)}, sort_keys=True))
        return 2
    print(
        json.dumps(
            {
                "status": result.status,
                "run_dir": str(result.run_dir),
                "backend": backend,
                "plant_id": spec.plant_id,
                "plant_class": type(plant).__name__,
                "qualification": result.qualification,
            },
            sort_keys=True,
        )
    )
    # A completed and qualified run is the normal successful CLI outcome.
    # Keep RUN_OK accepted for compatibility with callers that disable or
    # defer post-run qualification in a custom integration.
    return 0 if result.status in {"RUN_OK", "QUALIFIED"} else 1


_PLANT_BACKENDS = {
    "fake": "fake",
    "fake-load": "fake-load",
    "buck": "buck",
    "boost": "boost",
    "ngspice-buck": "ngspice-buck",
}
_BACKEND_CLASSES = {
    "fake": "FakePlant",
    "fake-load": "FakeLoadPlant",
    "buck": "BuckPlant",
    "boost": "BoostPlant",
    "ngspice-buck": "NgspiceBuckPlant",
}


def _select_backend(document: dict[str, object], requested: str | None) -> str:
    """Map plant_id to a reviewed backend and fail closed on mismatch."""

    plant_id = str(document.get("plant_id", ""))
    expected = _PLANT_BACKENDS.get(plant_id)
    if expected is None:
        raise ValueError(
            f"plant_id {plant_id!r} has no reviewed CLI backend; supported: {sorted(_PLANT_BACKENDS)}"
        )
    backend = requested or expected
    if backend != expected:
        raise ValueError(
            f"plant_id {plant_id!r} requires --backend {expected!r}, not {backend!r}"
        )
    return backend


def _validate_schedule_keys(document: dict[str, object], backend: str) -> None:
    """Reject schedule keys that the selected Plant would silently ignore."""

    allowed = {"time_s", "vref"}
    if backend == "fake-load":
        allowed.add("load")
    elif backend in {"buck", "boost", "ngspice-buck"}:
        allowed.update({"vin_v", "input_voltage_v", "load_ohm", "load_resistance_ohm"})
    schedule = document.get("input_schedule", [])
    if not isinstance(schedule, list):
        raise ValueError("input_schedule must be an array")
    for index, item in enumerate(schedule):
        if not isinstance(item, dict):
            raise ValueError(f"input_schedule[{index}] must be an object")
        unknown = set(item) - allowed
        if unknown:
            raise ValueError(
                f"input_schedule[{index}] has keys not supported by backend {backend!r}: "
                f"{sorted(unknown)}; allowed: {sorted(allowed)}"
            )


def _validate_plant_adapter(backend: str, plant: object) -> None:
    """Reject a runtime adapter that does not match the declared backend."""

    expected = _BACKEND_CLASSES.get(backend)
    actual = type(plant).__name__
    if expected is not None and actual != expected:
        raise ValueError(f"backend {backend!r} built {actual!r}, expected {expected!r}")


def _build_controller(spec: ExperimentSpec):
    """Build the reviewed PI controller from validated configuration."""

    if spec.controller_id != "pi":
        raise ValueError(
            f"unsupported controller_id {spec.controller_id!r}; supported: ['pi']"
        )
    return FakePIController.from_config(spec.controller_config)


def _build_plant(backend: str, config: dict[str, object], timebase: Timebase | None = None, ngspice_executable: str | None = None, model_builder: str = "native"):
    """Instantiate a named backend from JSON-safe configuration values."""
    if backend in {"fake", "fake-load"}:
        allowed = {"gain", "tau_s"}
        unknown = set(config) - allowed
        if unknown:
            raise ValueError(f"unsupported {backend} plant_config fields: {sorted(unknown)}")
        values = dict(config)
        plant_type = FakePlant if backend == "fake" else FakeLoadPlant
        return plant_type(**values)
    if backend == "ngspice-buck":
        return _build_ngspice_buck(config, timebase, ngspice_executable, model_builder)
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


def _build_ngspice_buck(
    config: dict[str, object],
    timebase: Timebase | None,
    ngspice_executable: str | None,
    model_builder: str = "native",
    *,
    require_executable: bool = True,
):
    """Build the reviewed switching-level Buck adapter with explicit evidence."""
    defaults = {
        "input_voltage_v": 12.0,
        "inductance_h": 100e-6,
        "capacitance_f": 470e-6,
        "load_resistance_ohm": 10.0,
        "switching_frequency_hz": 100e3,
        "control_period_s": None,
        "switch_on_resistance_ohm": 1e-3,
        "switch_off_resistance_ohm": 1e9,
        "diode_saturation_current_a": 1e-12,
        "diode_emission_coefficient": 0.02,
        "timeout_s": 30.0,
        "sample_step_s": None,
        "keep_artifacts": False,
    }
    values = dict(defaults)
    unknown = set(config) - set(values)
    if unknown:
        raise ValueError(f"unsupported ngspice-buck plant_config fields: {sorted(unknown)}")
    values.update(config)
    control_period = values.pop("control_period_s")
    if control_period is None:
        if timebase is None:
            raise ValueError("ngspice-buck requires control_period_s in plant_config or timebase")
        control_period = timebase.control_period_s
    if require_executable:
        try:
            resolution = resolve_ngspice_executable(ngspice_executable)
        except (BackendNotFoundError, BackendConfigError) as exc:
            raise ValueError(str(exc)) from exc
        executable = resolution.executable
    else:
        # Preview and offline inspection do not execute the solver, so a
        # missing installation must not prevent reading the circuit template.
        executable = str(ngspice_executable or "ngspice")
    if model_builder == "pyspice":
        builder = PyspiceBuckNetlistBuilder()
        try:
            builder.check_available()
        except PyspiceUnavailableError as exc:
            raise ValueError(str(exc)) from exc
    else:
        builder = NativeBuckNetlistBuilder()
    return NgspiceBuckPlant(
        control_period_s=float(control_period),
        ngspice_executable=executable,
        netlist_builder=builder,
        **values,
    )


def _run_preview_netlist(args) -> int:
    """Render the reviewed netlist without starting a simulator."""

    document = json.loads(args.config.read_text(encoding="utf-8"))
    validate_experiment_document(document)
    backend = _select_backend(document, None)
    if backend != "ngspice-buck":
        raise ValueError("preview-netlist is available only for the ngspice-buck backend")
    _validate_schedule_keys(document, backend)
    spec = ExperimentSpec.from_dict(document)
    plant = _build_ngspice_buck(
        spec.plant_config,
        spec.timebase,
        args.ngspice,
        args.model_builder,
        require_executable=False,
    )
    plant.reset(spec.initial_state.to_dict())
    netlist = plant.preview_netlist(duty=args.duty, duration_s=args.duration)
    digest = hashlib.sha256(netlist.encode("utf-8")).hexdigest()
    duration_s = plant.control_period_s if args.duration is None else float(args.duration)
    summary = {
        "status": "rendered",
        "backend": backend,
        "plant_id": spec.plant_id,
        "duty": float(args.duty),
        "duration_s": duration_s,
        "model_builder": args.model_builder,
        "netlist_sha256": digest,
    }
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(netlist, encoding="utf-8")
        print(json.dumps({**summary, "output": str(args.output)}, sort_keys=True))
        return 0
    if args.as_json:
        print(json.dumps({**summary, "netlist": netlist}, sort_keys=True, indent=2))
    else:
        print(netlist, end="" if netlist.endswith("\n") else "\n")
    return 0


def _run_backend_command(args) -> int:
    """Inspect discovery and onboarding without modifying the checkout."""

    if args.backend_command == "list":
        print(
            json.dumps(
                {"platform": current_platform(), "backends": [item.to_dict() for item in list_backends()]},
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    if args.backend_command == "config-path":
        path = user_config_path()
        print(json.dumps({"path": str(path), "exists": path.is_file()}, sort_keys=True))
        return 0
    if args.backend_command == "template":
        print(
            config_template(
                args.executable,
                backend=args.name,
                minimum_major=args.minimum_major,
            ),
            end="",
        )
        return 0
    if args.backend_command == "guide":
        try:
            descriptor = get_backend(args.name)
        except KeyError as exc:
            print(json.dumps({"status": "UNKNOWN_BACKEND", "error": str(exc)}, sort_keys=True))
            return 2
        platform = args.platform or current_platform()
        print(
            json.dumps(
                {
                    "status": descriptor.status,
                    "backend": descriptor.name,
                    "platform": platform,
                    "kind": descriptor.kind,
                    "notes": list(descriptor.notes),
                    "install_recipes": [recipe.to_dict() for recipe in descriptor.recipes_for(platform)],
                    "config_path": str(user_config_path()),
                    "config_template": config_template(
                        "<path-to-executable>",
                        backend=descriptor.name,
                        minimum_major=descriptor.minimum_major,
                    )
                    if descriptor.kind == "executable"
                    else None,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    if args.backend_command == "scan":
        try:
            descriptor = get_backend(args.name)
        except KeyError as exc:
            print(json.dumps({"status": "UNKNOWN_BACKEND", "error": str(exc)}, sort_keys=True))
            return 2
        candidates = scan_for_backend(descriptor, args.root, max_depth=args.max_depth)
        payload = {
            "status": "FOUND" if candidates else "NOT_FOUND",
            "backend": descriptor.name,
            "roots": [str(Path(root)) for root in args.root],
            "candidates": [
                {"path": str(item), "preferred": item.name.lower() == (descriptor.console_sibling or "").lower()}
                for item in candidates
            ],
            "config_template": config_template(
                str(candidates[0]),
                backend=descriptor.name,
                minimum_major=descriptor.minimum_major,
            )
            if candidates
            else None,
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if candidates else 1
    if args.backend_command in {"doctor", "setup"}:
        try:
            descriptor = get_backend(args.name)
        except KeyError as exc:
            print(json.dumps({"status": "UNKNOWN_BACKEND", "error": str(exc)}, sort_keys=True))
            return 2
        config_path = args.config if args.config is not None else user_config_path()
        if descriptor.kind == "library":
            version = pyspice_version() if descriptor.name == "pyspice" else None
            payload = {
                "status": "known" if version else "NOT_FOUND",
                "backend": descriptor.name,
                "kind": descriptor.kind,
                "version": version,
                "notes": list(descriptor.notes),
                "install_recipes": [recipe.to_dict() for recipe in descriptor.recipes_for()],
                "config_path": str(config_path),
            }
            print(json.dumps(payload, indent=2, sort_keys=True))
            return 0 if version else 1
        if descriptor.status == "planned":
            payload = {
                "status": "PLANNED",
                "backend": descriptor.name,
                "notes": list(descriptor.notes),
                "install_recipes": [recipe.to_dict() for recipe in descriptor.recipes_for()],
                "config_path": str(config_path),
            }
            print(json.dumps(payload, indent=2, sort_keys=True))
            return 1
        try:
            resolution = resolve_backend_executable(
                descriptor,
                args.executable,
                config_path=args.config,
            )
        except (BackendNotFoundError, BackendConfigError) as exc:
            payload = {
                "status": "NOT_FOUND",
                "backend": descriptor.name,
                "error": str(exc),
                "config_path": str(config_path),
                "install_recipes": [recipe.to_dict() for recipe in descriptor.recipes_for()],
                "next_steps": [
                    f"install {descriptor.display_name} using one of the recipes above",
                    f"then run: pe-sim backend setup --name {descriptor.name} --executable <path> --write-config",
                ],
            }
            print(json.dumps(payload, indent=2, sort_keys=True))
            return 1
        evidence = dict(NgspiceExecutableAdapter(resolution.executable).backend_provenance())
        payload = {
            "status": evidence.get("status"),
            "resolution": resolution.to_dict(),
            "provenance": evidence,
            "config_path": str(config_path),
        }
        if getattr(args, "write_config", False):
            try:
                written = write_user_config(descriptor, resolution.executable, config_path=args.config)
            except BackendConfigError as exc:
                payload["config_write_error"] = str(exc)
                print(json.dumps(payload, indent=2, sort_keys=True))
                return 2
            payload["config_written"] = str(written)
            payload["next_steps"] = [
                f"verify: pe-sim backend doctor --name {descriptor.name}",
                "run: pe-sim run examples/ngspice_buck_step/config.json --backend ngspice-buck --model-builder pyspice",
            ]
        else:
            payload["next_steps"] = [
                "repeat with --write-config to save this path outside the repository",
            ]
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if evidence.get("status") == "known" else 1
    print(
        json.dumps(
            {
                "status": "USAGE",
                "error": "backend requires one of: list, guide, setup, doctor, scan, config-path, template",
            },
            sort_keys=True,
        )
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

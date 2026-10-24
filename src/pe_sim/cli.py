from __future__ import annotations
import argparse
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import sys
import time
from typing import Any, Mapping

from .applicability import ApplicabilityPolicy
from .attestation import attest_run, verify_attestation
from .cancellation import FileCancelToken
from .contracts import ExperimentSpec, validate_experiment_document
from .error_codes import RequestRejected, rejection_document
from .qualification_rules import QualificationRulePack
from .run_events import DEMO_RESULT_SCHEMA, RUN_RESULT_SCHEMA, ProgressPolicy
from .run_store import discovery_root
from .runner import RunOptions
from .runtime import FakePIController, Runner
from .builtin_plants import PlantBuildContext, load_builtin_plants
from .plant_registry import PlantRegistration, plant_catalog, resolve
from .time_model import TIME_MODEL_STEPPED, TIME_MODEL_WHOLE_RUN, TIME_MODELS
from .backends import (
    BackendConfigError,
    BackendNotFoundError,
    NgspiceExecutableAdapter,
    config_template,
    current_platform,
    get_backend,
    list_backends,
    pyspice_version,
    resolve_backend_executable,
    scan_for_backend,
    user_config_path,
    write_user_config,
)
from .dirty import FormalComparisonError, analyze_git_worktree
from .environment import check_recommended_environment
from .inspection import RunInspectionError, inspect_run
from .provenance import ExecutableBackendAdapter
from .reproducibility import ToleranceSpec, audit_repeated_runs, compare_runs, cross_machine_evidence
from .baseline import build_baseline_report
from .catalog import scan_runs
from .visualization_service import serve as serve_visualization
from .workbench_export import export_run_to_workbench_fixture


def _parse_signal_tolerances(values: list[str] | None) -> tuple[tuple[str, float, float], ...]:
    """Parse repeated ``NAME:ABSOLUTE:RELATIVE`` CLI tolerance declarations."""

    entries: list[tuple[str, float, float]] = []
    for raw in values or ():
        parts = str(raw).split(":")
        if len(parts) != 3:
            raise ValueError(f"--signal-tolerance expects NAME:ABSOLUTE:RELATIVE, got {raw!r}")
        name, absolute_text, relative_text = (part.strip() for part in parts)
        if not name:
            raise ValueError(f"--signal-tolerance {raw!r} has an empty signal name")
        try:
            absolute, relative = float(absolute_text), float(relative_text)
        except ValueError:
            raise ValueError(f"--signal-tolerance {raw!r} has a non-numeric tolerance") from None
        entries.append((name, absolute, relative))
    return tuple(entries)


def _cli_tolerance_policy(relative_tolerance: float, signal_tolerances: list[str] | None) -> ToleranceSpec | None:
    """Return a policy only when the caller opted in, so defaults stay unchanged."""

    signals = _parse_signal_tolerances(signal_tolerances)
    # Constructing the policy always validates ``--relative-tolerance`` (NaN,
    # infinity and negative values are rejected by ``ToleranceSpec``) even when
    # the zero/empty combination falls back to the historical scalar path.
    candidate = ToleranceSpec(absolute=0.0, relative=relative_tolerance, per_signal=signals)
    if relative_tolerance > 0 or signals:
        return candidate
    return None


def _emit_rejection(status: str, code: str, error: str, **extra: Any) -> None:
    """Print one versioned rejection envelope; the caller owns the exit code."""

    print(json.dumps(rejection_document(status, code, error, extra), sort_keys=True))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Auditable power-electronics simulation runtime")
    parser.add_argument("--version", action="version", version="0.5.12")
    sub = parser.add_subparsers(dest="command")
    run = sub.add_parser("run", help="run an experiment from JSON config")
    run.add_argument("config", type=Path)
    run.add_argument(
        "--backend",
        default=None,
        help="backend adapter; defaults to the plant_id registration and must match it",
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
    run.add_argument(
        "--progress",
        action="store_true",
        help="stream pe-sim.run-event.v1 progress events as JSON lines on stderr",
    )
    run.add_argument(
        "--cancel-file",
        type=Path,
        default=None,
        metavar="PATH",
        help="cancel the run when this path exists; checked once per control window",
    )
    demo = sub.add_parser(
        "demo",
        help="run a bundled reference experiment without writing a configuration first",
    )
    demo.add_argument(
        "directory",
        nargs="?",
        type=Path,
        default=Path("pe-sim-demo"),
        help="directory that receives config.json and runs/ (default: ./pe-sim-demo)",
    )
    demo.add_argument("--name", default="buck", help="bundled template name, for example buck or boost")
    demo.add_argument("--run-id", default=None, help="override the generated demo run id")
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
    compare.add_argument(
        "--relative-tolerance",
        type=float,
        default=0.0,
        help="relative tolerance scaled by the left-hand value magnitude",
    )
    compare.add_argument(
        "--signal-tolerance",
        action="append",
        default=[],
        metavar="NAME:ABSOLUTE:RELATIVE",
        help="per-signal tolerance override; repeatable",
    )
    audit = sub.add_parser(
        "audit-reproducibility",
        aliases=("reproducibility-audit", "audit-runs"),
        help="audit repeated runs pairwise on one machine",
    )
    audit.add_argument("runs", type=Path, nargs="+")
    audit.add_argument("--tolerance", type=float, default=1e-9)
    audit.add_argument(
        "--relative-tolerance",
        type=float,
        default=0.0,
        help="relative tolerance scaled by the left-hand value magnitude",
    )
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
    listing = sub.add_parser(
        "list-runs",
        help="discover run packages under a directory without verifying them",
    )
    listing.add_argument(
        "root",
        nargs="?",
        type=Path,
        default=None,
        help="directory to scan; resolution order: this argument, PE_SIM_RUNS_ROOT, ./runs",
    )
    listing.add_argument("--plant", default=None, help="only runs produced by this plant_id")
    listing.add_argument("--status", default=None, help="only runs with this recorded manifest status")
    listing.add_argument("--run-id-prefix", default=None)
    listing.add_argument("--published-only", action="store_true", help="only PUBLISHED packages")
    listing.add_argument("--limit", type=int, default=None)
    sub.add_parser(
        "list-plants",
        help="list registered plants and the inputs each one declares",
    )
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
    service.add_argument("--runs-root", type=Path, default=None, help="runs root; defaults to PE_SIM_RUNS_ROOT or ./runs")
    service.add_argument("--host", default="127.0.0.1")
    service.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    try:
        # The CLI is the composition root: it attaches the project-owned plant
        # registrations. The kernel never enumerates plants itself.  Kept
        # inside the try so a registration conflict is reported as JSON rather
        # than as a traceback.
        load_builtin_plants()
        if args.command == "backend":
            return _run_backend_command(args)
        if args.command == "demo":
            return _run_demo(args)
        if args.command == "run":
            code, payload = _execute_experiment(
                args.config,
                backend=args.backend,
                mode=args.mode,
                applicability=args.applicability,
                ngspice=args.ngspice,
                model_builder=args.model_builder,
                no_standard_qualification=args.no_standard_qualification,
                progress=args.progress,
                cancel_file=args.cancel_file,
            )
            print(json.dumps(payload, sort_keys=True))
            return code
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
            policy = _cli_tolerance_policy(args.relative_tolerance, args.signal_tolerance)
            if policy is None:
                result = compare_runs(args.left, args.right, tolerance=args.tolerance)
            else:
                result = compare_runs(args.left, args.right, tolerance=args.tolerance, tolerance_policy=policy)
            print(json.dumps(result.to_dict(), sort_keys=True))
            return 0 if result.outcome in {"exact_match", "tolerance_match"} else 1
        elif args.command in {"audit-reproducibility", "reproducibility-audit", "audit-runs"}:
            policy = _cli_tolerance_policy(args.relative_tolerance, None)
            if policy is None:
                result = audit_repeated_runs(args.runs, tolerance=args.tolerance)
            else:
                result = audit_repeated_runs(args.runs, tolerance=args.tolerance, tolerance_policy=policy)
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
        elif args.command == "list-plants":
            print(json.dumps(plant_catalog(discover=True), sort_keys=True, indent=2))
            return 0
        elif args.command == "list-runs":
            catalog = scan_runs(
                discovery_root(args.root),
                plant_id=args.plant,
                status=args.status,
                run_id_prefix=args.run_id_prefix,
                published_only=args.published_only,
                limit=args.limit,
            )
            print(json.dumps(catalog.to_dict(), sort_keys=True, indent=2))
            return 0
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
            serve_visualization(discovery_root(args.runs_root), args.host, args.port)
            return 0
        else:
            parser.print_help()
            return 0
    except RunInspectionError as exc:
        _emit_rejection("RUN_INSPECTION_REJECTED", "run_inspection_rejected", str(exc))
        return 2
    except FormalComparisonError as exc:
        _emit_rejection(
            "FORMAL_COMPARISON_REJECTED",
            "formal_comparison_rejected",
            str(exc),
            worktree_analysis=exc.analysis.to_dict(),
        )
        return 2
    except (OSError, RuntimeError, ValueError, TypeError) as exc:
        # Configuration and preflight errors are machine-readable and must not
        # start a partial run. JSONDecodeError is a ValueError subclass and is
        # therefore covered here as well. TypeError covers adapter protocol
        # violations so they surface as JSON rather than as a traceback.
        code = exc.code if isinstance(exc, RequestRejected) else "invalid_request"
        _emit_rejection("CONFIG_REJECTED", code, str(exc))
        return 2


def _execute_experiment(
    config: Path,
    *,
    backend: str | None,
    mode: str,
    applicability: str,
    ngspice: str | None,
    model_builder: str,
    no_standard_qualification: bool,
    progress: bool,
    cancel_file: Path | None,
) -> tuple[int, dict[str, Any]]:
    """Run one experiment document and return its terminal result.

    This is the single documented ``run`` path.  ``pe-sim demo`` calls it too,
    so the zero-configuration entry point cannot drift from the normal CLI.
    """

    document = json.loads(config.read_text(encoding="utf-8"))
    # Validate the serialized contract before the compatibility reader
    # constructs an ExperimentSpec.  This prevents unknown or misspelled
    # fields from being silently ignored at the CLI boundary.
    validate_experiment_document(document)
    registration = _select_registration(document, backend)
    _validate_schedule_keys(document, registration)
    if mode == "formal_comparison" and registration.time_model == TIME_MODEL_WHOLE_RUN:
        # The kernel cannot enforce constraints while the engine owns the
        # clock, so a whole-run route can never be a formal comparison.  Refuse
        # before building an adapter or a run directory so the reason is
        # branchable and nothing is left behind (docs/45).
        raise RequestRejected(
            "formal_comparison_rejected",
            f"plant_id {registration.plant_id!r} declares time_model "
            f"{TIME_MODEL_WHOLE_RUN!r}; formal comparison requires in-run "
            "enforcement, which this route cannot provide",
        )
    spec = ExperimentSpec.from_dict(document)
    plant = _build_plant(
        registration,
        spec.plant_config,
        PlantBuildContext(
            timebase=spec.timebase,
            ngspice_executable=ngspice,
            model_builder=model_builder,
        ),
    )
    _validate_plant_adapter(registration, plant)
    controller = _build_controller(spec)
    qualification_plugins = (
        ()
        if no_standard_qualification
        else (QualificationRulePack.standard(plant=plant),)
    )
    options = RunOptions(
        mode=mode,
        applicability=ApplicabilityPolicy(mode=applicability),
        qualification_plugins=qualification_plugins,
        progress=ProgressPolicy(sink=_stderr_event_sink) if progress else None,
        cancellation=FileCancelToken(cancel_file) if cancel_file is not None else None,
    )
    result = Runner().run(
        spec,
        plant,
        controller,
        options=options,
        time_model=registration.time_model,
    )
    payload = {
        "schema": RUN_RESULT_SCHEMA,
        "status": result.status,
        "run_id": spec.run_id,
        "run_dir": str(result.run_dir),
        "backend": registration.backend_id,
        "plant_id": spec.plant_id,
        "plant_class": type(plant).__name__,
        "evidence_level": result.evidence_level,
        "failure_category": result.failure_category,
        "qualification": result.qualification,
    }
    # A completed and qualified run is the normal successful CLI outcome.
    # Keep RUN_OK accepted for compatibility with callers that disable or
    # defer post-run qualification in a custom integration.
    return (0 if result.status in {"RUN_OK", "QUALIFIED"} else 1), payload


def _run_demo(args) -> int:
    """Write a bundled experiment document, run it, and describe the next steps.

    The demo reads only package data and public contracts: it creates no
    adapter, bypasses no check, and grants the caller no authority beyond
    starting a normal experimental run.
    """

    from importlib import resources

    directory = Path(args.directory).expanduser()
    directory.mkdir(parents=True, exist_ok=True)
    template = resources.files("pe_sim.templates").joinpath(f"{args.name}_pi_step.json")
    if not template.is_file():
        raise RequestRejected(
            "invalid_request",
            f"no bundled demo template named {args.name!r}; shipped templates are "
            "the JSON documents under pe_sim/templates",
        )
    document = json.loads(template.read_text(encoding="utf-8"))
    run_id = args.run_id or time.strftime("demo-%Y%m%d-%H%M%S", time.gmtime())
    document["run_id"] = str(run_id)
    document["output"]["directory"] = str((directory / "runs").resolve())
    config_path = directory / "config.json"
    config_path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")

    code, payload = _execute_experiment(
        config_path,
        backend=None,
        mode="exploratory",
        applicability="default",
        ngspice=None,
        model_builder="native",
        no_standard_qualification=False,
        progress=False,
        cancel_file=None,
    )
    print(
        json.dumps(
            {
                "schema": DEMO_RESULT_SCHEMA,
                "name": args.name,
                "config": str(config_path),
                "run": payload,
                "next": [
                    f"pe-sim inspect-run {payload['run_dir']}",
                    f"pe-sim visualization-serve --runs-root {directory / 'runs'}",
                ],
            },
            sort_keys=True,
        )
    )
    return code


def _stderr_event_sink(event: Mapping[str, Any]) -> None:
    """Write one event per line to stderr so stdout stays the terminal result."""

    print(json.dumps(event, sort_keys=True), file=sys.stderr, flush=True)


def _select_registration(document: dict[str, object], requested: str | None) -> PlantRegistration:
    """Resolve ``plant_id`` through the registry and check the requested backend.

    The registry is the only source of truth for which plants exist.  A miss
    triggers exactly one entry-point discovery, so a third-party distribution
    can contribute a plant without this module knowing its name.
    """

    plant_id = str(document.get("plant_id", ""))
    try:
        registration = resolve(plant_id)
    except KeyError as exc:
        raise RequestRejected("unknown_plant", str(exc)) from exc
    backend = requested or registration.backend_id
    if backend != registration.backend_id:
        raise RequestRejected(
            "backend_mismatch",
            f"plant_id {plant_id!r} requires --backend {registration.backend_id!r}, not {backend!r}",
        )
    return registration


def _validate_schedule_keys(document: dict[str, object], registration: PlantRegistration) -> None:
    """Reject schedule keys that the selected Plant would silently ignore."""

    allowed = set(registration.schedule_keys)
    schedule = document.get("input_schedule", [])
    if not isinstance(schedule, list):
        raise ValueError("input_schedule must be an array")
    for index, item in enumerate(schedule):
        if not isinstance(item, dict):
            raise ValueError(f"input_schedule[{index}] must be an object")
        unknown = set(item) - allowed
        if unknown:
            raise ValueError(
                f"input_schedule[{index}] has keys not supported by plant {registration.plant_id!r}: "
                f"{sorted(unknown)}; allowed: {sorted(allowed)}"
            )


# The declared time model decides which callables the adapter must expose.
# These are shape facts only: no engine, topology, or vendor name appears here.
_STEPPED_ADAPTER_METHODS = ("capabilities", "reset", "observe", "advance", "snapshot", "restore")
_WHOLE_RUN_ADAPTER_METHODS = ("trace_contract", "run_whole")


def _validate_plant_adapter(registration: PlantRegistration, plant: object) -> None:
    """Reject a runtime adapter that does not match the declared registration.

    An adapter must declare the same time model as its registration and expose
    the callables that model requires: a stepped adapter is driven by the kernel
    clock, a whole-run adapter owns its own time axis.  Only callables are
    inspected, so a third-party adapter never has to match an engine name.
    """

    actual = type(plant).__name__
    if actual != registration.class_name:
        raise ValueError(
            f"plant {registration.plant_id!r} built {actual!r}, expected {registration.class_name!r}"
        )
    actual_time_model = getattr(plant, "time_model", None)
    if actual_time_model is not None and str(actual_time_model) != registration.time_model:
        raise ValueError(
            f"plant {registration.plant_id!r} declares time_model {actual_time_model!r}, "
            f"but its registration declares {registration.time_model!r}"
        )
    if registration.time_model == TIME_MODEL_STEPPED:
        required = _STEPPED_ADAPTER_METHODS
    elif registration.time_model == TIME_MODEL_WHOLE_RUN:
        required = _WHOLE_RUN_ADAPTER_METHODS
    else:
        raise ValueError(
            f"plant {registration.plant_id!r} declares unknown time_model "
            f"{registration.time_model!r}; expected one of {list(TIME_MODELS)}"
        )
    missing = [name for name in required if not callable(getattr(plant, name, None))]
    if missing:
        raise ValueError(
            f"plant {registration.plant_id!r} declares time_model "
            f"{registration.time_model!r} but is missing required callables: {missing}"
        )


_PI_CONFIG_KEYS = frozenset(
    {"kp", "ki", "output_min", "output_max", "anti_windup", "measurement_key"}
)
_PID_CONFIG_KEYS = _PI_CONFIG_KEYS | {"kd", "kd_filter_tau_s", "kff_v"}


@dataclass(frozen=True)
class _ControllerBinding:
    """One built-in CLI controller and the configuration keys it accepts.

    The controller class is shared: a ``pi`` binding rejects derivative and
    feedforward keys instead of silently ignoring them, which keeps a typo or a
    copied PID configuration from looking like it took effect.
    """

    controller_id: str
    class_name: str
    config_keys: frozenset[str]


_CONTROLLER_BINDINGS: dict[str, _ControllerBinding] = {
    "pi": _ControllerBinding("pi", "FakePIController", _PI_CONFIG_KEYS),
    "pid": _ControllerBinding("pid", "FakePIController", _PID_CONFIG_KEYS),
}


def _build_controller(spec: ExperimentSpec):
    """Build the reviewed controller from a registered controller_id."""

    binding = _CONTROLLER_BINDINGS.get(spec.controller_id)
    if binding is None:
        raise ValueError(
            f"unsupported controller_id {spec.controller_id!r}; "
            f"supported: {sorted(_CONTROLLER_BINDINGS)}"
        )
    unknown = set(spec.controller_config) - binding.config_keys
    if unknown:
        raise ValueError(
            f"unsupported {spec.controller_id} controller_config fields: {sorted(unknown)}"
        )
    controller = FakePIController.from_config(spec.controller_config)
    if type(controller).__name__ != binding.class_name:
        raise ValueError(
            f"controller_id {spec.controller_id!r} built {type(controller).__name__!r}, "
            f"expected {binding.class_name!r}"
        )
    return controller


def _build_plant(
    registration: PlantRegistration,
    config: dict[str, object],
    context: PlantBuildContext,
):
    """Build one plant through its registered factory.

    The kernel and the CLI share this path: a plant is whatever its registration
    says it is, and the factory owns every construction detail.  The declared
    trust level is attached to the built object so the run evidence can record
    who supplied the adapter without the kernel consulting the registry.
    """

    plant = registration.factory(dict(config), context)
    if hasattr(plant, "__dict__"):
        plant.trust_level = registration.trust_level
        # A factory may declare its own route; the registration is only the
        # fallback.  Overwriting here would hide a disagreement from
        # ``_validate_plant_adapter``.
        if not hasattr(plant, "time_model"):
            plant.time_model = registration.time_model
    return plant


def _run_preview_netlist(args) -> int:
    """Render the reviewed netlist without starting a simulator."""

    document = json.loads(args.config.read_text(encoding="utf-8"))
    validate_experiment_document(document)
    registration = _select_registration(document, None)
    _validate_schedule_keys(document, registration)
    spec = ExperimentSpec.from_dict(document)
    plant = _build_plant(
        registration,
        spec.plant_config,
        PlantBuildContext(
            timebase=spec.timebase,
            ngspice_executable=args.ngspice,
            model_builder=args.model_builder,
            require_executable=False,
        ),
    )
    if not hasattr(plant, "preview_netlist"):
        raise ValueError(
            f"plant {registration.plant_id!r} does not declare a netlist preview"
        )
    plant.reset(spec.initial_state.to_dict())
    netlist = plant.preview_netlist(duty=args.duty, duration_s=args.duration)
    digest = hashlib.sha256(netlist.encode("utf-8")).hexdigest()
    duration_s = plant.control_period_s if args.duration is None else float(args.duration)
    summary = {
        "status": "rendered",
        "backend": registration.backend_id,
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
            _emit_rejection("UNKNOWN_BACKEND", "unknown_backend", str(exc))
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
            _emit_rejection("UNKNOWN_BACKEND", "unknown_backend", str(exc))
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
            _emit_rejection("UNKNOWN_BACKEND", "unknown_backend", str(exc))
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
                extra = {key: value for key, value in payload.items() if key != "status"}
                # The envelope status describes the refusal, not the backend
                # evidence the payload happened to carry at this point.
                _emit_rejection(
                    "CONFIG_REJECTED",
                    "invalid_request",
                    str(exc),
                    **extra,
                )
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
    _emit_rejection(
        "USAGE",
        "invalid_request",
        "backend requires one of: list, guide, setup, doctor, scan, config-path, template",
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

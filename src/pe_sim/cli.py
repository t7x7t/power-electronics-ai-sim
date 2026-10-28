from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import sys
import time
from typing import Any, Mapping

# Read the version from the package, never from a literal in this module.
from . import __version__
from .applicability import ApplicabilityPolicy
from .attestation import attest_run, verify_attestation
from .cancellation import FileCancelToken
from .capabilities import capability_report
from .contracts import ExperimentSpec
from .error_codes import REQUEST_REJECTION_CODES, RequestRejected, rejection_document
from .qualification_rules import QualificationRulePack
from .run_events import DEMO_RESULT_SCHEMA, RUN_RESULT_SCHEMA, ProgressPolicy
from .run_store import discovery_root
from .runner import RunOptions
from .runtime import Runner
from .study import parse_study_document
from .study_runner import run_study
from .capability_analysis import (
    CapabilityAnalysisError,
    parse_capability_document,
    run_capability_analysis,
    write_capability_report,
)
from .builtin_controllers import load_builtin_controllers
from .builtin_plants import PlantBuildContext, load_builtin_plants
from .circuit_compile import CircuitCompileError, compile_topology
from .circuit_devices import describe_devices, describe_device
from .controller_registry import ControllerBuildContext, resolve_controller
from .plant_parameters import PLANT_PARAMETERS, describe_plant
from .plant_registry import PlantRegistration, plant_catalog, resolve
from .spec_validation import (
    SPEC_VALIDATION_SCHEMA,
    SpecIssue,
    SpecValidationError,
    collect_issues,
    validate_document,
    validation_document,
)
from .topology_validation import (
    TOPOLOGY_VALIDATION_SCHEMA,
    TopologyValidationError,
    build_descriptor,
    topology_validation_document,
)
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
from .model_capabilities import CapabilityUnavailable
from .parameter_confirmation import (
    ParameterConfirmationError,
    build_confirmation,
    verify_confirmation,
    write_confirmation,
)
from .parameter_view import ParameterViewError, build_parameter_view
from .review import ReviewError, build_run_review, render_markdown, render_text
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


def _key_error_text(exc: KeyError) -> str:
    """Return a ``KeyError`` payload without the repr quoting ``str`` adds.

    ``describe`` and ``describe-devices`` raise ``KeyError`` for a misspelled
    name, and ``str(KeyError("x"))`` is ``"'x'"`` - quoting a message that is
    already prose makes the CLI output look like a serialisation bug.
    """

    return str(exc.args[0]) if exc.args else str(exc)


def _run_validate(args) -> int:
    """Validate one experiment document and print its issue envelope.

    This is the agent's compile-time loop.  It reads the document, the
    packaged (or explicit) JSON Schema, the plant's declared parameter surface,
    and the schedule vocabulary, then prints ``pe-sim.spec-validation.v1``.
    It never builds a plant, imports an adapter, starts a solver, or creates a
    run directory, so it is safe to call before any run exists and costs
    nothing when the document is wrong.

    ``--backend`` is a consistency assertion, not a build request: the
    registration is looked up only to compare ``backend_id``, and a mismatch
    is rejected with the existing ``backend_mismatch`` code.  The envelope is
    always plain JSON — this surface is machine-consumed, so a ``--json``
    switch would only add a way to get the wrong format.
    """

    try:
        document = json.loads(Path(args.config).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        _emit_rejection(
            "CONFIG_REJECTED",
            "invalid_request",
            str(exc),
            reason="invalid_document",
        )
        return 2
    if args.backend is not None and isinstance(document, Mapping):
        # A mismatch says the caller's claim about the document is wrong, so it
        # is a rejected request rather than one more validation issue.  Resolving
        # the claim needs the shipped registrations, so this is the one branch
        # that attaches the composition root.
        load_builtin_plants()
        _select_registration(document, args.backend)
    declared = document.get("plant_id") if isinstance(document, Mapping) else None
    payload = validation_document(
        document,
        issues=collect_issues(document, schema_path=args.schema),
        plant_id=declared if isinstance(declared, str) else None,
    )
    print(json.dumps(payload, sort_keys=True))
    return 0 if payload["ok"] is True else 2


def _run_describe(args) -> int:
    """Print one plant's declared parameter surface without building it.

    The declaration is data owned by ``pe_sim.plant_parameters``, so answering
    needs no adapter import, no solver probe, and no run directory.  An unknown
    id is reported with the closed ``unknown_plant`` code because a misspelled
    name is a request-shape problem, not a document issue.
    """

    try:
        payload = describe_plant(args.plant_id)
    except KeyError as exc:
        # Mirror the shape ``run`` uses for the same mistake, so a client needs
        # one parser for one class of error.  ``path`` points at the field an
        # experiment document would have carried the name in.
        message = _key_error_text(exc)
        issue = SpecIssue(
            code="unknown_plant",
            path="/plant_id",
            message=message,
            expected="one of: " + ", ".join(sorted(PLANT_PARAMETERS)),
        )
        _emit_rejection(
            "CONFIG_REJECTED",
            "unknown_plant",
            message,
            validation=SPEC_VALIDATION_SCHEMA,
            issues=[issue.to_dict()],
        )
        return 2
    print(json.dumps(payload, sort_keys=True))
    return 0


def _run_describe_devices(args) -> int:
    """Print the closed device vocabulary without loading any adapter.

    This is the other half of the authoring question: ``describe`` says what a
    ``plant_config`` may contain, and this says what a *topology* may contain.
    An unknown kind is reported the same way ``describe`` reports an unknown
    plant, so a client needs one parser for one class of mistake.
    """

    if args.device_kind is None:
        print(json.dumps(describe_devices(), sort_keys=True))
        return 0
    try:
        payload = describe_device(args.device_kind)
    except KeyError as exc:
        from .circuit_devices import CIRCUIT_DEVICES

        message = _key_error_text(exc)
        print(
            json.dumps(
                rejection_document(
                    "CONFIG_REJECTED",
                    "invalid_request",
                    message,
                    {
                        "validation": "pe-sim.circuit-devices.v1",
                        "issues": [
                            {
                                "code": "unsupported_device_kind",
                                "path": "/kind",
                                "message": message,
                                "expected": "one of: "
                                + ", ".join(sorted(CIRCUIT_DEVICES)),
                                "suggestion": None,
                                "severity": "error",
                            }
                        ],
                    },
                ),
                sort_keys=True,
            )
        )
        return 2
    print(json.dumps(payload, sort_keys=True))
    return 0


def _run_validate_topology(args) -> int:
    """Validate one topology document and print its issue envelope.

    Like ``validate``, this builds nothing and starts nothing, so it is safe to
    run before any circuit exists.  An unreadable file is a rejected request
    rather than a document issue, because no document was read.
    """

    try:
        document = json.loads(Path(args.topology).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        _emit_rejection(
            "CONFIG_REJECTED",
            "invalid_request",
            f"{args.topology}: {exc}",
            reason="invalid_document",
        )
        return 2
    payload = topology_validation_document(document)
    print(json.dumps(payload, sort_keys=True))
    return 0 if payload["ok"] is True else 2


def _run_compile_topology(args) -> int:
    """Compile one topology document into a netlist, without executing it.

    Validation runs first because a compile refusal and a document defect are
    different answers, and an author should see the document defects as a list
    rather than one refusal at a time.
    """

    try:
        document = json.loads(Path(args.topology).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        _emit_rejection(
            "CONFIG_REJECTED",
            "invalid_request",
            f"{args.topology}: {exc}",
            reason="invalid_document",
        )
        return 2

    envelope = topology_validation_document(document)
    if envelope["ok"] is not True:
        _emit_rejection(
            "CONFIG_REJECTED",
            "invalid_request",
            f"{args.topology}: topology document has "
            f"{envelope['error_count']} error(s)",
            validation=TOPOLOGY_VALIDATION_SCHEMA,
            issues=envelope["issues"],
        )
        return 2

    try:
        compiled = compile_topology(
            build_descriptor(document),
            duty=args.duty,
            duration_s=args.duration,
            sample_step_s=args.step,
            integrator=args.integrator,
        )
    except CircuitCompileError as exc:
        _emit_rejection(
            "CONFIG_REJECTED",
            "invalid_request",
            str(exc),
            compile_code=exc.code,
            details=exc.details,
        )
        return 2

    summary = compiled.to_dict()
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(compiled.netlist, encoding="utf-8")
        print(json.dumps({**summary, "output": str(args.output)}, sort_keys=True))
        return 0
    if args.as_json:
        print(json.dumps({**summary, "netlist": compiled.netlist}, sort_keys=True, indent=2))
    else:
        print(compiled.netlist, end="" if compiled.netlist.endswith("\n") else "\n")
    return 0


def _validate_run_document(document: dict[str, Any]) -> None:
    """Refuse a document before anything is built or written.

    The structured checker knows the shipped parameter tables and, through the
    registry, any plant a distribution registered.  A plant it cannot resolve
    at all is reported as ``unknown_plant``, which is the same outcome
    ``_select_registration`` would produce later - only earlier and with a
    machine-readable path, so no run directory is created for a typo.
    """

    validate_document(document)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Auditable power-electronics simulation runtime")
    parser.add_argument("--version", action="version", version=__version__)
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
    study = sub.add_parser(
        "study",
        help="run a study document as independent per-point experiments",
    )
    study.add_argument("config", type=Path)
    study.add_argument(
        "--backend",
        default=None,
        help="backend adapter; defaults to the plant_id registration and must match it",
    )
    study.add_argument("--mode", choices=("exploratory", "formal_comparison"), default="exploratory")
    study.add_argument(
        "--applicability",
        choices=("default", "record", "warn", "enforce", "off"),
        default="default",
        help="model-envelope policy; default records exploratory runs and enforces formal comparisons",
    )
    study.add_argument(
        "--no-standard-qualification",
        action="store_true",
        help="use only the baseline structural qualification checks",
    )
    study.add_argument(
        "--ngspice",
        default=None,
        help="reviewed ngspice executable; defaults to PE_SIM_NGSPICE or PATH",
    )
    study.add_argument(
        "--model-builder",
        choices=("native", "pyspice"),
        default="native",
        help="netlist builder for the ngspice-buck backend; 'pyspice' requires the optional PySpice install",
    )
    capability = sub.add_parser(
        "capability",
        help="run one AC, impedance, or loss analysis from a capability-analysis document",
    )
    capability.add_argument("config", type=Path)
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
    capabilities = sub.add_parser(
        "capabilities",
        help="report optional capabilities without starting a simulator",
    )
    capabilities.add_argument(
        "--require",
        action="append",
        default=[],
        metavar="CAPABILITY_ID",
        help="require this capability to be installed; repeatable",
    )
    capabilities.add_argument("--compact", action="store_true", help="emit single-line JSON")
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
    review_run = sub.add_parser(
        "review-run",
        help="print the hierarchical text review of a published run package",
    )
    review_run.add_argument("run_dir", type=Path)
    review_run.add_argument(
        "--format",
        choices=("text", "markdown"),
        default="text",
        help="rendered report format; ignored when --json is used",
    )
    review_run.add_argument(
        "--json",
        action="store_true",
        help="emit the pe-sim.run-review.v1 envelope instead of a rendered report",
    )
    parameter_view = sub.add_parser(
        "parameter-view",
        help="show the recorded parameter set of a published run package",
    )
    parameter_view.add_argument("run_dir", type=Path)
    parameter_view.add_argument("--compact", action="store_true", help="emit single-line JSON")
    confirm_parameters = sub.add_parser(
        "confirm-parameters",
        help="write a detached parameter confirmation outside the run package",
    )
    confirm_parameters.add_argument("run_dir", type=Path)
    confirm_parameters.add_argument("--reviewer", required=True, help="name of the person who reviewed the parameters")
    confirm_parameters.add_argument(
        "--statement",
        required=True,
        help="what was reviewed; a human confirmation statement of at least 10 characters",
    )
    confirm_parameters.add_argument(
        "--output",
        type=Path,
        required=True,
        help="confirmation file path; it must resolve outside the run package",
    )
    confirm_parameters.add_argument(
        "--timestamp",
        default=None,
        help="ISO-8601 UTC confirmation time; defaults to the current UTC time",
    )
    confirm_parameters.add_argument("--compact", action="store_true", help="emit single-line JSON")
    verify_parameters = sub.add_parser(
        "verify-parameter-confirmation",
        help="verify a detached parameter confirmation against a run package",
    )
    verify_parameters.add_argument("run_dir", type=Path)
    verify_parameters.add_argument("--confirmation", type=Path, required=True, help="detached confirmation file to verify")
    verify_parameters.add_argument("--compact", action="store_true", help="emit single-line JSON")
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
    validate = sub.add_parser(
        "validate",
        help="validate an experiment document without building a plant or starting a solver",
    )
    validate.add_argument("config", type=Path)
    validate.add_argument(
        "--schema",
        type=Path,
        default=None,
        help="experiment JSON Schema; defaults to the packaged schema",
    )
    validate.add_argument(
        "--backend",
        default=None,
        help="assert that the document's plant_id resolves to this backend",
    )
    describe = sub.add_parser(
        "describe",
        help="print the declared plant_config surface of one plant",
    )
    describe.add_argument("plant_id")
    devices = sub.add_parser(
        "describe-devices",
        help="print the closed device vocabulary a topology may use",
    )
    devices.add_argument("device_kind", nargs="?", default=None)
    validate_topology_parser = sub.add_parser(
        "validate-topology",
        help="validate a circuit topology document without compiling it",
    )
    validate_topology_parser.add_argument("topology", type=Path)
    compile_topology_parser = sub.add_parser(
        "compile-topology",
        help="compile a circuit topology document into a netlist without executing it",
    )
    compile_topology_parser.add_argument("topology", type=Path)
    compile_topology_parser.add_argument("--duty", type=float, default=0.5)
    compile_topology_parser.add_argument("--duration", type=float, default=1e-3)
    compile_topology_parser.add_argument("--step", type=float, default=None)
    compile_topology_parser.add_argument(
        "--integrator", choices=("trap", "gear", "euler"), default="trap"
    )
    compile_topology_parser.add_argument("--output", type=Path, default=None)
    compile_topology_parser.add_argument("--json", action="store_true", dest="as_json")
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
        # ``describe`` reads only the declared parameter table and ``validate``
        # only needs the registry when the document carries a schedule or the
        # caller asserts a backend, so both are served before the composition
        # root runs.  That keeps the two query commands answerable on a machine
        # with no simulator installed, which is exactly when an agent is still
        # deciding what to write.
        if args.command == "describe":
            return _run_describe(args)
        if args.command == "validate":
            return _run_validate(args)
        if args.command == "describe-devices":
            return _run_describe_devices(args)
        if args.command == "validate-topology":
            return _run_validate_topology(args)
        if args.command == "compile-topology":
            return _run_compile_topology(args)
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
        if args.command == "study":
            return _execute_study(args)
        if args.command == "capability":
            return _execute_capability(args)
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
        elif args.command == "capabilities":
            report = capability_report()
            known = {str(item["capability_id"]) for item in report["capabilities"]}
            required = list(dict.fromkeys(args.require))
            unknown = [item for item in required if item not in known]
            if unknown:
                _emit_rejection(
                    "CONFIG_REJECTED",
                    "invalid_request",
                    "unknown capability id(s): " + ", ".join(unknown),
                    known_capabilities=sorted(known),
                )
                return 2
            print(json.dumps(report, sort_keys=True, indent=None if args.compact else 2))
            statuses = {
                str(item["capability_id"]): str(item["status"])
                for item in report["capabilities"]
            }
            return 0 if all(statuses[item] == "installed" for item in required) else 1
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
        elif args.command == "review-run":
            review = build_run_review(args.run_dir)
            if args.json:
                print(json.dumps(review, sort_keys=True, indent=2))
            elif args.format == "markdown":
                print(render_markdown(review), end="")
            else:
                print(render_text(review), end="")
            return 0
        elif args.command == "parameter-view":
            view = build_parameter_view(args.run_dir)
            print(
                json.dumps(
                    view,
                    sort_keys=True,
                    indent=None if args.compact else 2,
                )
            )
            return 0
        elif args.command == "confirm-parameters":
            confirmation = build_confirmation(
                args.run_dir,
                reviewer=args.reviewer,
                statement=args.statement,
                confirmed_at_utc=args.timestamp,
            )
            written = write_confirmation(
                args.output, confirmation, run_dir=args.run_dir
            )
            print(
                json.dumps(
                    {
                        "status": "written",
                        "path": str(written) if written is not None else str(args.output),
                        "run_id": confirmation["run"]["run_id"],
                        "parameter_set_digest": confirmation["parameter_set"]["digest"],
                        "confirmation_digest": confirmation["confirmation_digest"],
                    },
                    sort_keys=True,
                    indent=None if args.compact else 2,
                )
            )
            return 0
        elif args.command == "verify-parameter-confirmation":
            confirmation_path = Path(args.confirmation)
            try:
                confirmation = json.loads(
                    confirmation_path.read_text(encoding="utf-8-sig")
                )
            except (OSError, ValueError) as exc:
                _emit_rejection(
                    "RUN_INSPECTION_REJECTED",
                    "run_inspection_rejected",
                    f"cannot read parameter confirmation {confirmation_path}: {exc}",
                    scope="parameter_confirmation",
                )
                return 2
            verification = verify_confirmation(
                args.run_dir,
                confirmation,
                confirmation_path=confirmation_path,
            )
            print(
                json.dumps(
                    verification,
                    sort_keys=True,
                    indent=None if args.compact else 2,
                )
            )
            status = verification.get("status")
            if status == "unknown":
                return 2
            return 0 if status == "current" else 1
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
    except (ParameterViewError, ParameterConfirmationError) as exc:
        # A parameter view or confirmation refusal is a run-inspection outcome:
        # the package was not read or the record was not written.  The extra
        # ``scope`` field discriminates the two surfaces without adding a new
        # rejection code to the closed vocabulary.
        _emit_rejection(
            "RUN_INSPECTION_REJECTED",
            "run_inspection_rejected",
            str(exc),
            scope="parameter_view" if isinstance(exc, ParameterViewError) else "parameter_confirmation",
        )
        return 2
    except ReviewError as exc:
        # A refused review is an inspection outcome, not a new error family:
        # the package was not read, or it did not support the envelope.
        _emit_rejection(
            "RUN_INSPECTION_REJECTED",
            "run_inspection_rejected",
            str(exc),
            scope="run_review",
        )
        return 2
    except FormalComparisonError as exc:
        _emit_rejection(
            "FORMAL_COMPARISON_REJECTED",
            "formal_comparison_rejected",
            str(exc),
            worktree_analysis=exc.analysis.to_dict(),
        )
        return 2
    except CapabilityUnavailable as exc:
        # A missing or self-certified capability is a rejected request, not a
        # traceback.  The closed reason vocabulary travels in ``extra`` so a
        # client can branch without a new rejection code.
        _emit_rejection(
            "CONFIG_REJECTED",
            "invalid_request",
            str(exc),
            reason=exc.reason,
        )
        return 2
    except SpecValidationError as exc:
        # ``SpecValidationError`` is a ``ValueError`` subclass, so this branch
        # must come before the generic one: the structured issue list is the
        # whole point of the surface, and the generic envelope would flatten it
        # back into one string the agent cannot repair from.
        #
        # An issue code that also names a rejection code keeps that code.  A
        # client already branching on ``unknown_plant`` must not have to start
        # parsing ``issues`` to recognise the outcome it already understands.
        mapped = [
            issue
            for issue in exc.issues
            if issue.severity == "error" and issue.code in REQUEST_REJECTION_CODES
        ]
        _emit_rejection(
            "CONFIG_REJECTED",
            mapped[0].code if mapped else "invalid_request",
            str(exc),
            validation=SPEC_VALIDATION_SCHEMA,
            issues=[issue.to_dict() for issue in exc.issues],
        )
        return 2
    except (OSError, RuntimeError, ValueError, TypeError) as exc:
        # Configuration and preflight errors are machine-readable and must not
        # start a partial run. JSONDecodeError is a ValueError subclass and is
        # therefore covered here as well. TypeError covers adapter protocol
        # violations so they surface as JSON rather than as a traceback.
        code = exc.code if isinstance(exc, RequestRejected) else "invalid_request"
        # ``docs/45`` documents one status per refusal family.  A route that
        # cannot support a formal comparison is refused for provenance reasons
        # even when the worktree is clean, and a client branching on ``status``
        # must not read that as a configuration mistake.
        status = (
            "FORMAL_COMPARISON_REJECTED"
            if code == "formal_comparison_rejected"
            else "CONFIG_REJECTED"
        )
        _emit_rejection(status, code, str(exc))
        return 2


def _execute_capability(args) -> int:
    """Run one capability-analysis document and print its JSON report."""

    try:
        document = json.loads(Path(args.config).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        _emit_rejection(
            "CONFIG_REJECTED",
            "invalid_request",
            str(exc),
            reason="invalid_document",
        )
        return 2
    try:
        plan = parse_capability_document(document)
        report = run_capability_analysis(plan)
        if plan.output_dir is not None:
            report_path = write_capability_report(report, plan.output_dir)
            report = {**report, "report_path": str(report_path)}
        print(json.dumps(report, sort_keys=True, allow_nan=False))
        return 0
    except CapabilityAnalysisError as exc:
        code = "unknown_plant" if exc.reason == "unknown_plant" else "invalid_request"
        _emit_rejection("CONFIG_REJECTED", code, str(exc), reason=exc.reason)
        return 2
    except CapabilityUnavailable as exc:
        _emit_rejection(
            "CONFIG_REJECTED",
            "invalid_request",
            str(exc),
            reason=exc.reason,
        )
        return 2
    except (TypeError, ValueError) as exc:
        _emit_rejection(
            "CONFIG_REJECTED",
            "invalid_request",
            str(exc),
            reason="invalid_plan",
        )
        return 2


def _execute_study(args) -> int:
    """Run one study document as independent, ordinary per-point runs.

    Parsing and the per-point preflight happen before any directory is
    created: a document that cannot be planned, or a point whose plant/route
    would be rejected by ``run``, must leave the output directory untouched.
    """

    try:
        document = json.loads(Path(args.config).read_text(encoding="utf-8"))
        plan = parse_study_document(document)
    except (OSError, ValueError) as exc:
        # StudyPlanError is a ValueError; a malformed document is a rejected
        # request, not a partially created study.
        _emit_rejection("CONFIG_REJECTED", "invalid_request", str(exc))
        return 2

    for point in plan.points:
        point_document = dict(point.document)
        registration = _select_registration(point_document, args.backend)
        _validate_schedule_keys(point_document, registration)
        if args.mode == "formal_comparison" and registration.time_model == TIME_MODEL_WHOLE_RUN:
            # Same refusal as ``run``: an engine that owns the clock cannot be
            # constrained window by window, so it cannot support a formal
            # comparison.  No point may start before every point passes.
            raise RequestRejected(
                "formal_comparison_rejected",
                f"plant_id {registration.plant_id!r} declares time_model "
                f"{TIME_MODEL_WHOLE_RUN!r}; formal comparison requires in-run "
                "enforcement, which this route cannot provide",
            )

    def execute_point(point, config_path):
        return _execute_experiment(
            Path(config_path),
            backend=args.backend,
            mode=args.mode,
            applicability=args.applicability,
            ngspice=args.ngspice,
            model_builder=args.model_builder,
            no_standard_qualification=args.no_standard_qualification,
            progress=False,
            cancel_file=None,
        )

    result = run_study(plan, execute_point=execute_point)
    print(json.dumps(result.report, sort_keys=True))
    return result.exit_code


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

    try:
        document = json.loads(config.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        # ``json`` reports a bare position, and the caller may be editing
        # several files at once, so the path has to travel with the message.
        raise RequestRejected(
            "invalid_request", f"{config}: {exc}"
        ) from exc
    # Validate the serialized contract before the compatibility reader
    # constructs an ExperimentSpec.  This prevents unknown or misspelled
    # fields from being silently ignored at the CLI boundary.  When the plant
    # declares a parameter surface the structured validator also checks it and
    # raises ``SpecValidationError``, so the CLI can return the issue list
    # instead of one flattened string.
    _validate_run_document(document)
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
        control_location=registration.control_location,
        event_granularity=registration.event_granularity,
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


def _build_controller(spec: ExperimentSpec):
    """Build the controller declared by a registered controller_id.

    The registry supplies both the factory and the accepted configuration key
    set.  Loading the built-ins here keeps every CLI entry point working even
    when a caller never asked for the composition root explicitly; the loader
    is idempotent.
    """

    load_builtin_controllers()
    return resolve_controller(
        spec.controller_id,
        spec.controller_config,
        ControllerBuildContext(control_period_s=spec.timebase.control_period_s),
    )


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
    # Same structured pre-flight as ``run``: a preview is cheap, but a caller
    # repairing a document should see the pointer-addressed issue here too.
    validate_document(document)
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
    plant.reset(spec.initial_state.reset_arguments())
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

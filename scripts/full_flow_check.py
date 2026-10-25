#!/usr/bin/env python3
"""End-to-end flow check for the documented pe-sim path (``docs/56``).

The check walks the documented user and agent path in order::

    environment -> capabilities -> agent-surface -> configure -> run ->
    debug -> inspect -> export -> service -> audit -> compare

Every CLI call is a subprocess (``[sys.executable, "-m", "pe_sim.cli", ...]``),
every artifact is written under ``--output``, and no published package outside
that directory is modified.  The check is a product-phase smoke check: it is
not a release gate, not a physical validation, and not a substitute for the
local release check.

Exit codes: ``0`` pass, ``2`` partial, ``1`` fail.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

FULL_FLOW_SCHEMA = "pe-sim.full-flow-check.v1"
FULL_FLOW_RULE_VERSION = "full-flow-check-v1"
STEP_ORDER = (
    "environment",
    "capabilities",
    "agent-surface",
    "configure",
    "run",
    "debug",
    "inspect",
    "export",
    "service",
    "audit",
    "compare",
)
REQUIRED_STEPS = frozenset(
    {
        "environment",
        "capabilities",
        "agent-surface",
        "configure",
        "run",
        "debug",
        "inspect",
        "export",
        "compare",
    }
)
STEP_TITLES = {
    "environment": "Python environment and verified pins",
    "capabilities": "Capability document and registered backends",
    "agent-surface": "Non-interactive agent commands",
    "configure": "Buck + PI + timebase experiment document",
    "run": "Experiment run to a published package",
    "debug": "Rejected configuration and tampered package",
    "inspect": "Inspection, parameter view, and review schemas",
    "export": "Visualization contracts exported",
    "service": "Loopback visualization service",
    "audit": "Detached attestation and verification",
    "compare": "Second run and comparison",
}
PUBLISHABLE_RUN_STATUSES = frozenset({"RUN_OK", "QUALIFIED", "COMPARABLE"})
INSPECTION_SCHEMA = "pe-sim.run-inspection.v1"
PARAMETER_VIEW_SCHEMA = "pe-sim.parameter-view.v1"
REVIEW_SCHEMA = "pe-sim.run-review.v1"
REVIEW_TEXT_PREFIX = "pe-sim run review:"
COMPARE_MATCH_OUTCOMES = frozenset({"exact_match", "tolerance_match"})
DEFAULT_STEP_TIMEOUT_S = 300.0
SERVICE_START_TIMEOUT_S = 15.0
SERVICE_STOP_TIMEOUT_S = 5.0
SERVICE_POLL_INTERVAL_S = 0.2
PRIMARY_RUN_ID = "flow-buck"
COPY_RUN_ID = "flow-buck-copy"
INVALID_RUN_ID = "flow-invalid"
_SCHEMA_KEYS = ("schema", "schema_version", "schemaVersion", "document_schema")
_CAPABILITY_LIST_KEYS = ("capabilities", "capability_list", "items", "entries", "backends", "registered_backends")
_CAPABILITY_ID_KEYS = ("capability_id", "id", "name", "backend_id", "capability")
_ATTESTATION_PATH_KEYS = ("attestation_path", "attestation_file", "path", "attestation")
_ERROR_SNIPPETS = ("unrecognized arguments", "invalid choice", "the following arguments are required")


class StepFailure(RuntimeError):
    """One assertion inside a step did not hold."""


@dataclass
class _Step:
    """Accumulates one step result without ever aborting the check."""

    id: str
    title: str
    required: bool
    status: str = "pass"
    detail: str = ""
    evidence: list[str] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)

    def record(self, message: str) -> None:
        self.evidence.append(message)

    def limit(self, message: str) -> None:
        if message not in self.limitations:
            self.limitations.append(message)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "required": self.required,
            "status": self.status,
            "detail": self.detail,
            "evidence": list(self.evidence),
        }


@dataclass
class _Context:
    """Paths and policies shared by the steps."""

    source_root: Path
    output_dir: Path
    ngspice: str | None
    skip_service: bool
    skip_audit: bool
    timeout_s: float = DEFAULT_STEP_TIMEOUT_S
    run_dir: Path | None = None

    @property
    def runs_root(self) -> Path:
        return self.output_dir / "runs"

    def cli(self, arguments: Sequence[Any], *, timeout: float | None = None) -> subprocess.CompletedProcess:
        return _run_cli(
            arguments,
            cwd=self.source_root,
            timeout=self.timeout_s if timeout is None else timeout,
        )


def _python_version() -> str:
    return "{}.{}.{}".format(*sys.version_info[:3])


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _default_source_root() -> Path:
    candidate = Path(__file__).resolve().parents[1]
    if (candidate / "src" / "pe_sim").is_dir():
        return candidate
    return Path.cwd().resolve()


def _default_output_dir(source_root: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return source_root / ".tmp" / "full-flow" / stamp


def _command_text(arguments: Sequence[Any]) -> str:
    return " ".join(["pe-sim", *[str(item) for item in arguments]])


def _one_line(value: Any, limit: int = 240) -> str:
    collapsed = " ".join(str(value).split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 3] + "..."


def _run_cli(arguments: Sequence[Any], *, cwd: Path, timeout: float) -> subprocess.CompletedProcess:
    """Run one CLI step as a subprocess with the documented stream settings."""

    command = [sys.executable, "-m", "pe_sim.cli", *[str(item) for item in arguments]]
    try:
        return subprocess.run(
            command,
            cwd=str(cwd),
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except subprocess.TimeoutExpired as exc:
        raise StepFailure(
            f"{_command_text(arguments)} timed out after {timeout:.0f} s"
        ) from exc
    except OSError as exc:
        raise StepFailure(
            f"{_command_text(arguments)} could not be started: {type(exc).__name__}: {exc}"
        ) from exc


def _json_document(text: str) -> dict[str, Any] | None:
    """Return the first JSON object found in ``text``, or ``None``."""

    stripped = (text or "").strip()
    if not stripped:
        return None
    candidates: list[str] = [stripped]
    for line in reversed(stripped.splitlines()):
        candidate = line.strip()
        if candidate.startswith("{") and candidate.endswith("}"):
            candidates.append(candidate)
            break
    start, end = stripped.find("{"), stripped.rfind("}")
    if 0 <= start < end:
        candidates.append(stripped[start : end + 1])
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except (TypeError, ValueError):
            continue
        if isinstance(value, dict):
            return value
    return None


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        return _json_document(path.read_text(encoding="utf-8"))
    except OSError:
        return None


def _write_json(path: Path, document: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")


def _reset_path(path: Path) -> None:
    """Remove a path the check owns so a repeated run starts clean."""

    try:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path, ignore_errors=True)
        elif path.exists() or path.is_symlink():
            path.unlink()
    except OSError:
        pass


def _require_document(document: dict[str, Any] | None, label: str) -> dict[str, Any]:
    if document is None:
        raise StepFailure(f"{label} did not yield a JSON object")
    return document


def _require_exit_zero(completed: subprocess.CompletedProcess, label: str) -> None:
    if completed.returncode != 0:
        raise StepFailure(
            f"{label} exited {completed.returncode}: {_one_line(completed.stderr or completed.stdout)}"
        )


def _usage_rejection(completed: subprocess.CompletedProcess) -> bool:
    if completed.returncode != 2:
        return False
    message = (completed.stderr or "") + (completed.stdout or "")
    return any(snippet in message for snippet in _ERROR_SNIPPETS)


def _schema_tag(document: Mapping[str, Any]) -> tuple[str, str] | None:
    for key in _SCHEMA_KEYS:
        value = document.get(key)
        if isinstance(value, str) and value:
            return key, value
    for key, value in document.items():
        if "schema" in str(key).lower() and isinstance(value, str) and value:
            return str(key), value
    return None


def _require_schema(
    document: Mapping[str, Any], expected: str, label: str
) -> tuple[str, str]:
    tag = _schema_tag(document)
    if tag is None:
        raise StepFailure(f"{label} does not carry a schema tag")
    key, value = tag
    if value != expected:
        raise StepFailure(f"{label} carries schema {value!r} instead of {expected!r}")
    return key, value


def _entry_like(value: Any) -> bool:
    return (
        isinstance(value, list)
        and bool(value)
        and all(isinstance(item, Mapping) for item in value)
        and any(any(key in item for key in _CAPABILITY_ID_KEYS) for item in value)
    )


def _capability_listing(document: Mapping[str, Any]) -> tuple[str, list[Mapping[str, Any]]] | None:
    """Find a non-empty backend/capability listing without assuming one key."""

    containers: list[Mapping[str, Any]] = [document]
    for nested_key in ("document", "report", "capabilities_report", "capability_report"):
        nested = document.get(nested_key)
        if isinstance(nested, Mapping):
            containers.append(nested)
    for container in containers:
        for key in _CAPABILITY_LIST_KEYS:
            value = container.get(key)
            if _entry_like(value):
                return key, list(value)
        for key, value in container.items():
            if key in _CAPABILITY_LIST_KEYS:
                continue
            if _entry_like(value):
                return str(key), list(value)
    return None


def _attestation_path(document: Mapping[str, Any], source_root: Path) -> tuple[str, Path] | None:
    for key in _ATTESTATION_PATH_KEYS:
        value = document.get(key)
        if isinstance(value, Mapping):
            value = value.get("path") or value.get("attestation_path")
        if isinstance(value, str) and value:
            candidate = Path(value)
            if not candidate.is_absolute():
                candidate = source_root / candidate
            return key, candidate
    return None


def _require_run_dir(context: _Context) -> Path:
    if context.run_dir is None:
        raise StepFailure("no published run package is available; the run step did not complete")
    if not (context.run_dir / "manifest.json").is_file():
        raise StepFailure(f"the run package has no manifest: {context.run_dir}")
    return context.run_dir


# -- steps -----------------------------------------------------------------


def _step_environment(context: _Context, step: _Step) -> None:
    arguments: list[Any] = [
        "environment-check",
        "--requirements",
        "requirements-tested.txt",
        "--pyproject",
        "pyproject.toml",
    ]
    if context.ngspice is not None:
        arguments += [
            "--backend-executable",
            context.ngspice,
            "--backend-name",
            "ngspice",
            "--backend-profile",
            "ngspice",
        ]
    command = _command_text(arguments)
    completed = context.cli(arguments)
    step.record(f"{command} -> exit {completed.returncode}")
    document = _require_document(_json_document(completed.stdout), command)
    status = str(document.get("status") or "unknown")
    python = document.get("python")
    python = python if isinstance(python, Mapping) else {}
    packages = document.get("packages")
    packages = packages if isinstance(packages, Mapping) else {}
    actual = str(python.get("actual") or "unknown")
    step.record(
        "python.actual={} (supported_status={}, verified_status={}, verified={})".format(
            actual,
            python.get("supported_status"),
            python.get("verified_status"),
            python.get("verified"),
        )
    )
    for name in sorted(packages):
        entry = packages[name] if isinstance(packages[name], Mapping) else {}
        step.record(
            "package {}: verified_status={} supported_status={} actual={} verified={}".format(
                name,
                entry.get("verified_status"),
                entry.get("supported_status"),
                entry.get("actual"),
                entry.get("verified"),
            )
        )
    backend = document.get("backend")
    backend = backend if isinstance(backend, Mapping) else {}
    if context.ngspice is not None:
        raw_records = backend.get("records")
        records = (
            [item for item in raw_records if isinstance(item, Mapping)]
            if isinstance(raw_records, Sequence) and not isinstance(raw_records, (str, bytes))
            else []
        )
        for record in records:
            step.record(
                "backend {}: status={} version={} digest={}".format(
                    record.get("name"),
                    record.get("status"),
                    record.get("version"),
                    record.get("executable_sha256") or record.get("sha256"),
                )
            )
        if str(backend.get("status")) != "known" or not records:
            raise StepFailure(
                "environment-check did not confirm the supplied ngspice executable "
                f"(backend status {backend.get('status')!r})"
            )
        versions = [str(item.get("version")) for item in records if item.get("version")]
        if not versions:
            raise StepFailure(
                "environment-check recorded no version for the supplied ngspice executable"
            )
        step.detail = f"status {status}; python {actual}; ngspice {versions[0]}"
        return
    if status == "pass":
        step.detail = (
            f"status pass; python {actual}; {len(packages)} package record(s) verified"
        )
        return
    unsupported = sorted(
        name
        for name, entry in packages.items()
        if isinstance(entry, Mapping)
        and str(entry.get("supported_status")) in {"missing", "unsupported"}
    )
    supported_python = str(python.get("supported_status")) == "supported"
    if supported_python and not unsupported:
        mismatched = sorted(
            name
            for name, entry in packages.items()
            if isinstance(entry, Mapping)
            and str(entry.get("verified_status")) in {"mismatch", "missing"}
        )
        detail = (
            "the exact tested toolchain in requirements-tested.txt was not reproduced: "
            + (f"{', '.join(mismatched)} differ from the verified pins; " if mismatched else "")
            + f"environment-check reports status {status!r} while Python {actual} and every "
            "package stay inside the declared supported range (docs/36 records this as informational)"
        )
        step.limit(detail)
        step.detail = (
            f"status {status}; python {actual} is supported but the verified pin set was not reproduced"
        )
        return
    raise StepFailure(
        f"environment-check reported status {status!r} "
        f"(python {python.get('supported_status')!r}, unsupported packages {unsupported})"
    )


def _step_capabilities(context: _Context, step: _Step) -> None:
    arguments = ["capabilities", "--compact"]
    command = _command_text(arguments)
    completed = context.cli(arguments)
    step.record(f"{command} -> exit {completed.returncode}")
    _require_exit_zero(completed, command)
    document = _require_document(_json_document(completed.stdout), command)
    listing = _capability_listing(document)
    if listing is None:
        raise StepFailure("the capability document has no non-empty backend or capability listing")
    key, entries = listing
    step.record(f"capability listing key: {key!r}")
    for entry in entries:
        name = (
            entry.get("capability_id")
            or entry.get("id")
            or entry.get("name")
            or entry.get("backend_id")
            or "?"
        )
        step.record(f"capability {name}: status={entry.get('status', 'unknown')}")
    step.detail = f"{len(entries)} capability entries listed under key {key!r}"


_AGENT_COMMANDS: tuple[tuple[tuple[str, ...], ...], ...] = (
    (("--help",),),
    (("capabilities", "--compact"),),
    (("backend", "list", "--compact"), ("backend", "list")),
    (("backend", "guide", "ngspice"), ("backend", "guide", "--name", "ngspice")),
    (("parameter-view", "--help"),),
    (("review-run", "--help"),),
    (("confirm-parameters", "--help"),),
    (("inspect-run", "--help"),),
)


def _step_agent_surface(context: _Context, step: _Step) -> None:
    failures: list[str] = []
    fallbacks = 0
    for candidates in _AGENT_COMMANDS:
        completed: subprocess.CompletedProcess | None = None
        final: Sequence[str] = candidates[-1]
        for index, arguments in enumerate(candidates):
            completed = context.cli(list(arguments))
            command = _command_text(arguments)
            step.record(f"{command} -> exit {completed.returncode}")
            if completed.returncode == 0:
                final = arguments
                break
            if index + 1 < len(candidates) and _usage_rejection(completed):
                fallbacks += 1
                step.record(
                    "argument fallback recorded: "
                    f"{_command_text(candidates[index + 1])} replaces {command} "
                    "(the CLI rejected the preferred arguments)"
                )
                continue
            final = arguments
            break
        if completed is None or completed.returncode != 0:
            failures.append(f"{_command_text(final)} exited {completed.returncode if completed else '?'}")
    if failures:
        raise StepFailure("agent command(s) failed: " + "; ".join(failures))
    step.detail = (
        f"{len(_AGENT_COMMANDS)} agent commands exited 0 "
        f"({fallbacks} argument fallback(s) recorded)"
    )


def _step_configure(context: _Context, step: _Step) -> None:
    source = context.source_root / "examples" / "buck_pi_step" / "config.json"
    if not source.is_file():
        raise StepFailure(f"the example document is missing: {source}")
    document = _require_document(_read_json(source), f"read {source}")
    document["run_id"] = PRIMARY_RUN_ID
    output = document.get("output")
    output = dict(output) if isinstance(output, Mapping) else {}
    output["directory"] = str(context.runs_root)
    document["output"] = output
    target = context.output_dir / "config.json"
    _write_json(target, document)
    parsed = _require_document(_read_json(target), f"read {target.name}")
    if parsed.get("run_id") != PRIMARY_RUN_ID:
        raise StepFailure(f"the written document carries run_id {parsed.get('run_id')!r}")
    recorded_output = parsed.get("output")
    recorded_directory = (
        str(recorded_output.get("directory")) if isinstance(recorded_output, Mapping) else ""
    )
    if recorded_directory != str(context.runs_root):
        raise StepFailure(
            f"the written document carries output.directory {recorded_directory!r}"
        )
    step.record(f"wrote {target}")
    step.record(f"run_id={PRIMARY_RUN_ID} output.directory={recorded_directory}")
    step.record(
        "the document declares no run_mode; --mode formal_comparison is never passed, "
        "so every run in this check is exploratory"
    )
    step.detail = f"config.json written and parsed (run_id {PRIMARY_RUN_ID}, output.directory runs)"


def _step_run(context: _Context, step: _Step) -> None:
    config = context.output_dir / "config.json"
    if not config.is_file():
        raise StepFailure("config.json is missing; the configure step did not write it")
    run_dir = context.runs_root / PRIMARY_RUN_ID
    _reset_path(run_dir)
    arguments = ["run", str(config)]
    command = _command_text(arguments)
    completed = context.cli(arguments)
    step.record(f"{command} -> exit {completed.returncode}")
    _require_exit_zero(completed, command)
    manifest_path = run_dir / "manifest.json"
    if not manifest_path.is_file():
        raise StepFailure(f"the run wrote no manifest: {manifest_path}")
    manifest = _require_document(_read_json(manifest_path), "manifest.json")
    status = str(manifest.get("status") or "")
    if status not in PUBLISHABLE_RUN_STATUSES:
        raise StepFailure(f"run status {status!r} is not a publishable terminal state")
    run_mode = str(manifest.get("run_mode") or "unknown")
    if run_mode == "formal_comparison":
        raise StepFailure("the run recorded formal_comparison; this check is exploratory only")
    working_tree = str(manifest.get("working_tree_status") or "unknown")
    context.run_dir = run_dir
    step.record(f"manifest {manifest_path}")
    step.record(f"manifest status={status} run_mode={run_mode} working_tree_status={working_tree}")
    step.record(f"package_sha256={manifest.get('package_sha256')}")
    step.detail = f"status {status}; working_tree_status {working_tree}; run_mode {run_mode}"


def _step_debug(context: _Context, step: _Step) -> None:
    source = context.output_dir / "config.json"
    document = _require_document(_read_json(source), "config.json")
    invalid = dict(document)
    invalid["plant_id"] = "no-such-plant"
    invalid["run_id"] = INVALID_RUN_ID
    invalid_path = context.output_dir / "invalid-config.json"
    _write_json(invalid_path, invalid)
    _reset_path(context.runs_root / INVALID_RUN_ID)
    arguments = ["run", str(invalid_path)]
    command = _command_text(arguments)
    completed = context.cli(arguments)
    step.record(f"{command} -> exit {completed.returncode}")
    if completed.returncode == 0:
        raise StepFailure("an unregistered plant_id was accepted instead of rejected")
    rejection = _json_document(completed.stdout)
    if rejection is None:
        raise StepFailure("the rejected configuration printed no JSON rejection document")
    if str(rejection.get("status")) != "CONFIG_REJECTED":
        raise StepFailure(
            f"the rejection carries status {rejection.get('status')!r}, expected CONFIG_REJECTED"
        )
    step.record(
        "rejection schema={} error_code={} status={}".format(
            rejection.get("schema"), rejection.get("error_code"), rejection.get("status")
        )
    )

    run_dir = _require_run_dir(context)
    tampered = context.output_dir / "tampered"
    _reset_path(tampered)
    shutil.copytree(run_dir, tampered)
    samples = tampered / "samples.json"
    if not samples.is_file():
        raise StepFailure(f"the published package has no samples.json to tamper with: {samples}")
    with samples.open("ab") as handle:
        handle.write(b" ")
    arguments = ["inspect-run", "--compact", str(tampered)]
    command = _command_text(arguments)
    completed = context.cli(arguments)
    step.record(f"{command} -> exit {completed.returncode}")
    inspection = _json_document(completed.stdout)
    if inspection is None:
        raise StepFailure("the tampered inspection printed no JSON document")
    tampered_status = str(inspection.get("status") or "")
    rejection_envelope = bool(inspection.get("error_code")) or tampered_status in {
        "CONFIG_REJECTED",
        "RUN_INSPECTION_REJECTED",
    }
    if completed.returncode == 0 and tampered_status == "ok" and not rejection_envelope:
        raise StepFailure("a tampered package was reported as intact")
    step.record(
        "tampered package: exit={} status={} artifact_index_verified={}".format(
            completed.returncode, tampered_status or "rejection", inspection.get("artifact_index_verified")
        )
    )
    problems = inspection.get("problems")
    if isinstance(problems, list) and problems:
        step.record(f"tampered problems: {_one_line(problems[0])}")
    step.detail = (
        "unregistered plant rejected with CONFIG_REJECTED; "
        f"tampered package reported {tampered_status or 'a rejection envelope'}"
    )


def _step_inspect(context: _Context, step: _Step) -> None:
    run_dir = _require_run_dir(context)
    checks = (
        (["inspect-run", "--compact", str(run_dir)], INSPECTION_SCHEMA, "inspect-run"),
        (["parameter-view", "--compact", str(run_dir)], PARAMETER_VIEW_SCHEMA, "parameter-view"),
        (["review-run", "--json", str(run_dir)], REVIEW_SCHEMA, "review-run"),
    )
    for arguments, expected, label in checks:
        command = _command_text(arguments)
        completed = context.cli(arguments)
        step.record(f"{command} -> exit {completed.returncode}")
        _require_exit_zero(completed, command)
        document = _require_document(_json_document(completed.stdout), label)
        key, value = _require_schema(document, expected, label)
        status = document.get("status")
        suffix = f"; status={status}" if status is not None else ""
        step.record(f"{label} schema tag: {key}={value}{suffix}")
    step.detail = "inspect-run, parameter-view, and review-run returned their schema tags"


def _step_export(context: _Context, step: _Step) -> None:
    run_dir = _require_run_dir(context)
    destination = context.output_dir / "workbench"
    _reset_path(destination)
    arguments = ["export-workbench", str(run_dir), str(destination), "--overwrite"]
    command = _command_text(arguments)
    completed = context.cli(arguments)
    step.record(f"{command} -> exit {completed.returncode}")
    _require_exit_zero(completed, command)
    for name in ("topology.json", "trace.json", "outputs.json"):
        path = destination / name
        if not path.is_file():
            raise StepFailure(f"export-workbench did not write {name}")
        document = _require_document(_read_json(path), name)
        tag = _schema_tag(document)
        step.record(
            "{}: {} ({} bytes)".format(
                name, f"{tag[0]}={tag[1]}" if tag else "no schema tag", path.stat().st_size
            )
        )
    step.detail = "topology.json, trace.json, and outputs.json exported and parsed"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _http_get(url: str, timeout: float = 2.0) -> tuple[int | None, str]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return int(response.status), response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        try:
            body = error.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001 - an unreadable error body is not a response
            body = ""
        return int(error.code), body
    except (urllib.error.URLError, OSError, ValueError):
        return None, ""


def _stop_process(process: subprocess.Popen) -> int | None:
    if process.poll() is not None:
        return process.returncode
    process.terminate()
    try:
        return process.wait(timeout=SERVICE_STOP_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        process.kill()
        try:
            return process.wait(timeout=SERVICE_STOP_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            return None


def _log_tail(path: Path, limit: int = 240) -> str:
    try:
        return _one_line(path.read_text(encoding="utf-8", errors="replace")[-800:], limit)
    except OSError:
        return "no service log"


def _step_service(context: _Context, step: _Step) -> None:
    if context.skip_service:
        step.status = "skip"
        step.detail = "skipped by --skip-service"
        return
    run_dir = _require_run_dir(context)
    if not run_dir.is_dir():
        raise StepFailure(f"the run package directory is missing: {run_dir}")
    port = _free_port()
    log_path = context.output_dir / "service.log"
    _reset_path(log_path)
    command = [
        sys.executable,
        "-m",
        "pe_sim.cli",
        "visualization-serve",
        "--runs-root",
        str(context.runs_root),
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
    ]
    step.record(f"{_command_text(command[3:])} -> started on 127.0.0.1:{port}")
    with log_path.open("w", encoding="utf-8", errors="replace") as log:
        process = subprocess.Popen(
            command,
            cwd=str(context.source_root),
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        try:
            base = f"http://127.0.0.1:{port}"
            deadline = time.monotonic() + SERVICE_START_TIMEOUT_S
            health: str | None = None
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise StepFailure(
                        "visualization-serve exited "
                        f"{process.returncode} before answering /health: {_log_tail(log_path)}"
                    )
                status, body = _http_get(base + "/health", timeout=1.0)
                if status == 200:
                    health = body
                    break
                time.sleep(SERVICE_POLL_INTERVAL_S)
            if health is None:
                raise StepFailure(
                    f"GET /health answered no 200 within {SERVICE_START_TIMEOUT_S:.0f} s: "
                    f"{_log_tail(log_path)}"
                )
            step.record(f"GET /health -> 200 {_one_line(health, 120)}")
            requests = (
                ("/v1/discovery", None),
                (f"/v1/runs/{PRIMARY_RUN_ID}/topology", None),
                (f"/v1/runs/{PRIMARY_RUN_ID}/review", REVIEW_TEXT_PREFIX),
            )
            discovery: dict[str, Any] | None = None
            for path, prefix in requests:
                status, body = _http_get(base + path)
                if status != 200:
                    raise StepFailure(f"GET {path} answered {status!r}")
                if prefix is not None and not body.startswith(prefix):
                    raise StepFailure(
                        f"GET {path} did not start with {prefix!r}: {_one_line(body, 120)!r}"
                    )
                step.record(f"GET {path} -> 200 ({len(body)} bytes)")
                if path == "/v1/discovery":
                    discovery = _json_document(body)
            schemas = discovery.get("inspection_schemas") if discovery else None
            if isinstance(schemas, Mapping):
                step.record(
                    "discovery inspection_schemas: "
                    + ", ".join(f"{key}={schemas[key]}" for key in sorted(schemas))
                )
        finally:
            exit_code = _stop_process(process)
    step.record(
        f"stopped only the process started by the check (exit code {exit_code}); "
        f"service log: {log_path.name}"
    )
    step.detail = (
        f"health, discovery, topology, and review answered on 127.0.0.1:{port}; "
        "the started process was stopped"
    )


def _git_worktree(path: Path) -> bool:
    try:
        completed = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--is-inside-work-tree"],
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0 and completed.stdout.strip().lower() == "true"


def _inside_repository(candidate: Path, repository: Path) -> bool:
    """Return whether ``candidate`` resolves inside ``repository``.

    ``pe_sim.attestation`` applies exactly this rule to the attestations
    directory (resolved path plus ``relative_to``) before it agrees to anchor a
    record, so the check asks the same question before it chooses between an
    anchored and an unanchored ``attest-run``.  Both sides are resolved first: a
    resolved path carries the on-disk spelling, which matters on Windows where
    the same drive may be written ``c:`` or ``C:``.
    """

    resolved_candidate = Path(candidate).expanduser().resolve()
    resolved_repository = Path(repository).expanduser().resolve()
    try:
        resolved_candidate.relative_to(resolved_repository)
    except ValueError:
        pass
    else:
        return True
    # A component that does not exist yet keeps the caller's spelling, so fall
    # back to the case-folded comparison the Windows file system itself makes.
    root = os.path.normcase(str(resolved_repository)).rstrip("\\/")
    target = os.path.normcase(str(resolved_candidate))
    return target == root or target.startswith(root + os.sep)


def _step_audit(context: _Context, step: _Step) -> None:
    if context.skip_audit:
        step.status = "skip"
        step.detail = "skipped by --skip-audit"
        return
    if shutil.which("git") is None:
        step.status = "skip"
        step.detail = "skipped because git is not on PATH"
        return
    if not _git_worktree(context.source_root):
        step.status = "skip"
        step.detail = f"skipped because {context.source_root} is not a Git worktree"
        return
    run_dir = _require_run_dir(context)
    attestations = context.output_dir / "attestations"
    _reset_path(attestations)
    # The producer refuses ``--repo`` together with an attestations directory
    # outside that repository, and the output directory is allowed to live
    # anywhere (docs/56).  So anchoring is chosen by containment, not assumed.
    anchored = _inside_repository(attestations, context.source_root)
    arguments: list[Any] = ["attest-run", str(run_dir)]
    if anchored:
        arguments += ["--repo", str(context.source_root)]
    else:
        arguments += ["--no-anchor"]
        step.record("anchor=disabled reason=attestations_dir_outside_repository")
        step.limit(
            "the attestations directory is outside the Git repository "
            f"({attestations} vs {context.source_root}), so attest-run ran with "
            "--no-anchor and verify-attestation with --no-git: the attestation is "
            "locally consistent but carries no anchor, and the audit step records a "
            "limitation instead of a pass"
        )
    arguments += ["--attestations-dir", str(attestations), "--compact"]
    command = _command_text(arguments)
    completed = context.cli(arguments)
    step.record(f"{command} -> exit {completed.returncode}")
    _require_exit_zero(completed, command)
    written = _require_document(_json_document(completed.stdout), "attest-run")
    located = _attestation_path(written, context.source_root)
    if located is None:
        raise StepFailure("attest-run did not report where the attestation was written")
    key, path = located
    step.record(f"attestation path key: {key!r} -> {path}")
    if not path.is_file():
        raise StepFailure(f"the reported attestation does not exist: {path}")

    arguments = ["verify-attestation", str(run_dir), "--attestation", str(path)]
    if anchored:
        arguments += ["--repo", str(context.source_root)]
    else:
        arguments += ["--no-git"]
    arguments += ["--compact"]
    command = _command_text(arguments)
    completed = context.cli(arguments)
    step.record(f"{command} -> exit {completed.returncode}")
    verification = _require_document(_json_document(completed.stdout), "verify-attestation")
    verification_status = str(verification.get("status") or "unknown")
    trust = str(verification.get("trust_level") or "unknown")
    step.record(
        f"verify-attestation schema={verification.get('schema')} "
        f"status={verification_status} trust_level={trust}"
    )
    if verification_status not in {"ok", "verified"}:
        raise StepFailure(
            f"verify-attestation reported status {verification_status!r} "
            f"(trust level {trust!r})"
        )
    if trust == "L1_anchored" and completed.returncode == 0:
        step.detail = "attestation written and verified at L1_anchored"
        return
    if trust == "L1_pending" and completed.returncode in {0, 1}:
        step.limit(
            "the detached attestation is locally consistent but unanchored (trust level "
            "L1_pending); verify-attestation exits 1 because L1 requires the attestation "
            "record in Git history, so the audit step records a limitation, not a failure"
        )
        step.detail = (
            f"attestation written and read back; trust level {trust}"
            + ("" if anchored else " (unanchored: output directory outside the repository)")
        )
        return
    raise StepFailure(
        f"verify-attestation reported trust level {trust!r} at exit {completed.returncode}"
    )


def _dirty_worktree_only(differences: Sequence[Any]) -> bool:
    if not differences:
        return False
    for difference in differences:
        if not isinstance(difference, Mapping):
            return False
        if str(difference.get("attribution")) != "environment":
            return False
        if "not clean" not in str(difference.get("reason")):
            return False
    return True


def _step_compare(context: _Context, step: _Step) -> None:
    run_dir = _require_run_dir(context)
    document = _require_document(_read_json(context.output_dir / "config.json"), "config.json")
    copy = dict(document)
    copy["run_id"] = COPY_RUN_ID
    output = document.get("output")
    copy["output"] = dict(output) if isinstance(output, Mapping) else {}
    copy_path = context.output_dir / "config-copy.json"
    _write_json(copy_path, copy)
    copy_run = context.runs_root / COPY_RUN_ID
    _reset_path(copy_run)
    arguments = ["run", str(copy_path)]
    command = _command_text(arguments)
    completed = context.cli(arguments)
    step.record(f"{command} -> exit {completed.returncode}")
    _require_exit_zero(completed, command)
    copy_manifest = _require_document(_read_json(copy_run / "manifest.json"), "manifest.json")
    copy_status = str(copy_manifest.get("status") or "")
    if copy_status not in PUBLISHABLE_RUN_STATUSES:
        raise StepFailure(f"the copy run status {copy_status!r} is not publishable")
    step.record(f"copy run {copy_run.name}: status={copy_status}")

    arguments = ["compare-runs", str(run_dir), str(copy_run)]
    command = _command_text(arguments)
    completed = context.cli(arguments)
    step.record(f"{command} -> exit {completed.returncode}")
    comparison = _require_document(_json_document(completed.stdout), "compare-runs")
    outcome = str(comparison.get("outcome") or "unknown")
    step.record(f"compare-runs outcome={outcome} schema={comparison.get('schema_version')}")
    if outcome in COMPARE_MATCH_OUTCOMES and completed.returncode == 0:
        step.detail = f"outcome {outcome}"
        return
    differences = comparison.get("differences")
    differences = differences if isinstance(differences, list) else []
    if outcome == "unknown" and _dirty_worktree_only(differences):
        reasons = "; ".join(
            str(item.get("reason")) for item in differences if isinstance(item, Mapping)
        )
        step.limit(
            "the two runs could not be certified as equal because the source worktree is "
            f"dirty ({reasons}); compare-runs reports outcome 'unknown', so no "
            "reproducibility claim is made"
        )
        step.detail = "outcome unknown: equality is blocked by the dirty source worktree"
        return
    raise StepFailure(
        f"compare-runs reported outcome {outcome!r} at exit {completed.returncode}"
    )


_STEP_HANDLERS = {
    "environment": _step_environment,
    "capabilities": _step_capabilities,
    "agent-surface": _step_agent_surface,
    "configure": _step_configure,
    "run": _step_run,
    "debug": _step_debug,
    "inspect": _step_inspect,
    "export": _step_export,
    "service": _step_service,
    "audit": _step_audit,
    "compare": _step_compare,
}


# -- envelope --------------------------------------------------------------


def _standard_limitations() -> list[str]:
    return [
        "this check is a smoke check of the documented path, not a release gate: "
        "passing it approves no release, package, or commit",
        "no formal comparison and no physical validation is performed: every run is "
        "exploratory, and no equality, numerical, safety, or hardware claim is made",
        "ngspice is not exercised unless --ngspice is supplied; the built-in averaged "
        "Buck reference is a project-owned model, not a hardware result",
        "attestation trust depends on the Git state of the source root: a dirty tree or "
        "an uncommitted attestation record yields L1_pending instead of L1_anchored",
    ]


def _outcome(steps: Sequence[_Step]) -> str:
    if any(step.required and step.status != "pass" for step in steps):
        return "fail"
    if any(
        (not step.required and step.status != "pass") or step.limitations for step in steps
    ):
        return "partial"
    return "pass"


def _assemble(
    started: str,
    environment: Mapping[str, Any],
    output_dir: Path,
    steps: Sequence[_Step],
    run_dir: Path | None,
) -> dict[str, Any]:
    limitations = _standard_limitations()
    for step in steps:
        for item in step.limitations:
            if item not in limitations:
                limitations.append(item)
    return {
        "schema_version": FULL_FLOW_SCHEMA,
        "rule_version": FULL_FLOW_RULE_VERSION,
        "outcome": _outcome(steps),
        "started_at_utc": started,
        "environment": dict(environment),
        "workspace": {
            "output_dir": str(output_dir),
            "run_dir": str(run_dir) if run_dir is not None else None,
        },
        "steps": [step.to_dict() for step in steps],
        "limitations": limitations,
    }


def _terminal_record(
    started: str, environment: Mapping[str, Any], output_dir: Path, reason: str
) -> dict[str, Any]:
    steps = [
        _Step(
            step_id,
            STEP_TITLES[step_id],
            step_id in REQUIRED_STEPS,
            status="fail",
            detail=reason,
        )
        for step_id in STEP_ORDER
    ]
    return _assemble(started, environment, output_dir, steps, None)


def _render_text(record: Mapping[str, Any]) -> str:
    lines = [
        f"full flow check: {record['outcome']} ({record['rule_version']})",
        f"output: {record['workspace']['output_dir']}",
    ]
    for step in record["steps"]:
        detail = _one_line(step.get("detail", ""))
        lines.append(f"[{step['status']}] {step['id']} - {step['title']}: {detail}")
    lines.append("limitations:")
    for item in record["limitations"]:
        lines.append(f"  - {_one_line(item)}")
    return "\n".join(lines)


def _print_summary(record: Mapping[str, Any]) -> None:
    # ASCII-only so that a Windows console with a legacy code page cannot fail.
    print(_render_text(record).encode("ascii", "replace").decode("ascii"), flush=True)


def run_flow(
    *,
    output_dir: str | Path | None = None,
    source_root: str | Path | None = None,
    ngspice: str | Path | None = None,
    skip_service: bool = False,
    skip_audit: bool = False,
    json_output: bool = False,
) -> dict[str, Any]:
    """Walk the documented flow once and return the ``pe-sim.full-flow-check.v1`` record.

    Step failures are recorded, never raised.  When ``json_output`` is false a
    short hierarchical text summary is printed; the caller prints the envelope.
    """

    started = _utc_timestamp()
    root = (
        Path(source_root).expanduser().resolve()
        if source_root is not None
        else _default_source_root()
    )
    target = (
        Path(output_dir).expanduser()
        if output_dir is not None
        else _default_output_dir(root)
    )
    target = target.resolve()
    environment = {
        "python": _python_version(),
        "executable": sys.executable,
        "source_root": str(root),
    }
    if not root.is_dir():
        return _terminal_record(
            started, environment, target, f"source root is not a directory: {root}"
        )
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return _terminal_record(
            started,
            environment,
            target,
            f"the output directory could not be created: {type(exc).__name__}: {exc}",
        )

    context = _Context(
        source_root=root,
        output_dir=target,
        ngspice=str(ngspice) if ngspice is not None else None,
        skip_service=bool(skip_service),
        skip_audit=bool(skip_audit),
    )
    steps = [
        _Step(step_id, STEP_TITLES[step_id], step_id in REQUIRED_STEPS)
        for step_id in STEP_ORDER
    ]
    stopped: str | None = None
    for step in steps:
        if stopped is not None:
            step.status = "fail"
            step.detail = f"not attempted: the required step {stopped!r} failed"
            continue
        try:
            _STEP_HANDLERS[step.id](context, step)
        except StepFailure as exc:
            step.status = "fail"
            step.detail = str(exc)
        except Exception as exc:  # noqa: BLE001 - a step failure must never abort the check
            step.status = "fail"
            message = str(exc)
            step.detail = f"{type(exc).__name__}: {message}" if message else type(exc).__name__
            step.record("traceback tail: " + _one_line(traceback.format_exc(limit=3), 400))
        if step.required and step.status != "pass":
            stopped = step.id

    record = _assemble(started, environment, target, steps, context.run_dir)
    record_path = target / "full-flow-check.json"
    try:
        _write_json(record_path, record)
    except OSError as exc:
        record["limitations"].append(
            f"the record could not be written to {record_path}: {type(exc).__name__}: {exc}"
        )
    if not json_output:
        _print_summary(record)
    return record


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="check the documented pe-sim flow end to end (docs/56)"
    )
    parser.add_argument(
        "--output",
        default=None,
        help="directory for every artifact written by the check "
        "(default .tmp/full-flow/<UTC timestamp>)",
    )
    parser.add_argument(
        "--source-root",
        default=None,
        help="checkout that provides pe_sim, the examples, and the pins",
    )
    parser.add_argument(
        "--ngspice",
        default=None,
        help="ngspice executable to record in the environment step",
    )
    parser.add_argument(
        "--skip-service",
        action="store_true",
        help="record the loopback service step as skipped",
    )
    parser.add_argument(
        "--skip-audit",
        action="store_true",
        help="record the attestation step as skipped",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="print the record envelope as JSON",
    )
    return parser.parse_args(list(argv) if argv is not None else None)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the check and return 0 pass, 2 partial, 1 fail."""

    arguments = _parse_args(argv)
    record = run_flow(
        output_dir=arguments.output,
        source_root=arguments.source_root,
        ngspice=arguments.ngspice,
        skip_service=arguments.skip_service,
        skip_audit=arguments.skip_audit,
        json_output=arguments.json_output,
    )
    if arguments.json_output:
        print(json.dumps(record, indent=2, ensure_ascii=True), flush=True)
    return {"pass": 0, "partial": 2, "fail": 1}.get(record["outcome"], 1)


if __name__ == "__main__":
    sys.exit(main())

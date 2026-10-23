"""Bounded subprocess execution of the reviewed ngspice executable.

This module is the only place that starts an external simulator.  It keeps a
strict process boundary so the MIT-licensed project does not link against a
GPL simulator library, and it records enough evidence (command, log, hashes,
exit code, duration) to diagnose and audit a failed window solve.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import math
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
from typing import Any, Mapping

from ..contracts import (
    BackendProcessError,
    BackendTimeoutError,
    NumericalNonconvergenceError,
    SimulationExecutionError,
)
from ..numerical_health import NumericalHealth, parse_log
from .discovery import BackendNotFoundError, resolve_ngspice_executable
from .settings import NgspiceSolverSettings
from .wrdata import TraceTable, WrdataParseError, parse_wrdata


_VERSION_RE = re.compile(
    r"(?:ngspice|version)\s*[-_:]?\s*v?([0-9]+(?:\.[0-9]+)*(?:[-+][0-9A-Za-z.-]+)?)",
    re.IGNORECASE,
)
_FALLBACK_VERSION_RE = re.compile(r"\bv?([0-9]+(?:\.[0-9]+){1,2}(?:[-+][0-9A-Za-z.-]+)?)\b")

# Tokens that mean the solver did not produce a trustworthy trajectory.  They
# are checked case-insensitively.  "singular matrix" is intentionally treated
# as a warning: ngspice can recover by reducing the timestep and still produce
# a complete window.
_FATAL_SOLVER_TOKENS = (
    "no convergence",
    "doanalyses: ... no convergence",
    "doanalyses: no convergence",
    "timestep too small",
    "time step too small",
    "convergence failed",
    "gmin stepping failed",
    "source stepping failed",
    "iteration limit reached",
)

_PROCESS_FAILURE_TOKENS = (
    "unknown subckt",
    "can't find",
    "cannot find",
    "cannot open",
    "no such file",
    "fatal error",
    "error: cannot",
    "unable to open",
)


def parse_ngspice_version(output: str) -> str | None:
    """Extract a conservative ngspice version token from command output."""

    if not isinstance(output, str):
        return None
    match = _VERSION_RE.search(output)
    if match:
        return match.group(1)
    match = _FALLBACK_VERSION_RE.search(output)
    return match.group(1) if match else None


def executable_digest(path: str | Path) -> tuple[str, int] | None:
    """Return ``(sha256, size_bytes)`` for a real executable file."""

    candidate = Path(path)
    if not candidate.is_file():
        return None
    digest = hashlib.sha256()
    size = 0
    with candidate.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
            size += len(block)
    return digest.hexdigest(), size


@dataclass(frozen=True)
class NgspiceRunResult:
    """Evidence and parsed data from one successful window solve."""

    returncode: int
    stdout: str
    stderr: str
    log: str
    table: TraceTable
    netlist_sha256: str
    output_sha256: str | None
    log_sha256: str
    duration_s: float
    command: tuple[str, ...]
    timed_out: bool = False
    numerical_health: NumericalHealth | None = None

    @property
    def time_s(self) -> tuple[float, ...]:
        return self.table.time_s

    @property
    def signals(self) -> Mapping[str, tuple[float, ...]]:
        return self.table.signals


_MAX_LOG_EXCERPT = 8192


def _log_evidence(log_text: str, workdir: Path | None = None) -> dict[str, Any]:
    """Return a bounded, portable, JSON-safe summary of one solver log.

    The digest covers the original text.  The excerpt is a convenience for
    diagnosis and replaces the temporary work directory so a Manifest never
    publishes a machine-local absolute path.
    """

    encoded = log_text.encode("utf-8", errors="replace")
    excerpt = log_text[-_MAX_LOG_EXCERPT:]
    if workdir is not None:
        for candidate in (str(workdir), workdir.as_posix()):
            excerpt = excerpt.replace(candidate, "<workdir>")
    return {
        "log_sha256": hashlib.sha256(encoded).hexdigest(),
        "log_excerpt": excerpt,
        "log_truncated": len(log_text) > _MAX_LOG_EXCERPT,
    }


def _classify_failure(log: str, stdout: str, stderr: str, returncode: int) -> None:
    """Raise a precise execution error for an unusable ngspice result."""

    combined = "\n".join(part for part in (log, stdout, stderr) if part)
    lowered = combined.lower()
    if any(token in lowered for token in _FATAL_SOLVER_TOKENS):
        raise NumericalNonconvergenceError(
            "ngspice reported a solver convergence failure"
        )
    if returncode != 0:
        raise BackendProcessError(f"ngspice exited with code {returncode}")
    if any(token in lowered for token in _PROCESS_FAILURE_TOKENS):
        raise BackendProcessError("ngspice reported a fatal input or file error")


class NgspiceProcess:
    """Run one generated netlist and return validated waveform data."""

    def __init__(
        self,
        executable: str | Path,
        *,
        solver: NgspiceSolverSettings | None = None,
        timeout_s: float = 30.0,
        keep_artifacts: bool = False,
        work_root: str | Path | None = None,
    ) -> None:
        if not str(executable).strip():
            raise ValueError("ngspice executable must be a non-empty path or command name")
        timeout = float(timeout_s)
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout_s must be finite and positive")
        self.executable = str(executable)
        self.solver = solver or NgspiceSolverSettings()
        self.timeout_s = timeout
        self.keep_artifacts = bool(keep_artifacts)
        self.work_root = Path(work_root) if work_root is not None else None

    def resolve_executable(self) -> str:
        """Resolve a command name or path through the documented discovery order."""

        try:
            return resolve_ngspice_executable(self.executable).executable
        except BackendNotFoundError as exc:
            raise BackendProcessError(str(exc)) from exc

    def run(self, netlist: str, *, label: str = "window") -> NgspiceRunResult:
        """Execute a netlist in an isolated directory.

        The temporary directory is removed after parsing unless
        ``keep_artifacts`` is set, so a long closed-loop run does not leave
        thousands of netlists behind.  When ``keep_artifacts`` is enabled the
        directory is left on disk for local diagnosis.
        """

        if not isinstance(netlist, str) or not netlist.strip():
            raise ValueError("netlist text must be a non-empty string")
        netlist_bytes = netlist.encode("utf-8")
        netlist_sha256 = hashlib.sha256(netlist_bytes).hexdigest()
        evidence: dict[str, Any] = {"netlist_sha256": netlist_sha256}
        try:
            executable = self.resolve_executable()
        except SimulationExecutionError as exc:
            exc.evidence = dict(evidence)
            raise

        safe_label = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(label)) or "window"
        parent = self.work_root
        if parent is not None:
            parent.mkdir(parents=True, exist_ok=True)
        workdir = Path(tempfile.mkdtemp(prefix=f"pe-sim-ngspice-{safe_label}-", dir=str(parent) if parent else None))
        log_path = workdir / "ngspice.log"
        try:
            netlist_path = workdir / "netlist.cir"
            netlist_path.write_text(netlist, encoding="utf-8", newline="\n")
            output_path = workdir / "output.dat"
            command = [executable, "-b", "-o", str(log_path), str(netlist_path)]
            evidence["executable_name"] = Path(executable).name
            evidence["arguments"] = ["-b", "-o", "<log>", "<netlist>"]

            started = time.perf_counter()
            try:
                completed = subprocess.run(
                    command,
                    cwd=str(workdir),
                    check=False,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=self.timeout_s,
                )
            except subprocess.TimeoutExpired as exc:
                raise BackendTimeoutError(
                    f"ngspice window solve exceeded {self.timeout_s:g}s"
                ) from exc
            except (FileNotFoundError, OSError, ValueError) as exc:
                raise BackendProcessError(
                    f"ngspice process could not be started: {type(exc).__name__}: {exc}"
                ) from exc
            duration_s = time.perf_counter() - started

            log_text = log_path.read_text(encoding="utf-8", errors="replace") if log_path.is_file() else ""
            evidence.update(_log_evidence(log_text, workdir))
            stdout = completed.stdout or ""
            stderr = completed.stderr or ""

            if not output_path.is_file():
                _classify_failure(log_text, stdout, stderr, completed.returncode)
                raise BackendProcessError("ngspice produced no wrdata output")

            output_bytes = output_path.read_bytes()
            try:
                table = parse_wrdata(output_bytes.decode("utf-8", errors="replace"))
            except WrdataParseError as exc:
                _classify_failure(log_text, stdout, stderr, completed.returncode)
                raise BackendProcessError(f"ngspice output could not be parsed: {exc}") from exc

            _classify_failure(log_text, stdout, stderr, completed.returncode)
            output_sha256 = hashlib.sha256(output_bytes).hexdigest()
            numerical_health = parse_log(log_text, workdir=str(workdir))
            evidence.update(
                {
                    "returncode": completed.returncode,
                    "output_sha256": output_sha256,
                    "duration_s": duration_s,
                    "numerical_health": numerical_health.to_dict(),
                }
            )

            return NgspiceRunResult(
                returncode=completed.returncode,
                stdout=stdout,
                stderr=stderr,
                log=log_text,
                table=table,
                netlist_sha256=netlist_sha256,
                output_sha256=output_sha256,
                log_sha256=hashlib.sha256(log_text.encode("utf-8", errors="replace")).hexdigest(),
                duration_s=duration_s,
                command=tuple(command),
                numerical_health=numerical_health,
            )
        except SimulationExecutionError as exc:
            if "log_excerpt" not in evidence and log_path.is_file():
                evidence.update(_log_evidence(log_path.read_text(encoding="utf-8", errors="replace"), workdir))
            exc.evidence = dict(evidence)
            raise
        finally:
            if not self.keep_artifacts:
                shutil.rmtree(workdir, ignore_errors=True)


@dataclass(frozen=True)
class NgspiceExecutableAdapter:
    """Record the identity of the external ngspice executable.

    The adapter is explicit: the generic infrastructure never guesses a
    simulator version.  The local executable path is intentionally omitted
    from the returned evidence so a Manifest remains portable across machines;
    the digest and size identify the binary itself.
    """

    executable: str | Path
    solver: NgspiceSolverSettings = field(default_factory=NgspiceSolverSettings)
    name: str = "ngspice"
    version_args: tuple[str, ...] = ("--version",)
    timeout_s: float = 5.0
    minimum_major: int = 40

    def backend_provenance(self) -> Mapping[str, Any]:
        executable = str(self.executable)
        base: dict[str, Any] = {
            "name": self.name,
            "executable_name": Path(executable).name or executable,
            "solver_settings": self.solver.to_dict(),
        }
        try:
            resolution = resolve_ngspice_executable(self.executable)
        except (BackendNotFoundError, ValueError) as exc:
            return {
                **base,
                "status": "unavailable",
                "version": None,
                "executable_sha256": None,
                "limitations": [f"ngspice executable could not be resolved: {exc}"],
            }
        executable = resolution.executable
        base["executable_name"] = resolution.executable_name
        try:
            completed = subprocess.run(
                [executable, *self.version_args],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=float(self.timeout_s),
            )
        except (FileNotFoundError, OSError, subprocess.SubprocessError, ValueError) as exc:
            return {
                **base,
                "status": "unavailable",
                "version": None,
                "executable_sha256": None,
                "limitations": [
                    *resolution.notes,
                    f"ngspice executable could not be queried: {type(exc).__name__}: {exc}",
                ],
            }

        output = (completed.stdout or "") + ("\n" + completed.stderr if completed.stderr else "")
        version = parse_ngspice_version(output)
        limitations: list[str] = list(resolution.notes)
        status = "known"
        if completed.returncode != 0:
            status = "partial"
            limitations.append(f"ngspice version command returned exit code {completed.returncode}")
        if version is None:
            status = "partial"
            limitations.append("ngspice version output could not be parsed")
        elif int(version.split(".")[0]) < self.minimum_major:
            status = "partial"
            limitations.append(
                f"ngspice version {version} is below the verified minimum major {self.minimum_major}"
            )

        digest = None
        size = None
        try:
            resolved = Path(executable)
            if not resolved.is_file():
                candidate = shutil.which(executable)
                resolved = Path(candidate) if candidate else resolved
            result = executable_digest(resolved)
        except OSError as exc:
            limitations.append(f"ngspice executable could not be hashed: {exc}")
            status = "partial"
        else:
            if result is None:
                limitations.append("ngspice executable file could not be resolved for hashing")
                status = "partial"
            else:
                digest, size = result

        return {
            **base,
            "status": status,
            "version": version,
            "executable_sha256": digest,
            "executable_size_bytes": size,
            "returncode": completed.returncode,
            "version_output_sha256": hashlib.sha256(
                output.encode("utf-8", errors="replace")
            ).hexdigest(),
            "limitations": limitations,
        }


__all__ = [
    "NgspiceExecutableAdapter",
    "NgspiceProcess",
    "NgspiceRunResult",
    "executable_digest",
    "parse_ngspice_version",
]

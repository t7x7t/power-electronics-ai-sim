#!/usr/bin/env python3
"""End-user acceptance for a fresh GitHub checkout.

The harness intentionally uses only public entry points:

1. export the working tree into a clean Git snapshot (a clone-like checkout);
2. create a fresh virtual environment and install the project;
3. exercise the two onboarding scenarios from ``docs/35``;
4. run a switching-level Buck experiment through ngspice (when available);
5. verify run-package integrity, determinism, and the three exported contracts.

It never downloads or installs a simulator.  When no simulator is supplied or
found, the ngspice-specific phases are recorded as ``skip`` and the overall
status is ``partial`` so the result cannot be mistaken for a full pass.

Example (Windows, existing ngspice installation)::

    python scripts/user_acceptance.py --ngspice "D:\\tools\\ngspice_con.exe"

The JSON report is written inside the acceptance workspace.  Machine-local
absolute paths are not copied into run evidence by the project; this report
records the executable name, version, and SHA-256 instead of its path.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

SCHEMA_VERSION = "pe-sim-user-acceptance-v1"
_SKIP_DIRECTORIES = (
    ".git/",
    ".venv/",
    "venv/",
    "runs/",
    ".tmp/",
    "node_modules/",
    "dist/",
    "build/",
    ".pytest_cache/",
    "__pycache__/",
)
_FIXTURE_FILES = ("topology.json", "trace.json", "outputs.json")
_REQUIRED_RUN_FILES = (
    "manifest.json",
    "config.snapshot.json",
    "environment.json",
    "qualification.json",
    "safety.json",
    "metrics.json",
    "events.json",
    "samples.json",
    "audit.json",
    "state_transitions.json",
)


class AcceptanceError(RuntimeError):
    """A required acceptance check failed."""


@dataclass
class Step:
    name: str
    status: str = "pass"
    detail: str = ""
    duration_s: float = 0.0
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "detail": self.detail,
            "duration_s": round(self.duration_s, 3),
            "evidence": self.evidence,
        }


@dataclass
class Acceptance:
    repo: Path
    workdir: Path
    checkout: Path
    python: str
    install_mode: str
    ngspice: Path | None
    pyspice: bool = True
    keep: bool = True
    steps: list[Step] = field(default_factory=list)
    snapshot_commit: str | None = None
    source_head: str | None = None
    source_branch: str | None = None
    limitations: list[str] = field(default_factory=list)

    # -- execution helpers -------------------------------------------------

    @property
    def venv_dir(self) -> Path:
        return self.checkout / ".venv"

    @property
    def venv_python(self) -> Path:
        if os.name == "nt":
            return self.venv_dir / "Scripts" / "python.exe"
        return self.venv_dir / "bin" / "python"

    @property
    def venv_pe_sim(self) -> Path:
        if os.name == "nt":
            return self.venv_dir / "Scripts" / "pe-sim.exe"
        return self.venv_dir / "bin" / "pe-sim"

    def step(self, name: str, fn: Callable[[], dict[str, Any] | None], *, critical: bool = True) -> None:
        started = time.perf_counter()
        print(f"[acceptance] {name} ...", flush=True)
        try:
            evidence = fn() or {}
        except Exception as exc:  # noqa: BLE001 - acceptance reports every failure
            record = Step(
                name=name,
                status="fail",
                detail=f"{type(exc).__name__}: {exc}",
                duration_s=time.perf_counter() - started,
                evidence={"traceback": traceback.format_exc(limit=4)},
            )
            self.steps.append(record)
            print(f"[acceptance] {name}: FAIL - {record.detail}", flush=True)
            if critical:
                raise AcceptanceError(record.detail) from exc
            return
        record = Step(
            name=name,
            status="pass",
            duration_s=time.perf_counter() - started,
            evidence=evidence,
        )
        self.steps.append(record)
        detail = evidence.get("summary") if isinstance(evidence, dict) else None
        suffix = f" - {detail}" if detail else ""
        print(f"[acceptance] {name}: pass ({record.duration_s:.1f}s){suffix}", flush=True)

    def skip(self, name: str, reason: str) -> None:
        self.steps.append(Step(name=name, status="skip", detail=reason))
        print(f"[acceptance] {name}: SKIP - {reason}", flush=True)

    # -- process helpers ---------------------------------------------------

    def base_env(self) -> dict[str, str]:
        env = dict(os.environ)
        for name in ("PYTHONPATH", "PYTHONHOME", "PE_SIM_NGSPICE", "PE_SIM_CONFIG_HOME"):
            env.pop(name, None)
        env["PYTHONIOENCODING"] = "utf-8"
        return env

    def sanitized_env(self, config_home: Path) -> dict[str, str]:
        """Return an environment that cannot discover a simulator accidentally."""

        env = self.base_env()
        empty_path = self.workdir / "empty-path"
        empty_path.mkdir(parents=True, exist_ok=True)
        env["PATH"] = str(empty_path)
        env["PE_SIM_CONFIG_HOME"] = str(config_home)
        if os.name == "nt":
            for name in ("SystemRoot", "windir", "ComSpec", "PATHEXT", "TEMP", "TMP"):
                if name in os.environ:
                    env[name] = os.environ[name]
        return env

    def run(
        self,
        args: Sequence[str | Path],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        timeout: float = 300.0,
        expect: Iterable[int] = (0,),
    ) -> subprocess.CompletedProcess[str]:
        command = [str(item) for item in args]
        completed = subprocess.run(
            command,
            cwd=str(cwd or self.checkout),
            env=dict(env) if env is not None else self.base_env(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
        allowed = tuple(expect)
        if completed.returncode not in allowed:
            raise AcceptanceError(
                f"command failed with exit {completed.returncode}: {' '.join(command)}\n"
                f"stdout: {completed.stdout[-2000:]}\nstderr: {completed.stderr[-2000:]}"
            )
        return completed

    def pe_sim(
        self,
        args: Sequence[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        timeout: float = 300.0,
        expect: Iterable[int] = (0,),
    ) -> tuple[subprocess.CompletedProcess[str], dict[str, Any] | None]:
        completed = self.run(
            [self.venv_pe_sim, *args],
            cwd=cwd,
            env=env,
            timeout=timeout,
            expect=expect,
        )
        payload: dict[str, Any] | None = None
        text = completed.stdout.strip()
        if text.startswith("{"):
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                payload = None
        return completed, payload

    # -- phases ------------------------------------------------------------

    def phase_source_identity(self) -> dict[str, Any]:
        def git(*args: str) -> str:
            completed = subprocess.run(
                ["git", "-C", str(self.repo), *args],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            if completed.returncode != 0:
                raise AcceptanceError(completed.stderr.strip() or f"git {' '.join(args)} failed")
            return completed.stdout.strip()

        self.source_head = git("rev-parse", "HEAD")
        self.source_branch = git("rev-parse", "--abbrev-ref", "HEAD")
        status = git("status", "--porcelain")
        return {
            "head_commit": self.source_head,
            "branch": self.source_branch,
            "dirty_entries": len([line for line in status.splitlines() if line.strip()]),
            "basis": "working-tree export (uncommitted changes are included)",
        }

    def phase_snapshot_checkout(self) -> dict[str, Any]:
        self.checkout.mkdir(parents=True, exist_ok=True)
        listing = subprocess.run(
            [
                "git",
                "-C",
                str(self.repo),
                "ls-files",
                "-z",
                "--cached",
                "--others",
                "--exclude-standard",
            ],
            capture_output=True,
            check=False,
        )
        if listing.returncode != 0:
            raise AcceptanceError(listing.stderr.decode("utf-8", "replace"))
        raw_paths = [item for item in listing.stdout.split(b"\0") if item]
        copied = 0
        skipped: list[str] = []
        for raw in raw_paths:
            relative = os.fsdecode(raw).replace("\\", "/")
            if any(relative.startswith(prefix) or f"/{prefix}" in f"/{relative}" for prefix in _SKIP_DIRECTORIES):
                skipped.append(relative)
                continue
            source = self.repo / Path(*relative.split("/"))
            if not source.is_file():
                skipped.append(relative)
                continue
            destination = self.checkout / Path(*relative.split("/"))
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            copied += 1

        self.run(["git", "-c", "init.defaultBranch=main", "init", "-q"], cwd=self.checkout)
        self.run(["git", "add", "-A"], cwd=self.checkout)
        self.run(
            [
                "git",
                "-c",
                "user.name=pe-sim acceptance",
                "-c",
                "user.email=acceptance@example.invalid",
                "commit",
                "-q",
                "-m",
                "user acceptance snapshot",
            ],
            cwd=self.checkout,
        )
        status = self.run(["git", "status", "--porcelain"], cwd=self.checkout).stdout.strip()
        if status:
            raise AcceptanceError(f"snapshot checkout is not clean:\n{status}")
        self.snapshot_commit = self.run(["git", "rev-parse", "HEAD"], cwd=self.checkout).stdout.strip()
        return {
            "files": copied,
            "skipped": len(skipped),
            "snapshot_commit": self.snapshot_commit,
            "clean_worktree": True,
            "summary": f"{copied} files, commit {self.snapshot_commit[:12]}",
        }

    def phase_fresh_venv(self) -> dict[str, Any]:
        self.run([self.python, "-m", "venv", str(self.venv_dir)], cwd=self.checkout, timeout=300)
        completed = self.run(
            [self.venv_python, "-c", "import sys; print(sys.version); print(sys.executable)"],
            cwd=self.checkout,
        )
        lines = completed.stdout.strip().splitlines()
        return {
            "python_version": lines[0] if lines else "unknown",
            "isolated": "site-packages" not in completed.stdout,
            "summary": lines[0] if lines else "created",
        }

    def phase_install(self) -> dict[str, Any]:
        args = [self.venv_python, "-m", "pip", "install", "--disable-pip-version-check", "-q"]
        if self.install_mode == "editable":
            args.append("-e")
        args.append(".")
        completed = self.run(args, cwd=self.checkout, timeout=900)
        pyspice_requirements: str | None = None
        if self.pyspice:
            version_text = self.run(
                [
                    self.venv_python,
                    "-c",
                    "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')",
                ],
                cwd=self.checkout,
            ).stdout.strip()
            try:
                major, minor = (int(item) for item in version_text.split(".")[:2])
            except ValueError as exc:
                raise AcceptanceError(f"could not determine the fresh venv Python version: {version_text}") from exc
            if (major, minor) < (3, 13):
                requirements = self.checkout / "requirements-pyspice-tested.txt"
                if not requirements.is_file():
                    raise AcceptanceError("requirements-pyspice-tested.txt is missing from the checkout")
                pyspice_requirements = str(requirements)
                install_args = [
                    self.venv_python,
                    "-m",
                    "pip",
                    "install",
                    "--disable-pip-version-check",
                    "-q",
                    "-r",
                    str(requirements),
                ]
            else:
                pyspice_requirements = "resolver-selected (Python 3.13+)"
                install_args = [
                    self.venv_python,
                    "-m",
                    "pip",
                    "install",
                    "--disable-pip-version-check",
                    "-q",
                    "PySpice==1.5",
                ]
            self.run(install_args, cwd=self.checkout, timeout=900)
        names = ["power-electronics-ai-sim", "jsonschema"]
        if self.pyspice:
            names.append("PySpice")
        probe = self.run(
            [
                self.venv_python,
                "-c",
                (
                    "import importlib.metadata as m, json; "
                    f"print(json.dumps({{name: m.version(name) for name in {names!r}}}))"
                ),
            ],
            cwd=self.checkout,
        )
        try:
            versions = json.loads(probe.stdout.strip().splitlines()[-1])
        except (json.JSONDecodeError, IndexError) as exc:
            raise AcceptanceError(f"could not inspect the fresh installation: {probe.stdout[-1000:]}") from exc
        missing = [name for name in names if not versions.get(name)]
        if missing:
            raise AcceptanceError(f"fresh install is missing required packages: {missing}")
        dependency_check = self.run(
            [self.venv_python, "-m", "pip", "check"],
            cwd=self.checkout,
            expect=(0, 1),
        )
        if dependency_check.returncode != 0:
            raise AcceptanceError(f"pip check is not clean: {dependency_check.stdout[-1000:]}")
        summary = f"{self.install_mode} install, jsonschema {versions['jsonschema']}"
        if versions.get("PySpice"):
            summary += f", PySpice {versions['PySpice']} (reference construction path)"
        summary += ", pip check clean"
        return {
            "mode": self.install_mode,
            "pip_stdout_tail": completed.stdout[-500:],
            "pyspice_requirements": pyspice_requirements,
            "pip_check": "clean",
            "packages": versions,
            "summary": summary,
        }

    def phase_registry(self) -> dict[str, Any]:
        _, payload = self.pe_sim(["backend", "list"])
        assert payload is not None, "backend list did not return JSON"
        entries = {item["name"]: item for item in payload.get("backends", [])}
        expected = {
            "ngspice": "supported",
            "xyce": "planned",
            "ltspice": "planned",
            "pyspice": "optional-library",
        }
        mismatched = {name: entries.get(name, {}).get("status") for name, status in expected.items() if entries.get(name, {}).get("status") != status}
        if mismatched:
            raise AcceptanceError(f"backend registry entries differ: {mismatched}")
        return {
            "backends": {name: entries[name]["status"] for name in expected},
            "summary": "4 registered backends",
        }

    def phase_no_simulator(self) -> dict[str, Any]:
        config_home = self.workdir / "config-scenario-a"
        config_home.mkdir(parents=True, exist_ok=True)
        env = self.sanitized_env(config_home)
        completed, payload = self.pe_sim(
            ["backend", "setup", "--name", "ngspice"],
            env=env,
            expect=(0, 1),
            timeout=120,
        )
        if completed.returncode != 1 or payload is None or payload.get("status") != "NOT_FOUND":
            raise AcceptanceError(f"scenario A did not report NOT_FOUND: {completed.stdout[-1000:]}")
        recipes = payload.get("install_recipes") or []
        if not recipes or not payload.get("next_steps"):
            raise AcceptanceError("scenario A did not provide installation recipes or next steps")
        config_path = config_home / "backends.toml"
        if config_path.exists():
            raise AcceptanceError("scenario A unexpectedly wrote a configuration file")

        scratch = self.workdir / "scenario-a-run"
        scratch.mkdir(parents=True, exist_ok=True)
        completed, rejected = self.pe_sim(
            [
                "run",
                str(self.checkout / "examples" / "ngspice_buck_step" / "config.json"),
                "--backend",
                "ngspice-buck",
            ],
            cwd=scratch,
            env=env,
            expect=(2,),
            timeout=120,
        )
        if rejected is None or rejected.get("status") != "CONFIG_REJECTED":
            raise AcceptanceError(f"missing simulator was not rejected explicitly: {completed.stdout[-1000:]}")
        if "not found" not in str(rejected.get("error", "")).lower():
            raise AcceptanceError(f"rejection did not explain the missing simulator: {rejected}")
        if (scratch / "runs").exists():
            raise AcceptanceError("a failed preflight must not create a run directory")
        return {
            "status": payload["status"],
            "recipes": [recipe.get("name") for recipe in recipes],
            "config_written": False,
            "run_rejected": rejected.get("status"),
            "summary": "NOT_FOUND with recipes; run rejected without writing artifacts",
        }

    def phase_installed_simulator(self) -> dict[str, Any]:
        assert self.ngspice is not None
        config_home = self.workdir / "config-scenario-b"
        config_home.mkdir(parents=True, exist_ok=True)
        env = self.base_env()
        env["PE_SIM_CONFIG_HOME"] = str(config_home)
        env.pop("PE_SIM_NGSPICE", None)

        _, setup = self.pe_sim(
            [
                "backend",
                "setup",
                "--name",
                "ngspice",
                "--executable",
                str(self.ngspice),
                "--write-config",
            ],
            env=env,
            timeout=120,
        )
        if setup is None or setup.get("status") != "known":
            raise AcceptanceError(f"backend setup did not verify the executable: {setup}")
        written = Path(str(setup.get("config_written", "")))
        if not written.is_file():
            raise AcceptanceError("backend setup did not write the user configuration")
        try:
            written.resolve().relative_to(self.checkout.resolve())
        except ValueError:
            pass
        else:
            raise AcceptanceError("user configuration was written inside the checkout")

        _, doctor = self.pe_sim(["backend", "doctor", "--name", "ngspice"], env=env, timeout=120)
        if doctor is None or doctor.get("status") != "known":
            raise AcceptanceError(f"backend doctor did not report a known backend: {doctor}")
        provenance = doctor.get("provenance") or {}
        version = str(provenance.get("version", ""))
        digest = str(provenance.get("executable_sha256", ""))
        if not version or len(digest) != 64:
            raise AcceptanceError("backend provenance lacks version or executable digest")

        return {
            "status": doctor["status"],
            "version": version,
            "executable_name": provenance.get("executable_name"),
            "executable_sha256": digest,
            "solver_settings": provenance.get("solver_settings"),
            "config_outside_checkout": True,
            "summary": f"ngspice {version}, sha256 {digest[:12]}",
        }

    def phase_gui_preference(self) -> dict[str, Any] | None:
        assert self.ngspice is not None
        gui = self.ngspice.with_name("ngspice.exe")
        if os.name != "nt" or not gui.is_file() or gui.name.lower() == self.ngspice.name.lower():
            return None
        config_home = self.workdir / "config-gui"
        config_home.mkdir(parents=True, exist_ok=True)
        env = self.base_env()
        env["PE_SIM_CONFIG_HOME"] = str(config_home)
        _, payload = self.pe_sim(
            ["backend", "setup", "--name", "ngspice", "--executable", str(gui)],
            env=env,
            timeout=120,
        )
        if payload is None or payload.get("status") != "known":
            raise AcceptanceError("GUI executable was not replaced by a usable console variant")
        resolution = payload.get("resolution") or {}
        resolved = str(resolution.get("executable", ""))
        notes = " ".join(str(item) for item in resolution.get("notes", []))
        if not resolved.lower().endswith("ngspice_con.exe"):
            raise AcceptanceError(f"GUI variant was not replaced: {resolved}")
        return {
            "requested": gui.name,
            "resolved": Path(resolved).name,
            "notes": notes,
            "summary": "GUI ngspice.exe replaced by ngspice_con.exe",
        }

    def phase_reference_simulation(self) -> dict[str, Any]:
        assert self.ngspice is not None
        config_home = self.workdir / "config-scenario-b"
        env = self.base_env()
        env["PE_SIM_CONFIG_HOME"] = str(config_home)
        env.pop("PE_SIM_NGSPICE", None)
        run_dir = self.checkout / "runs" / "ngspice-buck-reference"
        if run_dir.exists():
            shutil.rmtree(run_dir)

        command = ["run", "examples/ngspice_buck_step/config.json", "--backend", "ngspice-buck"]
        if self.pyspice:
            command.extend(["--model-builder", "pyspice"])
        completed, payload = self.pe_sim(command, env=env, timeout=300)
        if payload is None or payload.get("status") != "QUALIFIED":
            raise AcceptanceError(f"reference run did not qualify: {completed.stdout[-1000:]}")
        manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
        self._verify_package(run_dir, manifest)
        samples = json.loads((run_dir / "samples.json").read_text(encoding="utf-8"))
        self._verify_samples(samples, run_dir)
        if manifest.get("source_commit") != self.snapshot_commit:
            raise AcceptanceError(
                f"manifest source_commit {manifest.get('source_commit')} does not match snapshot {self.snapshot_commit}"
            )
        if manifest.get("working_tree_status") != "clean":
            raise AcceptanceError(f"published run is not from a clean tree: {manifest.get('working_tree_status')}")
        identity = manifest.get("plant", {}).get("identity", {})
        if identity.get("kind") != "ngspice_switching_reference":
            raise AcceptanceError(f"unexpected plant identity: {identity.get('kind')}")
        builder_name = str((identity.get("netlist_builder") or {}).get("name", ""))
        expected_builder = "pyspice-model" if self.pyspice else "native-netlist"
        if builder_name != expected_builder:
            raise AcceptanceError(
                f"reference run used {builder_name!r}, expected {expected_builder!r}"
            )
        backends = manifest.get("environment", {}).get("backends") or []
        if not backends or backends[0].get("status") != "known":
            raise AcceptanceError("manifest backend provenance is not known")
        return {
            "run_dir": str(run_dir),
            "status": manifest.get("status"),
            "evidence_level": manifest.get("evidence_level"),
            "sample_count": len(samples),
            "numpy_npz": (run_dir / "samples.npz").is_file(),
            "netlist_builder": builder_name,
            "backend_version": backends[0].get("version"),
            "manifest_sha256": manifest.get("manifest_sha256"),
            "summary": f"QUALIFIED, {len(samples)} samples, {builder_name}, backend {backends[0].get('version')}",
        }

    def phase_parameter_variation(self) -> dict[str, Any]:
        assert self.ngspice is not None
        config_home = self.workdir / "config-scenario-b"
        env = self.base_env()
        env["PE_SIM_CONFIG_HOME"] = str(config_home)
        source = json.loads((self.checkout / "examples" / "ngspice_buck_step" / "config.json").read_text(encoding="utf-8"))
        config = json.loads(json.dumps(source))
        config["experiment_id"] = "ngspice_buck_user_variation"
        config["run_id"] = "ngspice-buck-user-variation"
        config["plant_config"]["input_voltage_v"] = 24.0
        config["plant_config"]["load_resistance_ohm"] = 5.0
        config["plant_config"]["switching_frequency_hz"] = 200_000.0
        config["plant_config"]["control_period_s"] = 5e-6
        config["timebase"]["control_period_s"] = 5e-6
        config["timebase"]["duration_s"] = 1e-4
        output_dir = self.workdir / "runs-variation"
        config["output"]["directory"] = output_dir.as_posix()
        path = self.workdir / "variation.json"
        path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")

        command = ["run", str(path), "--backend", "ngspice-buck"]
        if self.pyspice:
            command.extend(["--model-builder", "pyspice"])
        _, payload = self.pe_sim(command, env=env, timeout=300)
        if payload is None or payload.get("status") != "QUALIFIED":
            raise AcceptanceError(f"parameterized run did not qualify: {payload}")
        run_dir = output_dir / "ngspice-buck-user-variation"
        manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
        self._verify_package(run_dir, manifest)
        parameters = manifest["plant"]["identity"]["parameters"]
        expected = {
            "input_voltage_v": 24.0,
            "load_resistance_ohm": 5.0,
            "switching_frequency_hz": 200_000.0,
        }
        for name, value in expected.items():
            if abs(float(parameters.get(name, float("nan"))) - value) > 1e-12:
                raise AcceptanceError(f"parameter {name} was not applied: {parameters.get(name)}")
        samples = json.loads((run_dir / "samples.json").read_text(encoding="utf-8"))
        self._verify_samples(samples, run_dir)
        if len(samples) != 20:
            raise AcceptanceError(f"expected 20 control samples for the variation, got {len(samples)}")
        return {
            "run_dir": str(run_dir),
            "parameters": expected,
            "sample_count": len(samples),
            "final_vout": samples[-1]["vout"],
            "summary": f"QUALIFIED with modified parameters, final vout {samples[-1]['vout']:.4f} V",
        }

    def phase_determinism(self) -> dict[str, Any]:
        assert self.ngspice is not None
        config_home = self.workdir / "config-scenario-b"
        env = self.base_env()
        env["PE_SIM_CONFIG_HOME"] = str(config_home)
        source = json.loads((self.checkout / "examples" / "ngspice_buck_step" / "config.json").read_text(encoding="utf-8"))
        directories = [self.workdir / "runs-det-a", self.workdir / "runs-det-b"]
        run_dirs: list[Path] = []
        for index, output_dir in enumerate(directories):
            config = json.loads(json.dumps(source))
            config["run_id"] = "ngspice-buck-determinism"
            config["output"]["directory"] = output_dir.as_posix()
            path = self.workdir / f"determinism-{index}.json"
            path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
            command = ["run", str(path), "--backend", "ngspice-buck"]
            if self.pyspice:
                command.extend(["--model-builder", "pyspice"])
            _, payload = self.pe_sim(command, env=env, timeout=300)
            if payload is None or payload.get("status") != "QUALIFIED":
                raise AcceptanceError(f"determinism run {index} did not qualify: {payload}")
            run_dirs.append(output_dir / "ngspice-buck-determinism")

        completed, comparison = self.pe_sim(
            ["compare-runs", str(run_dirs[0]), str(run_dirs[1])],
            env=env,
            timeout=120,
        )
        if comparison is None or comparison.get("outcome") not in {"exact_match", "tolerance_match"}:
            raise AcceptanceError(f"repeat runs are not comparable: {completed.stdout[-1500:]}")
        _, audit = self.pe_sim(
            ["audit-reproducibility", str(run_dirs[0]), str(run_dirs[1])],
            env=env,
            timeout=120,
        )
        if audit is None or not audit.get("reproducible"):
            raise AcceptanceError(f"repetition audit did not pass: {audit}")
        return {
            "comparison": comparison.get("outcome"),
            "audit": audit.get("outcome"),
            "maximum_absolute_error": comparison.get("maximum_absolute_error"),
            "summary": f"outcome {comparison.get('outcome')}",
        }

    def phase_export(self) -> dict[str, Any]:
        run_dir = self.checkout / "runs" / "ngspice-buck-reference"
        destination = self.workdir / "derived" / "ngspice-buck-reference"
        if destination.exists():
            shutil.rmtree(destination)
        completed, payload = self.pe_sim(
            ["export-workbench", str(run_dir), str(destination)],
            timeout=120,
        )
        if payload is None or payload.get("trace_sample_count", 0) < 2:
            raise AcceptanceError(f"workbench export failed: {completed.stdout[-1000:]}")
        missing = [name for name in _FIXTURE_FILES if not (destination / name).is_file()]
        if missing:
            raise AcceptanceError(f"exported fixture files are missing: {missing}")
        manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
        contracts = {name: json.loads((destination / name).read_text(encoding="utf-8")) for name in _FIXTURE_FILES}
        trace = contracts["trace.json"]
        topology = contracts["topology.json"]
        outputs = contracts["outputs.json"]
        provenance = trace.get("provenance", {})
        if provenance.get("run_id") != manifest.get("run_id"):
            raise AcceptanceError("trace provenance run_id does not match the manifest")
        if provenance.get("manifest_sha256") != manifest.get("manifest_sha256"):
            raise AcceptanceError("trace provenance manifest hash does not match the manifest")
        if provenance.get("package_sha256") != manifest.get("package_sha256"):
            raise AcceptanceError("trace provenance package hash does not match the manifest")
        if len(trace.get("samples", [])) != payload.get("trace_sample_count"):
            raise AcceptanceError("exported trace sample count is inconsistent")
        components = {item.get("kind") for item in topology.get("components", [])}
        expected_components = {
            "voltage_source",
            "pulse_source",
            "voltage_controlled_switch",
            "freewheeling_diode",
            "inductor",
            "capacitor",
            "resistor_load",
        }
        if not expected_components.issubset(components):
            raise AcceptanceError(f"topology descriptor is incomplete: {sorted(components)}")
        scalars = {item.get("output_id"): item.get("payload") for item in outputs.get("outputs", []) if item.get("kind") == "scalar"}
        samples = json.loads((run_dir / "samples.json").read_text(encoding="utf-8"))
        expected_scalars = {
            "vout.final": samples[-1]["vout"],
            "inductor_current.final": samples[-1]["inductor_current"],
            "duty.final": samples[-1]["action"],
        }
        for key, value in expected_scalars.items():
            if key not in scalars or abs(float(scalars[key]) - float(value)) > 1e-12:
                raise AcceptanceError(f"scalar output {key} does not match the final sample")
        return {
            "destination": str(destination),
            "trace_samples": len(trace["samples"]),
            "topology_components": len(topology.get("components", [])),
            "outputs_fingerprint": outputs.get("fingerprint"),
            "summary": f"{len(trace['samples'])} samples, 7 components, 3 contracts",
        }

    def phase_external_consumer(self) -> dict[str, Any]:
        destination = self.workdir / "derived" / "ngspice-buck-reference"
        script = self.checkout / "examples" / "visualization_consumer" / "verify_contracts.py"
        completed = self.run(
            [self.venv_python, str(script), "--directory", str(destination)],
            cwd=self.checkout,
            timeout=120,
        )
        text = completed.stdout
        if "run_id:" not in text or "topology_components:" not in text:
            raise AcceptanceError(f"external consumer did not summarize the contracts: {text[-1000:]}")
        fields: dict[str, str] = {}
        for line in text.splitlines():
            if ":" in line:
                key, value = line.split(":", 1)
                fields[key.strip()] = value.strip()
        if fields.get("run_id") != "ngspice-buck-reference":
            raise AcceptanceError(f"external consumer run_id mismatch: {fields.get('run_id')}")
        return {
            "consumer": "examples/visualization_consumer/verify_contracts.py",
            "run_id": fields.get("run_id"),
            "manifest_sha256": fields.get("manifest_sha256"),
            "trace_samples": fields.get("trace_samples"),
            "summary": "standard-library consumer validated the contracts",
        }

    def phase_environment_check(self) -> dict[str, Any]:
        _, payload = self.pe_sim(["environment-check"], timeout=120, expect=(0, 1))
        if payload is None:
            raise AcceptanceError("environment-check did not return JSON")
        return {
            "status": payload.get("status"),
            "verified_python": payload.get("python", {}).get("verified"),
            "actual_python": payload.get("python", {}).get("actual"),
            "note": "informational: the fresh venv is not the pinned release toolchain",
            "summary": f"informational only (release-toolchain status {payload.get('status')})",
        }

    # -- package verification ---------------------------------------------

    def _verify_package(self, run_dir: Path, manifest: Mapping[str, Any]) -> None:
        missing = [name for name in _REQUIRED_RUN_FILES if not (run_dir / name).is_file()]
        if missing:
            raise AcceptanceError(f"run package is missing artifacts: {missing}")
        if manifest.get("status") != "QUALIFIED":
            raise AcceptanceError(f"run status is {manifest.get('status')}, expected QUALIFIED")
        checks = manifest.get("checks")
        if not isinstance(checks, Mapping) or not checks.get("qualification", {}).get("passed"):
            raise AcceptanceError(f"manifest qualification checks did not pass: {checks}")
        if not checks.get("safety", {}).get("passed") or not checks.get("validity", {}).get("passed"):
            raise AcceptanceError(f"manifest safety/validity checks did not pass: {checks}")

        state = json.loads((run_dir / ".run-state.json").read_text(encoding="utf-8"))
        if state.get("status") != "PUBLISHED":
            raise AcceptanceError(f"run state is not PUBLISHED: {state}")

        sys.path.insert(0, str(self.repo / "src"))
        try:
            from pe_sim.artifacts import artifact_index, manifest_digest, package_digest
        finally:
            sys.path.pop(0)
        try:
            actual = artifact_index(run_dir)
        except (OSError, ValueError) as exc:
            raise AcceptanceError(f"artifact index could not be recomputed: {exc}") from exc
        declared = manifest.get("artifacts") or {}
        if set(declared) != set(actual):
            raise AcceptanceError("manifest artifact index does not cover exactly the package files")
        for name, expected in actual.items():
            entry = declared.get(name) or {}
            if entry.get("sha256") != expected.get("sha256"):
                raise AcceptanceError(f"artifact hash mismatch for {name}")
        if manifest_digest(manifest) != manifest.get("manifest_sha256"):
            raise AcceptanceError("manifest_sha256 does not match the manifest contents")
        if package_digest(run_dir, manifest) != manifest.get("package_sha256"):
            raise AcceptanceError("package_sha256 does not match the package contents")

        schema_path = self.repo / "schemas" / "manifest.schema.json"
        try:
            import jsonschema
        except ImportError as exc:  # pragma: no cover - harness runs under the project venv
            raise AcceptanceError("the acceptance interpreter lacks jsonschema") from exc
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        errors = sorted(jsonschema.Draft202012Validator(schema).iter_errors(manifest), key=lambda item: list(item.absolute_path))
        if errors:
            details = "; ".join(f"{'.'.join(str(part) for part in error.absolute_path) or '<root>'}: {error.message}" for error in errors[:3])
            raise AcceptanceError(f"manifest failed schema validation: {details}")

    def _verify_samples(self, samples: Sequence[Mapping[str, Any]], run_dir: Path) -> None:
        if len(samples) < 2:
            raise AcceptanceError("run must publish at least two samples")
        previous = -1.0
        for index, sample in enumerate(samples):
            for key in ("time_s", "vout", "inductor_current", "action"):
                value = sample.get(key)
                if not isinstance(value, (int, float)) or isinstance(value, bool):
                    raise AcceptanceError(f"sample {index} field {key} is not numeric")
                if value != value or value in (float("inf"), float("-inf")):
                    raise AcceptanceError(f"sample {index} field {key} is not finite")
            if float(sample["time_s"]) <= previous:
                raise AcceptanceError("sample times are not strictly increasing")
            previous = float(sample["time_s"])
        metrics = json.loads((run_dir / "metrics.json").read_text(encoding="utf-8"))
        if metrics.get("sample_count") != len(samples):
            raise AcceptanceError("metrics sample_count does not match samples.json")


def _find_ngspice(explicit: str | None) -> Path | None:
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_file():
            raise AcceptanceError(f"ngspice executable does not exist: {path}")
        return path.resolve()
    candidate = os.environ.get("PE_SIM_NGSPICE")
    if candidate and Path(candidate).expanduser().is_file():
        return Path(candidate).expanduser().resolve()
    for name in ("ngspice_con", "ngspice_con.exe", "ngspice", "ngspice.exe"):
        located = shutil.which(name)
        if located:
            return Path(located).resolve()
    return None


def _report(acceptance: Acceptance) -> dict[str, Any]:
    statuses = [step.status for step in acceptance.steps]
    if "fail" in statuses:
        status = "fail"
    elif "skip" in statuses:
        status = "partial"
    else:
        status = "pass"
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "generated_at": now,
        "host": {
            "platform": platform.platform(),
            "machine": platform.machine(),
            "python": platform.python_version(),
            "os_name": os.name,
        },
        "source": {
            "repository": str(acceptance.repo),
            "head_commit": acceptance.source_head,
            "branch": acceptance.source_branch,
            "snapshot_commit": acceptance.snapshot_commit,
            "basis": "working-tree export (includes uncommitted changes)",
        },
        "workspace": str(acceptance.workdir),
        "checkout": str(acceptance.checkout),
        "install_mode": acceptance.install_mode,
        "simulator": {
            "name": "ngspice",
            "status": "known" if acceptance.ngspice else "not_run",
            "executable_name": next(
                (
                    step.evidence.get("executable_name")
                    for step in acceptance.steps
                    if step.name == "installed_simulator"
                ),
                None,
            ),
            "version": next(
                (
                    step.evidence.get("version")
                    for step in acceptance.steps
                    if step.name == "installed_simulator"
                ),
                None,
            ),
            "sha256": next(
                (
                    step.evidence.get("executable_sha256")
                    for step in acceptance.steps
                    if step.name == "installed_simulator"
                ),
                None,
            ),
        },
        "steps": [step.to_dict() for step in acceptance.steps],
        "limitations": list(acceptance.limitations),
    }


def _markdown(report: Mapping[str, Any]) -> str:
    lines = [
        "# User acceptance report",
        "",
        f"- status: **{report['status']}**",
        f"- generated: {report['generated_at']}",
        f"- source snapshot: `{report['source']['snapshot_commit']}`",
        f"- host: {report['host']['platform']} / Python {report['host']['python']}",
        f"- install: {report['install_mode']}",
        "- reference builder: "
        + str(
            next(
                (
                    step.get("evidence", {}).get("netlist_builder")
                    for step in report["steps"]
                    if step.get("name") == "reference_simulation"
                ),
                "not_run",
            )
        ),
        f"- simulator: {report['simulator']['status']}"
        + (f" (ngspice {report['simulator']['version']})" if report["simulator"].get("version") else ""),
        "",
        "| step | status | detail |",
        "| --- | --- | --- |",
    ]
    for step in report["steps"]:
        detail = str(step.get("detail") or step.get("evidence", {}).get("summary") or "").replace("|", "\\|")
        lines.append(f"| `{step['name']}` | {step['status']} | {detail} |")
    if report["limitations"]:
        lines.extend(["", "## Limitations", ""])
        lines.extend(f"- {item}" for item in report["limitations"])
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run end-user acceptance against a fresh checkout snapshot.")
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parent.parent, help="source repository to export")
    parser.add_argument("--workdir", type=Path, default=None, help="acceptance workspace; defaults to a temporary directory")
    parser.add_argument("--python", default=sys.executable, help="base Python used to create the fresh venv")
    parser.add_argument("--install-mode", choices=("editable", "wheel"), default="editable")
    parser.add_argument("--ngspice", default=None, help="reviewed ngspice console executable")
    parser.add_argument("--require-ngspice", action="store_true", help="fail when no simulator is available")
    parser.add_argument(
        "--skip-pyspice",
        action="store_true",
        help="use the native netlist fallback instead of the PySpice reference construction path",
    )
    parser.add_argument("--report", type=Path, default=None, help="JSON report path")
    args = parser.parse_args(argv)

    repo = args.repo_root.expanduser().resolve()
    if not (repo / "pyproject.toml").is_file():
        parser.error(f"not a project checkout: {repo}")
    workdir = (args.workdir.expanduser().resolve() if args.workdir else Path(tempfile.mkdtemp(prefix="pe-sim-user-acceptance-")))
    workdir.mkdir(parents=True, exist_ok=True)
    checkout = workdir / "checkout"

    ngspice: Path | None
    try:
        ngspice = _find_ngspice(args.ngspice)
    except AcceptanceError as exc:
        parser.error(str(exc))
    if ngspice is None and args.require_ngspice:
        parser.error("--require-ngspice was requested but no ngspice executable was found")

    acceptance = Acceptance(
        repo=repo,
        workdir=workdir,
        checkout=checkout,
        python=args.python,
        install_mode=args.install_mode,
        ngspice=ngspice,
        pyspice=not args.skip_pyspice,
    )
    if ngspice is None:
        acceptance.limitations.append("no ngspice executable was available; switching-level phases were skipped")
    if not acceptance.pyspice:
        acceptance.limitations.append(
            "PySpice was skipped; the native netlist fallback is not the reference construction path"
        )
    acceptance.limitations.extend(
        [
            "the snapshot is an export of the working tree so uncommitted changes are included; publish a commit before treating it as a released clone",
            "QUALIFIED is an automated per-run state, not a hardware, product, or physical-validation claim",
            "the fresh venv is not the pinned release toolchain; environment-check is recorded as informational",
            "cross-machine numerical reproducibility is not assessed",
        ]
    )

    print(f"[acceptance] workspace: {workdir}", flush=True)
    print(f"[acceptance] ngspice: {ngspice or 'not found'}", flush=True)
    exit_code = 0
    try:
        acceptance.step("source_identity", acceptance.phase_source_identity)
        acceptance.step("checkout_snapshot", acceptance.phase_snapshot_checkout)
        acceptance.step("fresh_venv", acceptance.phase_fresh_venv)
        acceptance.step("install_package", acceptance.phase_install)
        acceptance.step("backend_registry", acceptance.phase_registry)
        acceptance.step("scenario_a_no_simulator", acceptance.phase_no_simulator)
        if ngspice is None:
            acceptance.skip("installed_simulator", "no ngspice executable was supplied or found")
            acceptance.skip("gui_variant_preference", "requires a Windows ngspice installation")
            acceptance.skip("reference_simulation", "requires ngspice")
            acceptance.skip("parameter_variation", "requires ngspice")
            acceptance.skip("determinism", "requires ngspice")
            acceptance.skip("data_export", "requires a switching-level reference run")
            acceptance.skip("external_consumer", "requires exported visualization contracts")
        else:
            acceptance.step("installed_simulator", acceptance.phase_installed_simulator)
            acceptance.step("gui_variant_preference", acceptance.phase_gui_preference, critical=False)
            acceptance.step("reference_simulation", acceptance.phase_reference_simulation)
            acceptance.step("parameter_variation", acceptance.phase_parameter_variation)
            acceptance.step("determinism", acceptance.phase_determinism)
            acceptance.step("data_export", acceptance.phase_export)
            acceptance.step("external_consumer", acceptance.phase_external_consumer)
        acceptance.step("environment_check_informational", acceptance.phase_environment_check, critical=False)
    except AcceptanceError:
        exit_code = 2

    report = _report(acceptance)
    report_path = args.report.expanduser().resolve() if args.report else workdir / "acceptance-report.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    markdown_path = report_path.with_suffix(".md")
    markdown_path.write_text(_markdown(report), encoding="utf-8")

    print("")
    print(f"[acceptance] status: {report['status']}")
    print(f"[acceptance] report: {report_path}")
    print(f"[acceptance] summary: {markdown_path}")
    if report["status"] == "fail":
        return 2
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())

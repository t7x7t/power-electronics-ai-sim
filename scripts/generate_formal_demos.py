"""Generate the two official L1 reference demo packages for a clean commit.

This is intentionally separate from ``release_check.ps1``.  It creates new
run packages only in an explicit, initially empty output directory; it never
changes source files, Git state, or an existing run package.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Any

EXPECTED = {
    "python": "3.12.7",
    "pytest": "7.4.4",
    "jsonschema": "4.23.0",
    "numpy": "1.26.4",
    "node": "v24.15.0",
    "npm": "11.12.1",
}


def _run(command: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> str:
    completed = subprocess.run(command, cwd=cwd, env=env, check=False, capture_output=True, text=True)
    if completed.returncode:
        raise RuntimeError(
            f"command failed ({completed.returncode}): {' '.join(command)}\n"
            f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
        )
    return completed.stdout.strip()


def _toolchain(cwd: Path) -> dict[str, str]:
    import importlib.metadata as metadata

    try:
        observed = {
            "python": ".".join(map(str, sys.version_info[:3])),
            "pytest": metadata.version("pytest"),
            "jsonschema": metadata.version("jsonschema"),
            "numpy": metadata.version("numpy"),
            "node": _run(["node", "--version"], cwd=cwd),
            # npm is a .cmd shim on Windows and must be invoked explicitly
            # when subprocess bypasses the shell.
            "npm": _run(["npm.cmd" if sys.platform == "win32" else "npm", "--version"], cwd=cwd),
        }
    except metadata.PackageNotFoundError as exc:
        raise RuntimeError(f"verified Python package is not installed: {exc}") from exc
    mismatches = [f"{key}: expected {EXPECTED[key]}, observed {observed[key]}" for key in EXPECTED if observed[key] != EXPECTED[key]]
    if mismatches:
        raise RuntimeError("exact verified toolchain required; " + "; ".join(mismatches))
    return observed


def _git_state(root: Path) -> str:
    commit = _run(["git", "rev-parse", "HEAD"], cwd=root)
    if len(commit) != 40 or any(char not in "0123456789abcdef" for char in commit.lower()):
        raise RuntimeError("Git HEAD is not a complete commit identity")
    status = _run(["git", "status", "--porcelain=v1", "--untracked-files=all"], cwd=root)
    if status:
        raise RuntimeError("formal demo generation requires a clean Git worktree:\n" + status)
    return commit


def _empty_output(root: Path) -> None:
    if root.exists():
        if not root.is_dir() or any(root.iterdir()):
            raise RuntimeError(f"formal demo output must be a new or empty directory: {root}")
    else:
        root.mkdir(parents=True)


def _config(source: Path, topology: str, output_root: Path, contracts: dict[str, dict[str, str]]) -> Path:
    config = json.loads((source / "examples" / f"{topology}_pi_step" / "config.json").read_text(encoding="utf-8"))
    config["run_id"] = topology
    config["output"]["directory"] = str(output_root)
    config["contracts"] = contracts
    handle = tempfile.NamedTemporaryFile("w", suffix=f"-{topology}.json", encoding="utf-8", delete=False)
    with handle:
        json.dump(config, handle, sort_keys=True, indent=2)
        handle.write("\n")
    return Path(handle.name)


def _validate_run(run_dir: Path, topology: str, commit: str) -> dict[str, Any]:
    from pe_sim.workbench_export import load_run_workbench_contracts

    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("run_mode") != "formal_comparison":
        raise RuntimeError(f"{topology} did not run in formal_comparison mode")
    if manifest.get("source_commit") != commit:
        raise RuntimeError(f"{topology} source_commit does not match current clean commit")
    if manifest.get("working_tree_status") != "clean":
        raise RuntimeError(f"{topology} manifest does not record a clean worktree")
    contracts = load_run_workbench_contracts(run_dir)
    return {
        "topology": topology,
        "run_id": manifest.get("run_id"),
        "status": manifest.get("status"),
        "run_mode": manifest.get("run_mode"),
        "source_commit": manifest.get("source_commit"),
        "manifest_sha256": contracts.manifest_sha256,
        "package_sha256": contracts.package_sha256,
        "claims": ["functional L1 ideal averaged reference evidence only", "not physical, thermal, switching, hardware, or product evidence"],
    }


def generate(source: Path, output_root: Path) -> dict[str, Any]:
    source = source.resolve()
    output_root = output_root.resolve()
    try:
        output_root.relative_to(source)
    except ValueError:
        pass
    else:
        raise RuntimeError("formal demo output must be outside the source checkout so the first run cannot dirty Git")
    commit = _git_state(source)
    toolchain = _toolchain(source)
    _empty_output(output_root)
    source_path = str(source / "src")
    if source_path not in sys.path:
        sys.path.insert(0, source_path)
    from pe_sim.default_contracts import default_contract_refs

    contracts = default_contract_refs()
    env = dict(__import__("os").environ)
    env["PYTHONPATH"] = str(source / "src") + (";" + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    records = []
    temp_configs: list[Path] = []
    try:
        for topology in ("buck", "boost"):
            config = _config(source, topology, output_root, contracts)
            temp_configs.append(config)
            _run([sys.executable, "-m", "pe_sim.cli", "run", str(config), "--backend", topology, "--mode", "formal_comparison"], cwd=source, env=env)
            records.append(_validate_run(output_root / topology, topology, commit))
    finally:
        for config in temp_configs:
            config.unlink(missing_ok=True)
    evidence = {
        "schema_version": "pe-sim.formal-reference-demo-evidence.v1",
        "source_commit": commit,
        "toolchain": toolchain,
        "runs": records,
        "scope": "functional L1 ideal averaged Buck/Boost reference packages",
        "limitations": ["No physical, thermal, switching, Ngspice, hardware, or product claim."],
    }
    (output_root / "formal-demo-evidence.json").write_text(json.dumps(evidence, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return evidence


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output_root", type=Path, help="new or empty directory receiving buck/, boost/, and evidence")
    parser.add_argument("--source", type=Path, default=Path.cwd(), help="clean repository root")
    args = parser.parse_args(argv)
    try:
        result = generate(args.source, args.output_root)
    except (OSError, RuntimeError, ValueError, KeyError) as exc:
        print(json.dumps({"status": "FORMAL_DEMO_REJECTED", "error": str(exc)}, sort_keys=True))
        return 2
    print(json.dumps(result, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Small external consumer for the three Stage 11 visualization contracts.

This file intentionally uses only the Python standard library.  It is a
starting point for an AI Agent, CI job, report generator, or display plugin;
it neither imports pe_sim nor obtains access to simulation objects.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any
from urllib.request import urlopen


EXPECTED_VERSIONS = {
    "topology": ("schema", "topology-descriptor-v1"),
    "trace": ("schema_version", "1.0"),
    "outputs": ("schema_version", "1.0"),
}


def _load_json(location: str) -> dict[str, Any]:
    if location.startswith(("http://", "https://")):
        with urlopen(location, timeout=5) as response:  # nosec B310: explicit user-selected local endpoint
            value = json.loads(response.read().decode("utf-8"))
    else:
        value = json.loads(Path(location).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{location} is not a JSON object")
    return value


def load_contracts(*, directory: Path | None = None, service: str | None = None, run: str | None = None) -> dict[str, dict[str, Any]]:
    if directory is not None:
        locations = {plane: str(directory / f"{plane}.json") for plane in EXPECTED_VERSIONS}
    elif service is not None and run is not None:
        root = service.rstrip("/")
        locations = {plane: f"{root}/v1/runs/{run}/{plane}" for plane in EXPECTED_VERSIONS}
    else:
        raise ValueError("provide either directory or service plus run")
    return {plane: _load_json(location) for plane, location in locations.items()}


def validate_contracts(contracts: dict[str, dict[str, Any]]) -> str:
    run_ids: set[str] = set()
    for plane, (field, expected) in EXPECTED_VERSIONS.items():
        document = contracts.get(plane)
        if not isinstance(document, dict) or document.get(field) != expected:
            raise ValueError(f"{plane} must use {field} {expected!r}")
        if plane == "topology":
            if not isinstance(document.get("fingerprint"), str):
                raise ValueError("topology is missing its fingerprint")
            continue
        provenance = document.get("provenance")
        if not isinstance(provenance, dict) or not isinstance(provenance.get("run_id"), str):
            raise ValueError(f"{plane} is missing provenance.run_id")
        run_ids.add(provenance["run_id"])
    if len(run_ids) != 1:
        raise ValueError("contracts do not identify the same provenance.run_id")
    return run_ids.pop()


def display_summary(contracts: dict[str, dict[str, Any]], run_id: str) -> str:
    trace = contracts["trace"]
    provenance = trace["provenance"]
    components = contracts["topology"].get("components", [])
    samples = trace.get("samples", [])
    scalars = [entry for entry in contracts["outputs"].get("outputs", []) if entry.get("kind") == "scalar"]
    latest = samples[-1].get("values", {}) if samples else {}
    lines = [
        f"run_id: {run_id}",
        f"producer: {provenance.get('producer', 'unknown')}",
        f"manifest_sha256: {provenance.get('manifest_sha256', 'unknown')}",
        f"package_sha256: {provenance.get('package_sha256', 'unknown')}",
        f"topology_components: {len(components)}",
        f"trace_samples: {len(samples)}",
        "latest_values: " + json.dumps(latest, ensure_ascii=True, sort_keys=True),
        "scalar_outputs: " + json.dumps({entry.get("output_id"): entry.get("payload") for entry in scalars}, ensure_ascii=True, sort_keys=True),
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read the public Stage 11 visualization contracts without pe_sim.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--directory", type=Path, help="directory containing topology.json, trace.json, outputs.json")
    group.add_argument("--service", help="base URL of the read-only visualization service")
    parser.add_argument("--run", help="run ID required with --service")
    args = parser.parse_args(argv)
    if args.service and not args.run:
        parser.error("--run is required with --service")
    try:
        contracts = load_contracts(directory=args.directory, service=args.service, run=args.run)
        run_id = validate_contracts(contracts)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    print(display_summary(contracts, run_id))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

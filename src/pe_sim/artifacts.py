"""Atomic run directory writer with deterministic JSON and SHA-256 hashes."""

from __future__ import annotations

from pathlib import Path
import hashlib
import json
import os
import shutil
import tempfile
from typing import Any, Mapping


def canonical_json(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


class ArtifactWriter:
    REQUIRED = ("manifest.json", "config.snapshot.json", "environment.json", "qualification.json", "safety.json", "metrics.json", "events.json", "samples.json", "logs/.keep")

    def __init__(self, root: str | Path, run_id: str):
        self.root = Path(root)
        self.run_id = run_id
        self.root.mkdir(parents=True, exist_ok=True)
        self.tmp = Path(tempfile.mkdtemp(prefix=f".{run_id}.", dir=self.root))
        (self.tmp / "logs").mkdir(parents=True, exist_ok=True)
        (self.tmp / "logs" / ".keep").write_bytes(b"")

    def write_json(self, name: str, value: Any) -> None:
        path = self.tmp / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(canonical_json(value))

    def finalize(self) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        target = self.root / self.run_id
        if target.exists():
            raise FileExistsError(target)
        missing = [name for name in self.REQUIRED if not (self.tmp / name).exists()]
        if missing:
            raise ValueError(f"missing required artifacts: {missing}")
        os.replace(self.tmp, target)
        return target

    def abort(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)


def artifact_index(run_dir: str | Path) -> dict[str, dict[str, Any]]:
    root = Path(run_dir)
    result: dict[str, dict[str, Any]] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.name != "manifest.json":
            payload = path.read_bytes()
            result[path.relative_to(root).as_posix()] = {"path": path.relative_to(root).as_posix(), "sha256": sha256_bytes(payload)}
    return result

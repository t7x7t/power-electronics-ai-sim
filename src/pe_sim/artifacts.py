"""Atomic run directory writer with deterministic JSON and SHA-256 hashes."""

from __future__ import annotations

from pathlib import Path
import hashlib
import json
import os
import shutil
import tempfile
import re
import time
from typing import Any, Mapping


def canonical_json(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _without_derived_hashes(value: Mapping[str, Any]) -> dict[str, Any]:
    """Return the manifest view used by the derived digest functions.

    The digest fields are deliberately removed before hashing.  In
    particular, ``package_sha256`` cannot be included in the package input
    because that would make the digest recursive.  Keeping this operation in
    one helper gives independent readers a precise recomputation rule.
    """

    result = dict(value)
    for key in (
        "manifest_sha256",
        "package_sha256",
        "manifest_hash",
        "package_hash",
    ):
        result.pop(key, None)
    hashes = result.get("hashes")
    if isinstance(hashes, Mapping):
        hashes = dict(hashes)
        hashes.pop("manifest_sha256", None)
        hashes.pop("package_sha256", None)
        hashes.pop("manifest_hash", None)
        hashes.pop("package_hash", None)
        if hashes:
            result["hashes"] = hashes
        else:
            result.pop("hashes", None)
    return result


def manifest_digest(manifest: Mapping[str, Any]) -> str:
    """Compute the non-self-referential SHA-256 digest of a Manifest."""

    return sha256_bytes(canonical_json(_without_derived_hashes(manifest)))


class ArtifactWriter:
    REQUIRED = ("manifest.json", "config.snapshot.json", "environment.json", "qualification.json", "safety.json", "metrics.json", "events.json", "samples.json", "logs/.keep")

    def __init__(self, root: str | Path, run_id: str):
        self.root = Path(root)
        self.run_id = run_id
        self.root.mkdir(parents=True, exist_ok=True)
        self.abandoned_runs = scan_abandoned_runs(self.root)
        self.tmp = Path(tempfile.mkdtemp(prefix=f".{run_id}.", dir=self.root))
        (self.tmp / "logs").mkdir(parents=True, exist_ok=True)
        (self.tmp / "logs" / ".keep").write_bytes(b"")
        self._write_state("RUNNING", reason="writer_created")

    def _write_state(self, status: str, *, reason: str | None = None) -> None:
        payload: dict[str, Any] = {
            "run_id": self.run_id,
            "status": status,
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        if reason:
            payload["reason"] = reason
        _atomic_write(self.tmp / ".run-state.json", canonical_json(payload))

    def write_json(self, name: str, value: Any) -> None:
        path = self.tmp / name
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(path, canonical_json(value))

    def finalize(self) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        target = self.root / self.run_id
        if target.exists():
            raise FileExistsError(target)
        missing = [name for name in self.REQUIRED if not (self.tmp / name).exists()]
        if missing:
            raise ValueError(f"missing required artifacts: {missing}")
        # Keep the temporary directory discoverable as incomplete until the
        # directory rename succeeds. A failed rename must never be hidden as
        # an already-published run.
        self._write_state("READY_TO_PUBLISH", reason="artifact_validation_passed")
        os.replace(self.tmp, target)
        _atomic_write(
            target / ".run-state.json",
            canonical_json(
                {
                    "run_id": self.run_id,
                    "status": "PUBLISHED",
                    "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                }
            ),
        )
        _fsync_directory(self.root)
        return target

    def abort(self) -> None:
        if self.tmp.exists():
            self._write_state("ABORTED", reason="writer_abort")
        shutil.rmtree(self.tmp, ignore_errors=True)


def _fsync_directory(path: Path) -> None:
    """Flush a directory entry where the platform supports it."""

    try:
        fd = os.open(str(path), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except (OSError, ValueError):
        # Windows and some filesystems do not allow directory fsync.  The
        # atomic rename still prevents a partially named run from publishing.
        return


def _atomic_write(path: Path, payload: bytes) -> None:
    """Write and replace one artifact without exposing a truncated file."""

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


# ``run_id`` may contain dots; the final component is tempfile's random suffix.
# A marker check below keeps unrelated hidden directories out of the report.
_TEMP_RUN_RE = re.compile(r"^\.(?P<run_id>.+)\.[^.]+$")


def scan_abandoned_runs(root: str | Path) -> list[dict[str, Any]]:
    """Inspect writer temporary directories without deleting or publishing them.

    A temporary directory is evidence of an interrupted or crashed writer.  It
    is intentionally reported as ``INCOMPLETE`` and left in place so users can
    inspect partial logs/checkpoints before cleanup.  Unrelated hidden
    directories (for example ``.git``) are ignored by the strict name pattern.
    """

    root_path = Path(root)
    if not root_path.exists():
        return []
    reports: list[dict[str, Any]] = []
    for path in sorted(root_path.iterdir(), key=lambda item: item.name):
        if not path.is_dir():
            continue
        match = _TEMP_RUN_RE.match(path.name)
        if not match:
            continue
        marker = path / ".run-state.json"
        metadata: dict[str, Any] = {}
        if marker.exists():
            try:
                loaded = json.loads(marker.read_text(encoding="utf-8"))
                if isinstance(loaded, Mapping):
                    metadata = dict(loaded)
            except (OSError, ValueError, json.JSONDecodeError):
                metadata = {"marker_error": "unreadable"}
        if metadata.get("status") == "PUBLISHED":
            continue
        missing = [name for name in ArtifactWriter.REQUIRED if not (path / name).exists()]
        reports.append(
            {
                "path": path.name,
                "run_id": str(metadata.get("run_id") or match.group("run_id")),
                "status": "INCOMPLETE",
                "reason": "abandoned_temporary_directory",
                "missing_artifacts": missing,
                "marker": metadata,
            }
        )
    return reports


def recover_abandoned_runs(root: str | Path) -> list[dict[str, Any]]:
    """Return safe recovery candidates; never silently converts them to OK.

    This helper is deliberately non-destructive.  A caller may inspect the
    returned records and then remove a temporary directory after preserving
    any evidence.  The normal Runner uses the scan as startup evidence.
    """

    return scan_abandoned_runs(root)


def artifact_index(run_dir: str | Path) -> dict[str, dict[str, Any]]:
    root = Path(run_dir)
    result: dict[str, dict[str, Any]] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.name != "manifest.json":
            payload = path.read_bytes()
            result[path.relative_to(root).as_posix()] = {"path": path.relative_to(root).as_posix(), "sha256": sha256_bytes(payload)}
    return result


def package_digest(run_dir: str | Path, manifest: Mapping[str, Any]) -> str:
    """Compute a deterministic digest for a published run directory.

    Files are sorted by POSIX relative path and represented by their byte
    SHA-256.  ``manifest.json`` is represented by its canonical content with
    the package digest removed; all other files are included verbatim.  This
    makes the result cover the complete package while avoiding a recursive
    package-hash field.
    """

    root = Path(run_dir)
    files = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.name == "manifest.json":
            continue
        relative = path.relative_to(root).as_posix()
        files.append({"path": relative, "sha256": sha256_bytes(path.read_bytes())})
    manifest_view = _without_derived_hashes(manifest)
    payload = {"manifest": manifest_view, "files": files}
    return sha256_bytes(canonical_json(payload))

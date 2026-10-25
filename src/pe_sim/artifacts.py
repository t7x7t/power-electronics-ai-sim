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


_RUN_ID_PATTERN = re.compile(r"[A-Za-z0-9_.-]+")

#: Windows reserves these names in every directory, with or without an
#: extension.  A run directory cannot use them.
_RESERVED_RUN_IDS = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{index}" for index in range(1, 10)}
    | {f"LPT{index}" for index in range(1, 10)}
)


def validate_run_id(run_id: Any) -> str:
    """Return ``run_id`` when it is safe as a directory and file component.

    This is the only run-id rule in the project.  The write path and the
    attestation path used to apply different rules, so a formal run could
    reach ``comparable`` with a package that could never be attested
    (``CON``, ``AUX``, ``COM1``, ...), and a trailing dot made the filesystem
    silently rename ``trailing.`` to ``trailing`` while the manifest kept the
    original string.  A run id that cannot be attested must not be creatable.
    """

    if not isinstance(run_id, str) or not run_id:
        raise ValueError("run_id must be a non-empty relative identifier")
    if run_id in {".", ".."} or ".." in run_id or not _RUN_ID_PATTERN.fullmatch(run_id):
        raise ValueError(
            "run_id must contain only letters, digits, '.', '_' and '-' and must "
            "not traverse directories"
        )
    # Windows reserves the device names on the base name, before the first dot:
    # ``NUL.txt`` is the NUL device, not a file.  Checking only the whole string
    # left the rule weaker than this comment says it is.
    if run_id.split(".", 1)[0].upper() in _RESERVED_RUN_IDS:
        raise ValueError(f"run_id {run_id!r} is a reserved device name on Windows")
    if run_id.endswith((".", " ")):
        raise ValueError("run_id must not end with a dot or a space")
    return run_id


def validate_run_location(root: str | Path, run_id: str, *, check_conflict: bool = True) -> Path:
    """Validate a run output path without creating any filesystem state."""

    validate_run_id(run_id)
    root_path = Path(root).expanduser().resolve()
    target = (root_path / run_id).resolve()
    try:
        target.relative_to(root_path)
    except ValueError as exc:
        raise ValueError("run_id escapes the output directory") from exc
    if check_conflict and target.exists():
        raise FileExistsError(target)
    stale = [item for item in root_path.glob(f".{run_id}.*") if item.is_dir()]
    if check_conflict and stale:
        raise FileExistsError(f"temporary run directory already exists for {run_id}: {stale[0]}")
    return target


class ArtifactWriter:
    REQUIRED = ("manifest.json", "config.snapshot.json", "environment.json", "qualification.json", "safety.json", "metrics.json", "events.json", "samples.json", "logs/.keep")

    def __init__(self, root: str | Path, run_id: str):
        self.root = Path(root)
        self.run_id = run_id
        validate_run_location(self.root, run_id)
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
        _validate_runtime_manifest(self.tmp)
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
        # The state marker is transaction metadata, not immutable run
        # evidence.  Its status and timestamp change during publication, so
        # including it would make a pre-publication package digest unstable.
        if path.is_file() and path.name not in {"manifest.json", ".run-state.json"}:
            payload = path.read_bytes()
            result[path.relative_to(root).as_posix()] = {"path": path.relative_to(root).as_posix(), "sha256": sha256_bytes(payload)}
    return result


def package_digest(run_dir: str | Path, manifest: Mapping[str, Any]) -> str:
    """Compute a deterministic digest for a published run directory.

    Immutable files are sorted by POSIX relative path and represented by their
    byte SHA-256.  ``manifest.json`` is represented by its canonical content
    with the package digest removed; ``.run-state.json`` is transaction
    metadata and is excluded because its publication status changes.  This
    makes the result cover the complete evidence package while avoiding a
    recursive package-hash field.
    """

    root = Path(run_dir)
    files = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.name in {"manifest.json", ".run-state.json"}:
            continue
        relative = path.relative_to(root).as_posix()
        files.append({"path": relative, "sha256": sha256_bytes(path.read_bytes())})
    manifest_view = _without_derived_hashes(manifest)
    payload = {"manifest": manifest_view, "files": files}
    return sha256_bytes(canonical_json(payload))


_PACKAGE_HASH = re.compile(r"[0-9a-f]{64}")


def verify_package(manifest: Mapping[str, Any], run_dir: str | Path) -> tuple[str, ...]:
    """Return every way a package contradicts its own recorded derived hashes.

    This is the single implementation of the package self-consistency rule.
    Publication, inspection, attestation, restricted post-processing, workbench
    export, and reproducibility comparison all call it instead of re-deriving
    the rule, because the copies had drifted: ``inspect-run`` once accepted an
    altered ``hashes`` summary that every other reader rejected.

    The rule covers the derived-hash protocol only — the summary, plus the two
    digests recomputed from the manifest and from the files on disk.  Artifact
    index equality, the ``PUBLISHED`` marker, and provenance eligibility are
    separate rules that not every reader needs, so they stay with their readers.

    An empty tuple means the package is self-consistent.  The order is stable,
    so a caller that reports only the first problem gets a deterministic
    message; the messages are the ones readers already reported.
    """

    problems: list[str] = []
    manifest_hash = manifest.get("manifest_sha256")
    package_hash = manifest.get("package_sha256")
    manifest_hash_ok = isinstance(manifest_hash, str) and bool(_PACKAGE_HASH.fullmatch(manifest_hash))
    package_hash_ok = isinstance(package_hash, str) and bool(_PACKAGE_HASH.fullmatch(package_hash))
    if not manifest_hash_ok:
        problems.append("manifest_sha256 is missing or malformed")
    if not package_hash_ok:
        problems.append("package_sha256 is missing or malformed")
    hashes = manifest.get("hashes")
    if (
        not isinstance(hashes, Mapping)
        or hashes.get("manifest_sha256") != manifest_hash
        or hashes.get("package_sha256") != package_hash
    ):
        problems.append("manifest hash summary is missing or inconsistent")
    if manifest_hash_ok:
        try:
            if manifest_digest(manifest) != manifest_hash:
                problems.append("manifest_sha256 does not match manifest contents")
        except (TypeError, ValueError):
            problems.append("manifest cannot be canonically hashed")
    if package_hash_ok:
        try:
            if package_digest(run_dir, manifest) != package_hash:
                problems.append("package_sha256 does not match package contents")
        except (OSError, TypeError, ValueError):
            problems.append("package cannot be canonically hashed")
    return tuple(problems)


def _validate_runtime_manifest(directory: Path) -> None:
    """Reject an incomplete runtime summary before directory publication.

    ``ArtifactWriter`` also supports small generic fixture packages whose
    manifest is intentionally just ``{}``; those remain compatible.  A
    runtime manifest is identified by its publication status/artifact index
    fields and must carry internally consistent derived hashes while it is
    still in the temporary directory.
    """

    path = directory / "manifest.json"
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"manifest.json is unreadable: {type(exc).__name__}: {exc}") from exc
    if not isinstance(loaded, Mapping):
        raise ValueError("manifest.json must contain an object")
    if not any(key in loaded for key in ("status", "artifacts", "manifest_sha256", "package_sha256", "hashes")):
        return
    artifacts = loaded.get("artifacts")
    if not isinstance(artifacts, Mapping) or not artifacts:
        raise ValueError("runtime manifest artifact index is missing")
    actual = artifact_index(directory)
    if dict(artifacts) != actual:
        raise ValueError("runtime manifest artifact index is incomplete or inconsistent")
    problems = verify_package(loaded, directory)
    if problems:
        raise ValueError(f"runtime {problems[0]}")

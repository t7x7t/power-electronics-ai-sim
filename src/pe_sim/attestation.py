"""Detached run attestations with an optional Git anchor (authenticity L1).

The run package already contains a manifest digest and a package digest.  A
writer who can edit the package can also recompute those digests, so integrity
is not authenticity.  This module records the package identity in a detached
attestation and, when a Git repository is available, appends the identity to a
Git-tracked anchor index.  Trust level ``L1_anchored`` means the exact anchor
record and attestation were found in a reachable Git commit.

The payload/signature split is deliberate: Level 2 signatures sign
``payload_sha256`` and are appended to ``signatures`` without changing the
payload or the anchor.  No cryptographic dependency is imported here.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
from typing import Any, Mapping, Protocol, runtime_checkable

from .artifacts import (
    artifact_index,
    canonical_json,
    validate_run_id,
    verify_package,
)
from .jsonutil import stable_sha256


ATTESTATION_SCHEMA_VERSION = "pe-sim.attestation.v1"
ANCHOR_SCHEMA_VERSION = "pe-sim.attestation-anchor.v1"

#: Schema tag for the ``verify-attestation`` view.  It labels the answer, not
#: the attestation document, whose own version stays ``ATTESTATION_SCHEMA_VERSION``.
ATTESTATION_VERIFICATION_SCHEMA = "pe-sim.attestation-verification.v1"

#: Schema tag for the ``attest-run`` write result.
ATTESTATION_WRITE_SCHEMA = "pe-sim.attestation-write.v1"

ATTESTATION_DIRNAME = "attestations"
ANCHOR_INDEX_NAME = "anchor-index.jsonl"
DEFAULT_INDEX_RELATIVE = f"{ATTESTATION_DIRNAME}/{ANCHOR_INDEX_NAME}"

TRUST_LEVELS = ("none", "L1_pending", "L1_anchored")
SIGNATURE_STATUSES = ("not_signed", "valid", "invalid", "unknown")


class AttestationError(ValueError):
    """Raised when an attestation cannot be produced or verified safely."""


@dataclass(frozen=True)
class AttestationPayload:
    schema_version: str
    run_id: str
    experiment_id: str
    manifest_sha256: str
    package_sha256: str
    artifact_index_sha256: str
    source_commit: str
    working_tree_status: str
    package_status: str
    run_state_status: str
    run_mode: str | None
    producer_name: str
    producer_version: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "experiment_id": self.experiment_id,
            "manifest_sha256": self.manifest_sha256,
            "package_sha256": self.package_sha256,
            "artifact_index_sha256": self.artifact_index_sha256,
            "source_commit": self.source_commit,
            "working_tree_status": self.working_tree_status,
            "package_status": self.package_status,
            "run_state_status": self.run_state_status,
            "run_mode": self.run_mode,
            "producer": {
                "name": self.producer_name,
                "version": self.producer_version,
            },
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "AttestationPayload":
        producer = value.get("producer") if isinstance(value.get("producer"), Mapping) else {}
        return cls(
            schema_version=str(value.get("schema_version", "")),
            run_id=str(value.get("run_id", "")),
            experiment_id=str(value.get("experiment_id", "")),
            manifest_sha256=str(value.get("manifest_sha256", "")),
            package_sha256=str(value.get("package_sha256", "")),
            artifact_index_sha256=str(value.get("artifact_index_sha256", "")),
            source_commit=str(value.get("source_commit", "")),
            working_tree_status=str(value.get("working_tree_status", "")),
            package_status=str(value.get("package_status", "")),
            run_state_status=str(value.get("run_state_status", "")),
            run_mode=None if value.get("run_mode") is None else str(value.get("run_mode")),
            producer_name=str(producer.get("name", "pe-sim")),
            producer_version=None if producer.get("version") is None else str(producer.get("version")),
        )


def payload_digest(payload: AttestationPayload | Mapping[str, Any]) -> str:
    value = payload.to_dict() if isinstance(payload, AttestationPayload) else dict(payload)
    return stable_sha256(value)


@dataclass(frozen=True)
class SignatureRecord:
    format: str
    raw: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return dict(self.raw)


@dataclass(frozen=True)
class AttestationDocument:
    payload: AttestationPayload
    payload_sha256: str
    signatures: tuple[SignatureRecord, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "payload": self.payload.to_dict(),
            "payload_sha256": self.payload_sha256,
            "signatures": [item.to_dict() for item in self.signatures],
        }


@runtime_checkable
class SignatureVerifier(Protocol):
    def verify(self, *, payload_sha256: str, signature: Mapping[str, Any]) -> str: ...


_SIGNATURE_VERIFIERS: dict[str, SignatureVerifier] = {}


def register_signature_verifier(format_name: str, verifier: SignatureVerifier) -> None:
    """Register an optional L2 verifier without changing L1 behaviour."""

    if not isinstance(format_name, str) or not format_name.strip():
        raise ValueError("signature format name must be non-empty")
    if not callable(getattr(verifier, "verify", None)):
        raise TypeError("signature verifier must provide verify()")
    _SIGNATURE_VERIFIERS[format_name] = verifier


def unregister_signature_verifier(format_name: str) -> None:
    _SIGNATURE_VERIFIERS.pop(format_name, None)


@dataclass(frozen=True)
class AnchorRecord:
    schema_version: str
    run_id: str
    payload_sha256: str
    package_sha256: str
    manifest_sha256: str
    artifact_index_sha256: str
    attestation_path: str
    source_commit: str
    recorded_at_utc: str
    record_sha256: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "payload_sha256": self.payload_sha256,
            "package_sha256": self.package_sha256,
            "manifest_sha256": self.manifest_sha256,
            "artifact_index_sha256": self.artifact_index_sha256,
            "attestation_path": self.attestation_path,
            "source_commit": self.source_commit,
            "recorded_at_utc": self.recorded_at_utc,
            "record_sha256": self.record_sha256,
        }


def anchor_record_digest(record: AnchorRecord | Mapping[str, Any]) -> str:
    value = record.to_dict() if isinstance(record, AnchorRecord) else dict(record)
    value.pop("record_sha256", None)
    # ``recorded_at_utc`` is human-review metadata, not identity.  Excluding it
    # makes the anchor digest reproducible by a verifier that only has the
    # attestation payload, and keeps re-attestation idempotent.
    value.pop("recorded_at_utc", None)
    return stable_sha256(value)


def build_anchor_record(
    payload: AttestationPayload,
    *,
    attestation_path: str,
    recorded_at_utc: str | None = None,
) -> AnchorRecord:
    record = AnchorRecord(
        schema_version=ANCHOR_SCHEMA_VERSION,
        run_id=payload.run_id,
        payload_sha256=payload_digest(payload),
        package_sha256=payload.package_sha256,
        manifest_sha256=payload.manifest_sha256,
        artifact_index_sha256=payload.artifact_index_sha256,
        attestation_path=attestation_path,
        source_commit=payload.source_commit,
        recorded_at_utc=recorded_at_utc or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        record_sha256="",
    )
    return replace(record, record_sha256=anchor_record_digest(record))


@dataclass(frozen=True)
class GitAnchorStatus:
    status: str
    repo_root: str | None = None
    index_path: str | None = None
    record_sha256: str | None = None
    record_present_locally: bool | None = None
    record_present_in_commit: bool | None = None
    anchor_commit: str | None = None
    anchor_commit_author: str | None = None
    attestation_in_commit: bool | None = None
    matching_commit_count: int = 0
    worktree_status: str = "unknown"
    shallow: bool | None = None
    limitations: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "repo_root": self.repo_root,
            "index_path": self.index_path,
            "record_sha256": self.record_sha256,
            "record_present_locally": self.record_present_locally,
            "record_present_in_commit": self.record_present_in_commit,
            "anchor_commit": self.anchor_commit,
            "anchor_commit_author": self.anchor_commit_author,
            "attestation_in_commit": self.attestation_in_commit,
            "matching_commit_count": int(self.matching_commit_count),
            "worktree_status": self.worktree_status,
            "shallow": self.shallow,
            "limitations": list(self.limitations),
        }


@dataclass(frozen=True)
class AttestationVerification:
    status: str
    trust_level: str
    signature_status: str
    run_dir: str
    attestation_path: str | None
    run_id: str | None = None
    payload_sha256: str | None = None
    manifest_sha256: str | None = None
    package_sha256: str | None = None
    artifact_index_sha256: str | None = None
    source_commit: str | None = None
    anchor: GitAnchorStatus = GitAnchorStatus(status="not_checked")
    signatures: tuple[dict[str, Any], ...] = ()
    problems: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": ATTESTATION_VERIFICATION_SCHEMA,
            "status": self.status,
            "trust_level": self.trust_level,
            "signature_status": self.signature_status,
            "run_dir": self.run_dir,
            "attestation_path": self.attestation_path,
            "run_id": self.run_id,
            "payload_sha256": self.payload_sha256,
            "manifest_sha256": self.manifest_sha256,
            "package_sha256": self.package_sha256,
            "artifact_index_sha256": self.artifact_index_sha256,
            "source_commit": self.source_commit,
            "anchor": self.anchor.to_dict(),
            "signatures": [dict(item) for item in self.signatures],
            "problems": list(self.problems),
            "limitations": list(self.limitations),
        }


@dataclass(frozen=True)
class AttestationWriteResult:
    status: str
    run_id: str | None
    run_dir: str
    attestation_path: str | None
    attestation_written: bool
    idempotent: bool
    payload_sha256: str | None
    manifest_sha256: str | None
    package_sha256: str | None
    artifact_index_sha256: str | None
    source_commit: str | None
    working_tree_status: str | None
    package_status: str | None
    run_state_status: str | None
    producer: Mapping[str, Any] | None
    signature_status: str
    anchor: GitAnchorStatus
    record_sha256: str | None = None
    record_appended: bool = False
    problems: tuple[str, ...] = ()
    next_steps: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": ATTESTATION_WRITE_SCHEMA,
            "status": self.status,
            "run_id": self.run_id,
            "run_dir": self.run_dir,
            "attestation_path": self.attestation_path,
            "attestation_written": bool(self.attestation_written),
            "idempotent": bool(self.idempotent),
            "payload_sha256": self.payload_sha256,
            "manifest_sha256": self.manifest_sha256,
            "package_sha256": self.package_sha256,
            "artifact_index_sha256": self.artifact_index_sha256,
            "source_commit": self.source_commit,
            "working_tree_status": self.working_tree_status,
            "package_status": self.package_status,
            "run_state_status": self.run_state_status,
            "producer": dict(self.producer) if isinstance(self.producer, Mapping) else None,
            "signature_status": self.signature_status,
            "anchor": self.anchor.to_dict(),
            "record_sha256": self.record_sha256,
            "record_appended": bool(self.record_appended),
            "problems": list(self.problems),
            "next_steps": list(self.next_steps),
        }


def _load_json(path: Path, description: str) -> Mapping[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AttestationError(f"{description} is unreadable: {type(exc).__name__}: {exc}") from exc
    if not isinstance(value, Mapping):
        raise AttestationError(f"{description} must be a JSON object")
    return value


def _validate_run_id(run_id: str) -> None:
    """Apply the shared run-id rule and report it as an attestation error."""

    try:
        validate_run_id(run_id)
    except ValueError as exc:
        raise AttestationError(str(exc)) from exc


def build_payload(run_dir: str | Path) -> tuple[AttestationPayload, Mapping[str, Any]]:
    """Recompute the package identity and return a detached payload."""

    directory = Path(run_dir).expanduser().resolve()
    if not directory.is_dir():
        raise AttestationError(f"run directory does not exist: {directory}")
    manifest = _load_json(directory / "manifest.json", "manifest.json")
    state = _load_json(directory / ".run-state.json", ".run-state.json")
    if state.get("status") != "PUBLISHED":
        raise AttestationError("the run package is not PUBLISHED")
    run_id = manifest.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        raise AttestationError("manifest run_id is missing")
    if state.get("run_id") not in {None, run_id}:
        raise AttestationError("run state does not match the manifest run_id")
    _validate_run_id(run_id)

    actual_artifacts = artifact_index(directory)
    recorded_artifacts = manifest.get("artifacts")
    if not isinstance(recorded_artifacts, Mapping) or dict(recorded_artifacts) != dict(actual_artifacts):
        raise AttestationError("manifest artifact index does not match the package contents")
    problems = verify_package(manifest, directory)
    if problems:
        raise AttestationError(problems[0])

    environment = manifest.get("environment") if isinstance(manifest.get("environment"), Mapping) else {}
    runner = environment.get("runner") if isinstance(environment.get("runner"), Mapping) else {}
    payload = AttestationPayload(
        schema_version=ATTESTATION_SCHEMA_VERSION,
        run_id=run_id,
        experiment_id=str(manifest.get("experiment_id", "")),
        manifest_sha256=str(manifest.get("manifest_sha256")),
        package_sha256=str(manifest.get("package_sha256")),
        artifact_index_sha256=stable_sha256(dict(actual_artifacts)),
        source_commit=str(manifest.get("source_commit", "unavailable")),
        working_tree_status=str(manifest.get("working_tree_status", "unknown")),
        package_status=str(manifest.get("status", "")),
        run_state_status=str(state.get("status")),
        run_mode=None if manifest.get("run_mode") is None else str(manifest.get("run_mode")),
        producer_name=str(runner.get("name", "pe_sim")),
        producer_version=None if runner.get("version") is None else str(runner.get("version")),
    )
    return payload, manifest


def attestation_filename(run_id: str) -> str:
    _validate_run_id(run_id)
    return f"{run_id}.attestation.json"


def default_attestation_path(attestations_dir: str | Path, run_id: str) -> Path:
    return Path(attestations_dir) / attestation_filename(run_id)


def _discover_repository(start: str | Path) -> Path | None:
    completed = _run_git(Path(start), "rev-parse", "--show-toplevel")
    if completed is None or completed.returncode != 0:
        return None
    value = completed.stdout.strip()
    return Path(value).resolve() if value else None


def _run_git(
    cwd: Path,
    *args: str,
    timeout_s: float = 30.0,
) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            ["git", "-C", str(cwd), *args],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_s,
        )
    except (FileNotFoundError, OSError, subprocess.SubprocessError, ValueError):
        return None


def _relative_posix(path: Path, root: Path) -> str | None:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return None


def read_anchor_index(index_path: str | Path) -> tuple[tuple[AnchorRecord, ...], tuple[str, ...]]:
    path = Path(index_path)
    if not path.is_file():
        return (), ()
    records: list[AnchorRecord] = []
    problems: list[str] = []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        return (), (f"anchor index is unreadable: {type(exc).__name__}: {exc}",)
    for line_number, line in enumerate(lines, start=1):
        text = line.strip()
        if not text:
            continue
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            problems.append(f"anchor index line {line_number} is not valid JSON")
            continue
        if not isinstance(value, Mapping):
            problems.append(f"anchor index line {line_number} is not an object")
            continue
        digest = anchor_record_digest(value)
        if value.get("record_sha256") not in {None, digest}:
            problems.append(f"anchor index line {line_number} has an inconsistent record digest")
            continue
        records.append(
            AnchorRecord(
                schema_version=str(value.get("schema_version", ANCHOR_SCHEMA_VERSION)),
                run_id=str(value.get("run_id", "")),
                payload_sha256=str(value.get("payload_sha256", "")),
                package_sha256=str(value.get("package_sha256", "")),
                manifest_sha256=str(value.get("manifest_sha256", "")),
                artifact_index_sha256=str(value.get("artifact_index_sha256", "")),
                attestation_path=str(value.get("attestation_path", "")),
                source_commit=str(value.get("source_commit", "")),
                recorded_at_utc=str(value.get("recorded_at_utc", "")),
                record_sha256=digest,
            )
        )
    return tuple(records), tuple(problems)


def _append_anchor_record(index_path: Path, record: AnchorRecord) -> tuple[bool, str]:
    index_path.parent.mkdir(parents=True, exist_ok=True)
    existing, _ = read_anchor_index(index_path)
    if any(item.record_sha256 == record.record_sha256 for item in existing):
        return False, "record already present"
    payload = canonical_json(record.to_dict())
    needs_newline = False
    if index_path.is_file():
        try:
            data = index_path.read_bytes()
        except OSError as exc:
            return False, f"anchor index is unreadable: {type(exc).__name__}: {exc}"
        needs_newline = bool(data) and not data.endswith(b"\n")
    with index_path.open("ab") as handle:
        if needs_newline:
            handle.write(b"\n")
        handle.write(payload)
    return True, "record appended"


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except OSError:
            pass


def _signature_status(signatures: tuple[Mapping[str, Any], ...], payload_sha256: str) -> str:
    if not signatures:
        return "not_signed"
    results: list[str] = []
    for signature in signatures:
        format_name = str(signature.get("format", ""))
        verifier = _SIGNATURE_VERIFIERS.get(format_name)
        if verifier is None:
            results.append("unknown")
            continue
        try:
            result = verifier.verify(payload_sha256=payload_sha256, signature=signature)
        except Exception:
            results.append("unknown")
            continue
        results.append(result if result in SIGNATURE_STATUSES else "unknown")
    if "invalid" in results:
        return "invalid"
    if "valid" in results and "unknown" not in results:
        return "valid"
    if "valid" in results:
        return "valid"
    return "unknown"


def verify_git_anchor(
    record: AnchorRecord,
    *,
    repo_root: str | Path,
    index_relative_path: str = DEFAULT_INDEX_RELATIVE,
    timeout_s: float = 30.0,
) -> GitAnchorStatus:
    """Check whether the exact anchor record is committed in the repository."""

    root = Path(repo_root).expanduser().resolve()
    if not root.is_dir():
        return GitAnchorStatus(
            status="not_a_repository",
            record_sha256=record.record_sha256,
            limitations=("the repository path does not exist",),
        )
    top = _discover_repository(root)
    if top is None:
        return GitAnchorStatus(
            status="not_a_repository",
            record_sha256=record.record_sha256,
            limitations=("Git anchor could not be verified: not a git repository",),
        )
    top_status = _run_git(top, "status", "--porcelain=v1", "--untracked-files=all", timeout_s=timeout_s)
    worktree_status = "unknown"
    if top_status is not None and top_status.returncode == 0:
        worktree_status = "dirty" if top_status.stdout.strip() else "clean"
    shallow_result = _run_git(top, "rev-parse", "--is-shallow-repository", timeout_s=timeout_s)
    shallow = None
    if shallow_result is not None and shallow_result.returncode == 0:
        shallow = shallow_result.stdout.strip().lower() == "true"

    local_index = top / index_relative_path
    local_records, _ = read_anchor_index(local_index)
    record_present_locally = any(item.record_sha256 == record.record_sha256 for item in local_records)

    log = _run_git(
        top,
        "log",
        "--all",
        "--format=%H",
        "--",
        index_relative_path,
        timeout_s=timeout_s,
    )
    if log is None:
        return GitAnchorStatus(
            status="git_unavailable",
            repo_root=str(top),
            index_path=index_relative_path,
            record_sha256=record.record_sha256,
            record_present_locally=record_present_locally,
            worktree_status=worktree_status,
            shallow=shallow,
            limitations=("Git is unavailable; the anchor cannot be verified",),
        )
    commits = [line.strip() for line in log.stdout.splitlines() if line.strip()]
    matching: list[str] = []
    attestation_in_commit = False
    latest_attestation_commit: str | None = None
    for commit in commits:
        shown = _run_git(top, "show", f"{commit}:{index_relative_path}", timeout_s=timeout_s)
        if shown is None or shown.returncode != 0:
            continue
        for line in shown.stdout.splitlines():
            text = line.strip()
            if not text:
                continue
            try:
                value = json.loads(text)
            except json.JSONDecodeError:
                continue
            if not isinstance(value, Mapping):
                continue
            if anchor_record_digest(value) == record.record_sha256:
                matching.append(commit)
                attestation = _run_git(
                    top,
                    "show",
                    f"{commit}:{record.attestation_path}",
                    timeout_s=timeout_s,
                )
                if attestation is not None and attestation.returncode == 0:
                    try:
                        document = json.loads(attestation.stdout)
                    except json.JSONDecodeError:
                        document = None
                    if isinstance(document, Mapping):
                        payload = document.get("payload")
                        if isinstance(payload, Mapping) and payload_digest(payload) == record.payload_sha256:
                            attestation_in_commit = True
                            latest_attestation_commit = commit
                break
    if not matching:
        status = "uncommitted" if record_present_locally else "record_missing"
        limitations = (
            ("the anchor record exists only in the working tree; commit attestations/ and re-verify",)
            if status == "uncommitted"
            else ("no matching anchor record was found in the reachable Git history",)
        )
        return GitAnchorStatus(
            status=status,
            repo_root=str(top),
            index_path=index_relative_path,
            record_sha256=record.record_sha256,
            record_present_locally=record_present_locally,
            record_present_in_commit=False,
            attestation_in_commit=False,
            matching_commit_count=0,
            worktree_status=worktree_status,
            shallow=shallow,
            limitations=limitations,
        )
    anchor_commit = latest_attestation_commit or matching[-1]
    author_result = _run_git(top, "show", "-s", "--format=%an <%ae>", anchor_commit, timeout_s=timeout_s)
    author = author_result.stdout.strip() if author_result is not None and author_result.returncode == 0 else None
    limitations: tuple[str, ...] = ()
    if not attestation_in_commit:
        limitations = ("the anchor record is committed but the attestation file is not in the same commit",)
    if shallow:
        limitations = limitations + ("the repository is a shallow clone; earlier history may be unavailable",)
    return GitAnchorStatus(
        status="committed" if attestation_in_commit else "attestation_missing_from_commit",
        repo_root=str(top),
        index_path=index_relative_path,
        record_sha256=record.record_sha256,
        record_present_locally=record_present_locally,
        record_present_in_commit=True,
        anchor_commit=anchor_commit,
        anchor_commit_author=author,
        attestation_in_commit=attestation_in_commit,
        matching_commit_count=len(matching),
        worktree_status=worktree_status,
        shallow=shallow,
        limitations=limitations,
    )


def verify_attestation(
    run_dir: str | Path,
    attestation_path: str | Path | None,
    *,
    repo_root: str | Path | None = None,
    check_git: bool = True,
    timeout_s: float = 30.0,
) -> AttestationVerification:
    """Verify a detached attestation against the package bytes."""

    directory = Path(run_dir).expanduser().resolve()
    if attestation_path is None:
        return AttestationVerification(
            status="invalid",
            trust_level="none",
            signature_status="not_signed",
            run_dir=str(directory),
            attestation_path=None,
            problems=("no attestation path was supplied",),
            limitations=("local package hashes prove integrity, not authorship",),
        )
    path = Path(attestation_path).expanduser().resolve()
    try:
        document = _load_json(path, "attestation")
    except AttestationError as exc:
        return AttestationVerification(
            status="invalid",
            trust_level="none",
            signature_status="not_signed",
            run_dir=str(directory),
            attestation_path=str(path),
            problems=(str(exc),),
            limitations=("local package hashes prove integrity, not authorship",),
        )
    payload_value = document.get("payload")
    if not isinstance(payload_value, Mapping):
        return AttestationVerification(
            status="invalid",
            trust_level="none",
            signature_status="not_signed",
            run_dir=str(directory),
            attestation_path=str(path),
            problems=("attestation payload is missing or not an object",),
        )
    try:
        payload = AttestationPayload.from_dict(payload_value)
    except (TypeError, ValueError) as exc:
        return AttestationVerification(
            status="invalid",
            trust_level="none",
            signature_status="not_signed",
            run_dir=str(directory),
            attestation_path=str(path),
            problems=(f"attestation payload is invalid: {type(exc).__name__}: {exc}",),
        )
    problems: list[str] = []
    computed_payload = payload_digest(payload)
    recorded_payload = document.get("payload_sha256")
    if recorded_payload != computed_payload:
        problems.append("payload_sha256 does not match the attestation payload")
    raw_signatures = document.get("signatures")
    signatures: tuple[Mapping[str, Any], ...] = ()
    if raw_signatures is None:
        raw_signatures = []
    if not isinstance(raw_signatures, list) or any(not isinstance(item, Mapping) for item in raw_signatures):
        problems.append("attestation signatures must be an array of objects")
    else:
        signatures = tuple(dict(item) for item in raw_signatures)
    signature_status = _signature_status(signatures, computed_payload)

    try:
        actual, _ = build_payload(directory)
    except AttestationError as exc:
        problems.append(str(exc))
        actual = None
    if actual is not None:
        for field_name in (
            "run_id",
            "experiment_id",
            "manifest_sha256",
            "package_sha256",
            "artifact_index_sha256",
            "source_commit",
            "working_tree_status",
            "package_status",
            "run_state_status",
            "run_mode",
            "producer_name",
            "producer_version",
        ):
            if getattr(payload, field_name) != getattr(actual, field_name):
                problems.append(f"{field_name} does not match the package")

    record = build_anchor_record(payload, attestation_path=(_relative_posix(path, Path(repo_root).resolve()) if repo_root else path.name))
    anchor = GitAnchorStatus(status="not_checked", record_sha256=record.record_sha256)
    if check_git:
        resolved_repo = Path(repo_root).expanduser().resolve() if repo_root is not None else _discover_repository(path.parent)
        if resolved_repo is None:
            anchor = GitAnchorStatus(
                status="not_a_repository",
                record_sha256=record.record_sha256,
                record_present_locally=None,
                limitations=("Git anchor could not be checked: not a git repository",),
            )
        else:
            relative = _relative_posix(path, resolved_repo)
            if relative is None:
                anchor = GitAnchorStatus(
                    status="not_a_repository",
                    record_sha256=record.record_sha256,
                    limitations=("the attestation is outside the repository",),
                )
            else:
                record = build_anchor_record(payload, attestation_path=relative)
                anchor = verify_git_anchor(
                    record,
                    repo_root=resolved_repo,
                    timeout_s=timeout_s,
                )

    if problems:
        status = "invalid"
        trust_level = "none"
    else:
        status = "ok"
        trust_level = (
            "L1_anchored"
            if anchor.status == "committed" and anchor.attestation_in_commit
            else "L1_pending"
        )
    limitations = (
        "L1 proves that the package identity and payload were recorded in Git history; "
        "it does not prove who produced the package or that a reviewer approved it.",
        "Git author and commit time are unauthenticated unless the commit itself is signed.",
    )
    if not problems and trust_level == "L1_pending":
        limitations = limitations + (
            "the attestation is locally consistent but no committed anchor was verified",
        )
    return AttestationVerification(
        status=status,
        trust_level=trust_level,
        signature_status=signature_status,
        run_dir=str(directory),
        attestation_path=str(path),
        run_id=payload.run_id,
        payload_sha256=computed_payload,
        manifest_sha256=payload.manifest_sha256,
        package_sha256=payload.package_sha256,
        artifact_index_sha256=payload.artifact_index_sha256,
        source_commit=payload.source_commit,
        anchor=anchor,
        signatures=signatures,
        problems=tuple(problems),
        limitations=tuple(limitations),
    )


def attest_run(
    run_dir: str | Path,
    *,
    repo_root: str | Path | None = None,
    attestations_dir: str | Path | None = None,
    anchor: bool = True,
    replace_existing: bool = False,
    recorded_at_utc: str | None = None,
) -> AttestationWriteResult:
    """Write a detached attestation and optionally append the Git anchor record.

    This function never runs ``git add``, ``git commit``, or ``git push``.
    """

    directory = Path(run_dir).expanduser().resolve()
    try:
        payload, manifest = build_payload(directory)
    except AttestationError as exc:
        return AttestationWriteResult(
            status="ATTESTATION_REJECTED",
            run_id=None,
            run_dir=str(directory),
            attestation_path=None,
            attestation_written=False,
            idempotent=False,
            payload_sha256=None,
            manifest_sha256=None,
            package_sha256=None,
            artifact_index_sha256=None,
            source_commit=None,
            working_tree_status=None,
            package_status=None,
            run_state_status=None,
            producer=None,
            signature_status="not_signed",
            anchor=GitAnchorStatus(status="not_checked"),
            problems=(str(exc),),
            next_steps=(f"pe-sim inspect-run {directory}",),
        )

    resolved_repo: Path | None = None
    if repo_root is not None:
        resolved_repo = Path(repo_root).expanduser().resolve()
    elif anchor:
        resolved_repo = _discover_repository(Path.cwd())
    if anchor and resolved_repo is None:
        return AttestationWriteResult(
            status="ATTESTATION_REFUSED",
            run_id=payload.run_id,
            run_dir=str(directory),
            attestation_path=None,
            attestation_written=False,
            idempotent=False,
            payload_sha256=payload_digest(payload),
            manifest_sha256=payload.manifest_sha256,
            package_sha256=payload.package_sha256,
            artifact_index_sha256=payload.artifact_index_sha256,
            source_commit=payload.source_commit,
            working_tree_status=payload.working_tree_status,
            package_status=payload.package_status,
            run_state_status=payload.run_state_status,
            producer={"name": payload.producer_name, "version": payload.producer_version},
            signature_status="not_signed",
            anchor=GitAnchorStatus(status="not_a_repository"),
            problems=("Git repository could not be located; pass --repo or use --no-anchor with --attestations-dir",),
        )

    if attestations_dir is None:
        if resolved_repo is None:
            return AttestationWriteResult(
                status="ATTESTATION_REFUSED",
                run_id=payload.run_id,
                run_dir=str(directory),
                attestation_path=None,
                attestation_written=False,
                idempotent=False,
                payload_sha256=payload_digest(payload),
                manifest_sha256=payload.manifest_sha256,
                package_sha256=payload.package_sha256,
                artifact_index_sha256=payload.artifact_index_sha256,
                source_commit=payload.source_commit,
                working_tree_status=payload.working_tree_status,
                package_status=payload.package_status,
                run_state_status=payload.run_state_status,
                producer={"name": payload.producer_name, "version": payload.producer_version},
                signature_status="not_signed",
                anchor=GitAnchorStatus(status="not_a_repository"),
                problems=("--attestations-dir is required when --no-anchor is used without a repository",),
            )
        attestation_root = resolved_repo / ATTESTATION_DIRNAME
    else:
        attestation_root = Path(attestations_dir).expanduser().resolve()

    try:
        relative_attestations = (
            _relative_posix(attestation_root, resolved_repo) if resolved_repo is not None else None
        )
        if anchor and relative_attestations is None:
            raise AttestationError("the attestations directory must be inside the Git repository")
        path = default_attestation_path(attestation_root, payload.run_id)
        try:
            path.resolve().relative_to(directory)
        except ValueError:
            pass
        else:
            raise AttestationError("the attestation path must not be inside the run package")
    except AttestationError as exc:
        return AttestationWriteResult(
            status="ATTESTATION_REJECTED",
            run_id=payload.run_id,
            run_dir=str(directory),
            attestation_path=None,
            attestation_written=False,
            idempotent=False,
            payload_sha256=payload_digest(payload),
            manifest_sha256=payload.manifest_sha256,
            package_sha256=payload.package_sha256,
            artifact_index_sha256=payload.artifact_index_sha256,
            source_commit=payload.source_commit,
            working_tree_status=payload.working_tree_status,
            package_status=payload.package_status,
            run_state_status=payload.run_state_status,
            producer={"name": payload.producer_name, "version": payload.producer_version},
            signature_status="not_signed",
            anchor=GitAnchorStatus(status="not_checked"),
            problems=(str(exc),),
        )

    payload_hash = payload_digest(payload)
    existing_document: Mapping[str, Any] | None = None
    if path.is_file():
        try:
            existing_document = _load_json(path, "attestation")
        except AttestationError:
            existing_document = None
    idempotent = False
    written = False
    if existing_document is not None:
        existing_payload = existing_document.get("payload")
        existing_hash = existing_document.get("payload_sha256")
        if isinstance(existing_payload, Mapping):
            existing_hash = payload_digest(existing_payload)
        if existing_hash == payload_hash:
            idempotent = True
        elif not replace_existing:
            return AttestationWriteResult(
                status="ATTESTATION_CONFLICT",
                run_id=payload.run_id,
                run_dir=str(directory),
                attestation_path=str(path),
                attestation_written=False,
                idempotent=False,
                payload_sha256=payload_hash,
                manifest_sha256=payload.manifest_sha256,
                package_sha256=payload.package_sha256,
                artifact_index_sha256=payload.artifact_index_sha256,
                source_commit=payload.source_commit,
                working_tree_status=payload.working_tree_status,
                package_status=payload.package_status,
                run_state_status=payload.run_state_status,
                producer={"name": payload.producer_name, "version": payload.producer_version},
                signature_status=_signature_status(
                    tuple(
                        dict(item)
                        for item in existing_document.get("signatures", [])
                        if isinstance(item, Mapping)
                    ),
                    payload_hash,
                ),
                anchor=GitAnchorStatus(status="not_checked", record_sha256=None),
                problems=("an attestation with a different payload already exists; use --replace to supersede it",),
                next_steps=(f"pe-sim attest-run {directory} --replace",),
            )
        else:
            superseded = attestation_root / "superseded"
            superseded.mkdir(parents=True, exist_ok=True)
            old_hash = str(existing_hash or "unknown")[:12]
            target = superseded / f"{payload.run_id}.{old_hash}.attestation.json"
            try:
                os.replace(path, target)
            except OSError as exc:
                return AttestationWriteResult(
                    status="ATTESTATION_REJECTED",
                    run_id=payload.run_id,
                    run_dir=str(directory),
                    attestation_path=str(path),
                    attestation_written=False,
                    idempotent=False,
                    payload_sha256=payload_hash,
                    manifest_sha256=payload.manifest_sha256,
                    package_sha256=payload.package_sha256,
                    artifact_index_sha256=payload.artifact_index_sha256,
                    source_commit=payload.source_commit,
                    working_tree_status=payload.working_tree_status,
                    package_status=payload.package_status,
                    run_state_status=payload.run_state_status,
                    producer={"name": payload.producer_name, "version": payload.producer_version},
                    signature_status="not_signed",
                    anchor=GitAnchorStatus(status="not_checked"),
                    problems=(f"existing attestation could not be superseded: {type(exc).__name__}: {exc}",),
                )

    if not idempotent:
        document = AttestationDocument(payload=payload, payload_sha256=payload_hash).to_dict()
        _atomic_write(path, canonical_json(document))
        written = True

    relative_attestation = _relative_posix(path, resolved_repo) if resolved_repo is not None else path.name
    record = build_anchor_record(
        payload,
        attestation_path=relative_attestation or path.name,
        recorded_at_utc=recorded_at_utc,
    )
    record_appended = False
    if anchor:
        index_path = attestation_root / ANCHOR_INDEX_NAME
        record_appended, _ = _append_anchor_record(index_path, record)
    verification = None
    if anchor:
        verification = verify_attestation(
            directory,
            path,
            repo_root=resolved_repo,
            check_git=True,
        )
    return AttestationWriteResult(
        status="ATTESTED",
        run_id=payload.run_id,
        run_dir=str(directory),
        attestation_path=str(path),
        attestation_written=written,
        idempotent=idempotent,
        payload_sha256=payload_hash,
        manifest_sha256=payload.manifest_sha256,
        package_sha256=payload.package_sha256,
        artifact_index_sha256=payload.artifact_index_sha256,
        source_commit=payload.source_commit,
        working_tree_status=payload.working_tree_status,
        package_status=payload.package_status,
        run_state_status=payload.run_state_status,
        producer={"name": payload.producer_name, "version": payload.producer_version},
        signature_status=verification.signature_status if verification is not None else "not_signed",
        anchor=verification.anchor if verification is not None else GitAnchorStatus(status="not_checked"),
        record_sha256=record.record_sha256,
        record_appended=record_appended,
        problems=(),
        next_steps=(
            f"git add {relative_attestation or path.name}",
            f"git commit -m \"attest run {payload.run_id}\"",
            f"pe-sim verify-attestation {directory} --attestation {path}",
        ),
    )


__all__ = [
    "ANCHOR_SCHEMA_VERSION",
    "ATTESTATION_SCHEMA_VERSION",
    "AnchorRecord",
    "AttestationDocument",
    "AttestationError",
    "AttestationPayload",
    "AttestationVerification",
    "AttestationWriteResult",
    "GitAnchorStatus",
    "SignatureRecord",
    "anchor_record_digest",
    "attest_run",
    "attestation_filename",
    "build_anchor_record",
    "build_payload",
    "default_attestation_path",
    "payload_digest",
    "read_anchor_index",
    "register_signature_verifier",
    "unregister_signature_verifier",
    "verify_attestation",
    "verify_git_anchor",
]

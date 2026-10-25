#!/usr/bin/env python3
"""Create, approve, and verify ``pe-sim.release-record.v1`` records.

The record is the machine-checkable form of a human release decision described
by ``docs/53_release_governance_v1.md``.  It ties one version, one commit, one
tag name, the built wheel/sdist, the distribution inventory, and the local gate
evidence together.  Approval is accountability, not cryptography: no artifact
and no tag is signed, and the record adds no simulation, physical, or
engineering claim.

The tool is standard-library only, never uses the network, and shells out only
to ``git`` for tag checks.  When ``git`` is unavailable the tag check reports a
failure or ``not_checked``; it never crashes and never passes.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping
import copy
from datetime import datetime, timezone
from email.parser import Parser as _MetadataParser
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ModuleNotFoundError:  # pragma: no cover - neither parser installed
        tomllib = None  # type: ignore[assignment]

RECORD_SCHEMA_VERSION = "pe-sim.release-record.v1"
RULE_VERSION = "release-record-v1"
EVIDENCE_SCHEMA_VERSION = "pe-sim.local-release-evidence.v1"
INVENTORY_SCHEMA_VERSION = "pe-sim.distribution-inventory.v1"
DISTRIBUTION_NAME = "power-electronics-ai-sim"
DEFAULT_SUPPORTED_PYTHON = ">=3.10"

SCOPE_DOCUMENT = "docs/27_minimum_local_release_scope_v0.md"
TOOLCHAIN_POLICY_DOCUMENT = "docs/28_release_toolchain_policy_v0.md"
GOVERNANCE_DOCUMENT = "docs/53_release_governance_v1.md"
RELEASE_NOTES_DIRECTORY = "docs/releases"

_COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
_VERSION_ASSIGNMENT = re.compile(r"__version__\s*=\s*[\"']([^\"']+)[\"']")

_TOOLCHAIN_FIELDS = ("python", "pytest", "jsonschema", "numpy")

LIMITATIONS = [
    "Approval records human accountability, not a cryptographic signature; "
    "the approval block can be written by hand.",
    "No artifact and no tag is signed; signature.status remains not_signed and "
    "no signing method is claimed.",
    "This record adds no simulation, physical, or engineering claim; it records "
    "identity, hashes, and non-claims only.",
    "Artifact hashes identify the exact files that were inspected; they do not "
    "claim that rebuilding the same commit yields byte-identical files.",
]

PENDING_APPROVAL = {
    "status": "pending",
    "reviewer": None,
    "reviewed_at_utc": None,
    "decision": None,
    "statement": None,
    "reviewed_record_digest": None,
}

EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_PARTIAL = 2


class RecordError(ValueError):
    """Raised when a record cannot be produced, approved, or read safely."""


# ---------------------------------------------------------------------------
# Canonical JSON and file helpers
# ---------------------------------------------------------------------------


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise RecordError(f"cannot read {path}: {exc}") from exc
    return digest.hexdigest()


def _read_json(path: Path) -> Any:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        raise RecordError(f"cannot read {path}: {exc}") from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise RecordError(f"cannot parse {path} as JSON: {exc}") from exc


def _write_json(path: Path, value: Any) -> None:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))
    except OSError as exc:
        raise RecordError(f"cannot write {path}: {exc}") from exc


def record_digest(record: dict) -> str:
    """SHA-256 of the canonical record body without ``approval``/``record_digest``.

    ``approval_history`` deliberately stays in the digest: re-approving changes
    the history, which is part of the body a later reviewer can inspect.  The
    current approval block itself is excluded so that approval does not alter
    the identity of the body it binds to.
    """

    body = {
        key: value
        for key, value in record.items()
        if key not in ("approval", "record_digest")
    }
    payload = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Small parsing helpers
# ---------------------------------------------------------------------------


def _format_utc(value: Any) -> str:
    if value is None:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    text = str(value).strip()
    if not text:
        raise RecordError("timestamp must not be empty")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RecordError(f"timestamp is not ISO 8601: {value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _repo_relative(path: Path, repo_root: Path) -> str:
    try:
        return path.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        return path.resolve().as_posix()


def _wheel_metadata_version(wheel: Path) -> str:
    import zipfile

    try:
        with zipfile.ZipFile(wheel) as archive:
            members = [
                name
                for name in archive.namelist()
                if name.endswith(".dist-info/METADATA") and name.count("/") == 1
            ]
            if not members:
                raise RecordError(f"wheel has no .dist-info/METADATA entry: {wheel.name}")
            if len(members) > 1:
                raise RecordError(
                    f"wheel has multiple .dist-info/METADATA entries: {wheel.name}"
                )
            raw = archive.read(members[0])
    except RecordError:
        raise
    except (OSError, zipfile.BadZipFile) as exc:
        raise RecordError(f"cannot read wheel {wheel}: {exc}") from exc
    message = _MetadataParser().parsestr(raw.decode("utf-8", errors="replace"))
    value = message.get("Version")
    if value is None or not str(value).strip():
        raise RecordError(f"wheel METADATA has no Version field: {wheel.name}")
    return str(value).strip()


def _distribution_artifact_sha(inventory: Any) -> str:
    if not isinstance(inventory, Mapping):
        return ""
    distribution = inventory.get("distribution")
    if not isinstance(distribution, Mapping):
        return ""
    artifact = distribution.get("artifact")
    if not isinstance(artifact, Mapping):
        return ""
    value = artifact.get("sha256")
    return value.strip() if isinstance(value, str) else ""


def _resolve_evidence_file(path: Path) -> Path:
    candidate = path / "release-evidence.json" if path.is_dir() else path
    if not candidate.is_file():
        raise RecordError(f"gate evidence file is missing: {candidate}")
    return candidate


def _artifact(kind: str, role: str, path: Path) -> dict:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise RecordError(f"cannot stat {path}: {exc}") from exc
    return {
        "kind": kind,
        "role": role,
        "filename": path.name,
        "sha256": _sha256_file(path),
        "size_bytes": size,
    }


def _supported_python(repo_root: Path) -> str:
    pyproject = repo_root / "pyproject.toml"
    if tomllib is not None and pyproject.is_file():
        try:
            data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
            project = data.get("project")
            if isinstance(project, Mapping):
                value = project.get("requires-python")
                if isinstance(value, str) and value.strip():
                    return value.strip()
        except Exception:  # a malformed pyproject is caught by check-identity
            pass
    return DEFAULT_SUPPORTED_PYTHON


# ---------------------------------------------------------------------------
# Record creation
# ---------------------------------------------------------------------------


def build_record(
    *,
    version,
    evidence_dir,
    wheel,
    sdist,
    inventory,
    formal_demo_evidence=None,
    upgrade_evidence=None,
    notes=None,
    tag=None,
    commit=None,
    repo_root=None,
    generated_at_utc=None,
) -> dict:
    """Build a pending, unsigned record or raise :class:`RecordError`.

    Every validation in ``docs/53_release_governance_v1.md`` runs before the
    caller writes anything, so a rejected invocation leaves no output file.
    """

    version_text = version.strip() if isinstance(version, str) else ""
    if not version_text:
        raise RecordError("version must be a non-empty string")
    repo = Path(repo_root).resolve() if repo_root is not None else Path.cwd().resolve()

    evidence_path = _resolve_evidence_file(Path(evidence_dir))
    evidence = _read_json(evidence_path)
    if not isinstance(evidence, Mapping):
        raise RecordError("gate evidence must be a JSON object")
    if evidence.get("schema_version") != EVIDENCE_SCHEMA_VERSION:
        raise RecordError(
            "gate evidence schema_version must be "
            f"{EVIDENCE_SCHEMA_VERSION!r}, found {evidence.get('schema_version')!r}"
        )
    if evidence.get("outcome") != "pass":
        raise RecordError(
            f"gate evidence outcome must be 'pass', found {evidence.get('outcome')!r}"
        )
    repository = evidence.get("repository")
    if not isinstance(repository, Mapping):
        raise RecordError("gate evidence has no repository block")
    source_commit = repository.get("source_commit")
    if not isinstance(source_commit, str) or not _COMMIT_PATTERN.match(source_commit):
        raise RecordError(
            "gate evidence repository.source_commit must be a 40-character "
            f"lowercase hex commit, found {source_commit!r}"
        )
    if "source_commit_after_checks" in repository and repository[
        "source_commit_after_checks"
    ] is not None:
        after_commit = repository["source_commit_after_checks"]
        if after_commit != source_commit:
            raise RecordError(
                "gate evidence commit changed during the gate: "
                f"source_commit={source_commit!r}, "
                f"source_commit_after_checks={after_commit!r}"
            )
    if commit is not None and str(commit).strip() != source_commit:
        raise RecordError(
            f"--commit {str(commit).strip()!r} does not match gate evidence "
            f"commit {source_commit!r}"
        )

    toolchain_path = evidence_path.parent / "toolchain-observed.json"
    if not toolchain_path.is_file():
        raise RecordError(f"toolchain evidence is missing: {toolchain_path}")
    toolchain = _read_json(toolchain_path)
    observed = toolchain.get("observed") if isinstance(toolchain, Mapping) else None
    if not isinstance(observed, Mapping):
        raise RecordError(
            f"toolchain-observed.json has no observed block: {toolchain_path}"
        )
    verified_toolchain: dict[str, str] = {}
    for name in _TOOLCHAIN_FIELDS:
        value = observed.get(name)
        if not isinstance(value, str) or not value.strip():
            raise RecordError(
                f"toolchain-observed.json observed.{name} must be a non-empty string"
            )
        verified_toolchain[name] = value.strip()

    wheel_path = Path(wheel)
    sdist_path = Path(sdist)
    inventory_path = Path(inventory)
    for label, path in (
        ("wheel", wheel_path),
        ("sdist", sdist_path),
        ("distribution inventory", inventory_path),
    ):
        if not path.is_file():
            raise RecordError(f"{label} file is missing: {path}")

    normalized_version = version_text.replace("-", "_")
    wheel_token = f"power_electronics_ai_sim-{normalized_version}-"
    if wheel_token not in wheel_path.name:
        raise RecordError(
            f"wheel filename must contain {wheel_token!r}, found {wheel_path.name!r}"
        )
    metadata_version = _wheel_metadata_version(wheel_path)
    if metadata_version != version_text:
        raise RecordError(
            f"wheel METADATA Version {metadata_version!r} does not equal "
            f"--version {version_text!r}"
        )
    if version_text not in sdist_path.name:
        raise RecordError(
            f"sdist filename must contain version {version_text!r}, "
            f"found {sdist_path.name!r}"
        )

    inventory_document = _read_json(inventory_path)
    if not isinstance(inventory_document, Mapping):
        raise RecordError("distribution inventory must be a JSON object")
    if inventory_document.get("schema_version") != INVENTORY_SCHEMA_VERSION:
        raise RecordError(
            "distribution inventory schema_version must be "
            f"{INVENTORY_SCHEMA_VERSION!r}, found "
            f"{inventory_document.get('schema_version')!r}"
        )
    distribution = inventory_document.get("distribution")
    inventory_version = (
        distribution.get("version") if isinstance(distribution, Mapping) else None
    )
    if inventory_version != version_text:
        raise RecordError(
            f"inventory distribution.version {inventory_version!r} does not "
            f"equal --version {version_text!r}"
        )
    wheel_sha256 = _sha256_file(wheel_path)
    recorded_wheel_sha256 = _distribution_artifact_sha(inventory_document)
    if recorded_wheel_sha256 != wheel_sha256:
        raise RecordError(
            "inventory distribution.artifact.sha256 "
            f"{recorded_wheel_sha256 or '<missing>'} does not match supplied "
            f"wheel sha256 {wheel_sha256}"
        )

    document_hashes: dict[str, str] = {}
    for relative in (SCOPE_DOCUMENT, TOOLCHAIN_POLICY_DOCUMENT, GOVERNANCE_DOCUMENT):
        document_path = repo / relative
        if not document_path.is_file():
            raise RecordError(f"required scope document is missing: {relative}")
        document_hashes[relative] = _sha256_file(document_path)

    notes_path = (
        Path(notes)
        if notes is not None
        else repo / RELEASE_NOTES_DIRECTORY / f"v{version_text}.md"
    )
    if not notes_path.is_absolute():
        notes_path = repo / notes_path
    if not notes_path.is_file():
        raise RecordError(f"release note is missing: {notes_path}")

    tag_name = str(tag).strip() if tag is not None else f"v{version_text}"
    if not tag_name:
        raise RecordError("tag must be a non-empty string")

    artifacts = [
        _artifact("wheel", "release_artifact", wheel_path),
        _artifact("sdist", "release_artifact", sdist_path),
        _artifact("distribution_inventory", "release_artifact", inventory_path),
        _artifact("gate_evidence", "supporting_evidence", evidence_path),
    ]
    if formal_demo_evidence is not None:
        formal_path = Path(formal_demo_evidence)
        if formal_path.is_dir():
            raise RecordError(
                f"formal demo evidence must be a file, not a directory: {formal_path}"
            )
        if not formal_path.is_file():
            raise RecordError(f"formal demo evidence file is missing: {formal_path}")
        artifacts.append(
            _artifact("formal_demo_evidence", "supporting_evidence", formal_path)
        )
    if upgrade_evidence is not None:
        upgrade_path = Path(upgrade_evidence)
        if upgrade_path.is_dir():
            raise RecordError(
                f"upgrade evidence must be a file, not a directory: {upgrade_path}"
            )
        if not upgrade_path.is_file():
            raise RecordError(f"upgrade evidence file is missing: {upgrade_path}")
        artifacts.append(
            _artifact("upgrade_evidence", "supporting_evidence", upgrade_path)
        )

    record: dict[str, Any] = {
        "schema_version": RECORD_SCHEMA_VERSION,
        "rule_version": RULE_VERSION,
        "release": {
            "distribution": DISTRIBUTION_NAME,
            "version": version_text,
            "tag": tag_name,
            "commit": source_commit,
            "commit_source": "gate_evidence",
            "generated_at_utc": _format_utc(generated_at_utc),
            "release_notes": {
                "path": _repo_relative(notes_path, repo),
                "sha256": _sha256_file(notes_path),
            },
        },
        "scope": {
            "document": SCOPE_DOCUMENT,
            "document_sha256": document_hashes[SCOPE_DOCUMENT],
            "toolchain_policy_document": TOOLCHAIN_POLICY_DOCUMENT,
            "toolchain_policy_sha256": document_hashes[TOOLCHAIN_POLICY_DOCUMENT],
            "governance_document": GOVERNANCE_DOCUMENT,
            "governance_document_sha256": document_hashes[GOVERNANCE_DOCUMENT],
            "supported_python": _supported_python(repo),
            "verified_toolchain": verified_toolchain,
        },
        "artifacts": artifacts,
        "approval": copy.deepcopy(PENDING_APPROVAL),
        "approval_history": [],
        "tag": {"status": "not_created", "name": tag_name, "commit": None},
        "signature": {"status": "not_signed", "method": None},
        "limitations": list(LIMITATIONS),
    }
    record["record_digest"] = record_digest(record)
    return record


# ---------------------------------------------------------------------------
# Approval
# ---------------------------------------------------------------------------


def approve_record(
    record: dict,
    *,
    reviewer,
    statement,
    decision="approve",
    reviewed_at_utc=None,
) -> dict:
    """Return a copy of ``record`` with a new approval block.

    ``reviewed_record_digest`` equals the digest of the body that the reviewer
    is accountable for, which is exactly the recomputed ``record_digest`` after
    archiving any previous approval into ``approval_history``.  That keeps the
    binding invariant that ``verify`` enforces.
    """

    if not isinstance(record, Mapping):
        raise RecordError("record must be a JSON object")

    ci_value = os.environ.get("CI")
    if ci_value is not None and ci_value.strip().lower() not in ("", "0", "false"):
        raise RecordError(
            "refusing to approve when CI is set: approval is a human local "
            f"action, not an automated step (CI={ci_value!r})"
        )

    reviewer_text = reviewer.strip() if isinstance(reviewer, str) else ""
    if not reviewer_text:
        raise RecordError("reviewer must be a non-empty name")
    statement_text = statement.strip() if isinstance(statement, str) else ""
    if len(statement_text) < 10:
        raise RecordError("statement must be at least 10 characters of review text")
    if decision not in ("approve", "reject"):
        raise RecordError(f"decision must be 'approve' or 'reject', found {decision!r}")

    updated: dict[str, Any] = copy.deepcopy(dict(record))
    history = updated.get("approval_history")
    history_list: list[Any] = list(history) if isinstance(history, list) else []
    existing = updated.get("approval")
    if isinstance(existing, Mapping) and existing.get("status") in (
        "approved",
        "rejected",
    ):
        history_list.append(copy.deepcopy(dict(existing)))
    updated["approval_history"] = history_list

    # ``record_digest`` excludes the approval block, so the old block is
    # irrelevant here; ``approval_history`` is included and therefore archived
    # before the digest is computed.
    digest = record_digest(updated)
    updated["approval"] = {
        "status": "approved" if decision == "approve" else "rejected",
        "reviewer": reviewer_text,
        "reviewed_at_utc": _format_utc(reviewed_at_utc),
        "decision": decision,
        "statement": statement_text,
        "reviewed_record_digest": digest,
    }
    updated["record_digest"] = digest
    return updated


# ---------------------------------------------------------------------------
# Git helper (only invoked for tag checks)
# ---------------------------------------------------------------------------


def _run_git(args, *, cwd):
    """Run git and return ``(returncode, stdout, stderr)`` or ``None``.

    ``None`` means the executable could not be started at all.  Callers must
    never treat it as a pass.
    """

    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return completed.returncode, completed.stdout, completed.stderr


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------


def _check(check_id: str, status: str, detail: str) -> dict:
    return {"id": check_id, "status": status, "detail": detail}


def _as_dirs(value) -> list[Path]:
    """Normalize a path or a sequence of paths into a list of directories."""

    if value is None:
        return []
    if isinstance(value, (str, Path)):
        return [Path(value)]
    return [Path(item) for item in value]


def _overall_status(checks: list[dict], optional_ids: set[str]) -> str:
    if any(check["status"] == "fail" for check in checks):
        return "fail"
    blocking = [
        check
        for check in checks
        if check["status"] == "not_checked" and check["id"] not in optional_ids
    ]
    return "partial" if blocking else "pass"


def _tag_check_detail(record: Mapping, repo: Path) -> tuple[str, str]:
    """Return ``(status, detail)`` for ``git rev-parse`` tag verification."""

    tag_block = record.get("tag")
    tag_name = ""
    if isinstance(tag_block, Mapping) and isinstance(tag_block.get("name"), str):
        tag_name = tag_block["name"].strip()
    if not tag_name:
        release = record.get("release")
        if isinstance(release, Mapping) and isinstance(release.get("tag"), str):
            tag_name = release["tag"].strip()
    if not tag_name:
        return "fail", "record does not name a tag to verify"

    release = record.get("release")
    expected_commit = ""
    if isinstance(release, Mapping) and isinstance(release.get("commit"), str):
        expected_commit = release["commit"].strip()
    if not expected_commit:
        return "fail", f"tag {tag_name} cannot be compared: record has no release.commit"

    inside = _run_git(["rev-parse", "--is-inside-work-tree"], cwd=repo)
    if inside is None:
        return "fail", (
            f"tag {tag_name} cannot be verified: the git executable is unavailable"
        )
    inside_code, inside_out, _inside_err = inside
    if inside_code != 0 or inside_out.strip() != "true":
        # A missing repository is not a missing tag: the check could not be
        # performed at all, which is reported as partial rather than failure.
        return "not_checked", f"tag {tag_name} could not be checked: no Git repository at {repo}"

    result = _run_git(
        ["rev-parse", "--verify", f"refs/tags/{tag_name}^{{commit}}"], cwd=repo
    )
    if result is None:
        return "fail", (
            f"tag {tag_name} cannot be verified: the git executable is unavailable"
        )
    code, out, err = result
    if code != 0:
        reason = err.strip() or "git rev-parse failed"
        return "fail", f"tag {tag_name} is missing at {repo} ({reason})"
    actual_commit = out.strip()
    if actual_commit != expected_commit:
        return "fail", (
            f"tag {tag_name} points at {actual_commit}, not the recorded commit "
            f"{expected_commit}"
        )
    return "pass", f"tag {tag_name} points at the recorded commit {expected_commit}"


def verify_record(
    record: dict,
    *,
    record_path=None,
    dist_dir=None,
    evidence_dir=None,
    require_approved=False,
    require_tag=False,
    repo_root=None,
) -> dict:
    """Recompute every binding in ``record`` and return a check list."""

    if not isinstance(record, Mapping):
        record = {}
    checks: list[dict] = []

    # 1. schema
    schema_ok = (
        record.get("schema_version") == RECORD_SCHEMA_VERSION
        and record.get("rule_version") == RULE_VERSION
    )
    checks.append(
        _check(
            "schema",
            "pass" if schema_ok else "fail",
            "schema_version/rule_version match"
            if schema_ok
            else (
                f"expected {RECORD_SCHEMA_VERSION!r}/{RULE_VERSION!r}, found "
                f"{record.get('schema_version')!r}/{record.get('rule_version')!r}"
            ),
        )
    )

    # 2. record digest
    actual_digest = record.get("record_digest")
    recomputed_digest = record_digest(dict(record))
    digest_ok = isinstance(actual_digest, str) and actual_digest == recomputed_digest
    checks.append(
        _check(
            "record-digest",
            "pass" if digest_ok else "fail",
            f"record_digest {actual_digest} matches the canonical body"
            if digest_ok
            else (
                f"record_digest {actual_digest!r} does not match recomputed "
                f"{recomputed_digest}"
            ),
        )
    )

    # 3. approval binding
    approval = record.get("approval")
    approval = approval if isinstance(approval, Mapping) else {}
    approval_status = approval.get("status")
    if approval_status in ("approved", "rejected"):
        reviewer = approval.get("reviewer")
        statement = approval.get("statement")
        reviewed_at = approval.get("reviewed_at_utc")
        fields_ok = all(
            isinstance(value, str) and value.strip()
            for value in (reviewer, statement, reviewed_at)
        )
        binding_ok = (
            isinstance(approval.get("reviewed_record_digest"), str)
            and approval.get("reviewed_record_digest") == actual_digest
        )
        if binding_ok and fields_ok:
            checks.append(
                _check(
                    "approval-binding",
                    "pass",
                    "reviewed_record_digest equals record_digest and the approval "
                    "fields are present",
                )
            )
        else:
            reasons = []
            if not binding_ok:
                reasons.append(
                    "reviewed_record_digest does not equal record_digest"
                )
            if not fields_ok:
                reasons.append("reviewer/statement/reviewed_at_utc are incomplete")
            checks.append(_check("approval-binding", "fail", "; ".join(reasons)))
    else:
        checks.append(
            _check(
                "approval-binding",
                "not_checked",
                f"approval.status is {approval_status!r}; no binding to check",
            )
        )

    # 4. approval required
    if require_approved:
        approved = approval_status == "approved"
        checks.append(
            _check(
                "approval-required",
                "pass" if approved else "fail",
                "approval.status is 'approved'"
                if approved
                else f"--require-approved was given but approval.status is {approval_status!r}",
            )
        )
    else:
        checks.append(
            _check(
                "approval-required",
                "not_checked",
                "--require-approved was not supplied",
            )
        )

    artifacts = record.get("artifacts")
    artifacts = artifacts if isinstance(artifacts, list) else []

    # 5. artifact hashes.  Release artifacts live with the build output; the
    # supporting evidence (gate, demo, upgrade) may be spread across several
    # evidence directories.  A role with no supplied directory is reported as
    # not checked, never silently skipped.
    evidence_dirs = _as_dirs(evidence_dir)
    if dist_dir is None and not evidence_dirs:
        checks.append(
            _check(
                "artifacts-hashed",
                "not_checked",
                "neither --dist nor --evidence-dir was supplied",
            )
        )
    else:
        dist = Path(dist_dir) if dist_dir is not None else None
        problems: list[str] = []
        skipped: list[str] = []
        checked = 0
        if not artifacts:
            problems.append("record has no artifacts")
        for entry in artifacts:
            if not isinstance(entry, Mapping):
                problems.append("artifact entry is not an object")
                continue
            filename = entry.get("filename")
            if not isinstance(filename, str) or not filename:
                problems.append("artifact entry has no filename")
                continue
            if entry.get("role") == "supporting_evidence":
                bases = evidence_dirs
            else:
                bases = [dist] if dist is not None else []
            if not bases:
                skipped.append(filename)
                continue
            path = next(
                (base / filename for base in bases if (base / filename).is_file()),
                None,
            )
            if path is None:
                problems.append(
                    f"{filename}: missing from {', '.join(str(base) for base in bases)}"
                )
                continue
            try:
                found_sha = _sha256_file(path)
                found_size = path.stat().st_size
            except (RecordError, OSError) as exc:
                problems.append(f"{filename}: cannot be read ({exc})")
                continue
            checked += 1
            if found_sha != entry.get("sha256"):
                problems.append(
                    f"{filename}: sha256 {found_sha} != recorded {entry.get('sha256')}"
                )
            elif found_size != entry.get("size_bytes"):
                problems.append(
                    f"{filename}: size {found_size} != recorded "
                    f"{entry.get('size_bytes')}"
                )
        if problems:
            checks.append(_check("artifacts-hashed", "fail", "; ".join(problems)))
        elif skipped:
            checks.append(
                _check(
                    "artifacts-hashed",
                    "not_checked",
                    f"{checked} artifact(s) matched; no directory was supplied for "
                    f"{len(skipped)}: {', '.join(sorted(skipped))}",
                )
            )
        else:
            checks.append(
                _check(
                    "artifacts-hashed",
                    "pass",
                    f"{checked} artifact(s) matched the recorded sha256 and size",
                )
            )

    # 6. inventory binding
    inventory_entry = next(
        (
            entry
            for entry in artifacts
            if isinstance(entry, Mapping) and entry.get("kind") == "distribution_inventory"
        ),
        None,
    )
    wheel_entry = next(
        (
            entry
            for entry in artifacts
            if isinstance(entry, Mapping) and entry.get("kind") == "wheel"
        ),
        None,
    )
    if dist_dir is None:
        checks.append(
            _check(
                "inventory-binding",
                "not_checked",
                "no --dist directory was supplied",
            )
        )
    elif inventory_entry is None:
        checks.append(
            _check(
                "inventory-binding",
                "not_checked",
                "record has no distribution_inventory artifact",
            )
        )
    elif wheel_entry is None:
        checks.append(
            _check(
                "inventory-binding",
                "fail",
                "record has no wheel artifact to bind the inventory to",
            )
        )
    else:
        inventory_name = inventory_entry.get("filename")
        inventory_path = (
            Path(dist_dir) / inventory_name
            if isinstance(inventory_name, str) and inventory_name
            else None
        )
        if inventory_path is None or not inventory_path.is_file():
            checks.append(
                _check(
                    "inventory-binding",
                    "not_checked",
                    f"inventory file cannot be located: {inventory_path}",
                )
            )
        else:
            try:
                inventory_document = _read_json(inventory_path)
            except RecordError as exc:
                checks.append(_check("inventory-binding", "fail", str(exc)))
            else:
                recorded_sha = _distribution_artifact_sha(inventory_document)
                wheel_sha = wheel_entry.get("sha256")
                wheel_sha = wheel_sha if isinstance(wheel_sha, str) else ""
                if not wheel_sha:
                    checks.append(
                        _check(
                            "inventory-binding",
                            "fail",
                            "wheel artifact has no sha256 to compare",
                        )
                    )
                elif recorded_sha != wheel_sha:
                    checks.append(
                        _check(
                            "inventory-binding",
                            "fail",
                            "inventory distribution.artifact.sha256 "
                            f"{recorded_sha or '<missing>'} does not match wheel "
                            f"artifact sha256 {wheel_sha}",
                        )
                    )
                else:
                    checks.append(
                        _check(
                            "inventory-binding",
                            "pass",
                            "inventory distribution.artifact.sha256 equals the "
                            "wheel artifact sha256",
                        )
                    )

    # 7. signature status
    signature = record.get("signature")
    signature = signature if isinstance(signature, Mapping) else {}
    signature_status = signature.get("status")
    if signature_status == "not_signed":
        checks.append(
            _check(
                "signature-status",
                "pass",
                "signature.status is 'not_signed'; no artifact or tag signing is "
                "claimed (policy-consistent for v1)",
            )
        )
    else:
        checks.append(
            _check(
                "signature-status",
                "not_checked",
                f"signature.status is {signature_status!r}; v1 does not verify "
                "signatures, so signing is neither claimed nor checked",
            )
        )

    # 8. limitations
    limitations = record.get("limitations")
    limitations_ok = isinstance(limitations, list) and len(limitations) > 0
    checks.append(
        _check(
            "limitations",
            "pass" if limitations_ok else "fail",
            f"{len(limitations)} limitation(s) recorded"
            if limitations_ok
            else "limitations must be a non-empty list",
        )
    )

    # 9. tag
    repo = Path(repo_root).resolve() if repo_root is not None else Path.cwd().resolve()
    if not require_tag:
        tag_block = record.get("tag")
        tag_name = ""
        if isinstance(tag_block, Mapping) and isinstance(tag_block.get("name"), str):
            tag_name = tag_block["name"].strip()
        checks.append(
            _check(
                "tag",
                "not_checked",
                f"tag {tag_name or '<unnamed>'} was not checked; --require-tag "
                "was not supplied",
            )
        )
    else:
        status, detail = _tag_check_detail(record, repo)
        checks.append(_check("tag", status, detail))

    optional_ids = {"approval-binding"}
    if not require_approved:
        optional_ids.add("approval-required")
    if dist_dir is None or not evidence_dirs:
        optional_ids.add("artifacts-hashed")
    if dist_dir is None:
        optional_ids.add("inventory-binding")
    if not require_tag:
        optional_ids.add("tag")

    return {
        "record": None if record_path is None else str(record_path),
        "record_digest": record.get("record_digest"),
        "status": _overall_status(checks, optional_ids),
        "checks": checks,
    }


# ---------------------------------------------------------------------------
# Identity checks
# ---------------------------------------------------------------------------


def _read_module_version(repo: Path) -> tuple[str | None, str]:
    """Return ``(version, detail)`` for ``pe_sim.__version__`` or ``_version.py``.

    An in-process import is used only when it resolves inside ``repo``; an
    import from an unrelated installed copy (for example the developer's
    checkout while checking a synthetic fixture) must not influence the result,
    so the source file is read instead.
    """

    import_error = ""
    try:
        import pe_sim  # noqa: PLC0415 - intentional, guarded import
    except Exception as exc:  # an import side effect must not abort the check
        import_error = f"{type(exc).__name__}: {exc}"
    else:
        module_file = getattr(pe_sim, "__file__", None)
        if module_file:
            try:
                Path(module_file).resolve().relative_to(repo)
            except ValueError:
                pass
            else:
                value = getattr(pe_sim, "__version__", None)
                if isinstance(value, str) and value.strip():
                    return value.strip(), f"imported pe_sim from {module_file}"
                return (
                    None,
                    f"pe_sim imported from {module_file} does not expose __version__",
                )

    version_file = repo / "src" / "pe_sim" / "_version.py"
    if not version_file.is_file():
        reason = import_error or "the imported pe_sim does not belong to this repository"
        return None, f"pe_sim could not be resolved ({reason}); {version_file} is missing"
    try:
        text = version_file.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        return None, f"cannot read {version_file}: {exc}"
    match = _VERSION_ASSIGNMENT.search(text)
    if match is None:
        return None, f"no __version__ assignment found in {version_file}"
    return match.group(1).strip(), f"read {version_file}"


def check_identity(*, repo_root=None, require_tag=False) -> dict:
    """Check the repository-level single-source-of-version invariants."""

    repo = Path(repo_root).resolve() if repo_root is not None else Path.cwd().resolve()
    checks: list[dict] = []

    version: str | None = None
    if tomllib is None:
        checks.append(
            _check(
                "pyproject-version",
                "not_checked",
                "no TOML parser is available (tomllib/tomli)",
            )
        )
    else:
        pyproject = repo / "pyproject.toml"
        if not pyproject.is_file():
            checks.append(
                _check("pyproject-version", "fail", f"pyproject.toml is missing: {pyproject}")
            )
        else:
            try:
                data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
            except Exception as exc:
                checks.append(
                    _check(
                        "pyproject-version",
                        "fail",
                        f"pyproject.toml could not be parsed: {exc}",
                    )
                )
            else:
                project = data.get("project")
                project = project if isinstance(project, Mapping) else {}
                dynamic = project.get("dynamic")
                value = project.get("version")
                if isinstance(dynamic, list) and "version" in dynamic:
                    checks.append(
                        _check(
                            "pyproject-version",
                            "fail",
                            "project.version must not be listed in project.dynamic",
                        )
                    )
                elif not isinstance(value, str) or not value.strip():
                    checks.append(
                        _check(
                            "pyproject-version",
                            "fail",
                            "project.version must be a non-empty static string",
                        )
                    )
                else:
                    version = value.strip()
                    checks.append(
                        _check(
                            "pyproject-version",
                            "pass",
                            f"project.version is {version!r}",
                        )
                    )

    if version is None:
        checks.append(
            _check(
                "module-version",
                "not_checked",
                "module version cannot be compared without a valid pyproject version",
            )
        )
    else:
        module_version, module_detail = _read_module_version(repo)
        if module_version is None:
            checks.append(_check("module-version", "fail", module_detail))
        elif module_version != version:
            checks.append(
                _check(
                    "module-version",
                    "fail",
                    f"pe_sim version {module_version!r} does not equal pyproject "
                    f"version {version!r} ({module_detail})",
                )
            )
        else:
            checks.append(
                _check(
                    "module-version",
                    "pass",
                    f"pe_sim version {module_version!r} matches pyproject ({module_detail})",
                )
            )

    cli_path = repo / "src" / "pe_sim" / "cli.py"
    if not cli_path.is_file():
        checks.append(
            _check("cli-version-source", "fail", f"CLI module is missing: {cli_path}")
        )
    else:
        try:
            cli_text = cli_path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError) as exc:
            checks.append(
                _check("cli-version-source", "fail", f"cannot read {cli_path}: {exc}")
            )
        else:
            if "__version__" not in cli_text:
                checks.append(
                    _check(
                        "cli-version-source",
                        "fail",
                        f"{cli_path} does not reference __version__",
                    )
                )
            elif version is not None and re.search(
                r"version=[\"']" + re.escape(version) + r"[\"']", cli_text
            ):
                checks.append(
                    _check(
                        "cli-version-source",
                        "fail",
                        f"{cli_path} contains a hard-coded version literal "
                        f"{version!r}; it must read __version__ instead",
                    )
                )
            else:
                checks.append(
                    _check(
                        "cli-version-source",
                        "pass",
                        f"{cli_path} reads __version__ and has no hard-coded "
                        f"{version!r} literal",
                    )
                )

    if version is None:
        checks.append(
            _check(
                "release-notes",
                "not_checked",
                "release note cannot be located without a valid pyproject version",
            )
        )
    else:
        note_path = repo / RELEASE_NOTES_DIRECTORY / f"v{version}.md"
        if not note_path.is_file():
            checks.append(
                _check("release-notes", "fail", f"release note is missing: {note_path}")
            )
        else:
            try:
                note_text = note_path.read_text(encoding="utf-8-sig")
            except (OSError, UnicodeDecodeError) as exc:
                checks.append(
                    _check("release-notes", "fail", f"cannot read {note_path}: {exc}")
                )
            else:
                if version not in note_text:
                    checks.append(
                        _check(
                            "release-notes",
                            "fail",
                            f"release note does not mention version {version!r}: "
                            f"{note_path}",
                        )
                    )
                else:
                    checks.append(
                        _check(
                            "release-notes",
                            "pass",
                            f"release note exists and mentions {version!r}: {note_path}",
                        )
                    )

    tag_name = f"v{version}" if version is not None else "<version>"
    if version is None:
        checks.append(
            _check(
                "tag",
                "not_checked",
                f"tag cannot be checked without a valid pyproject version ({tag_name})",
            )
        )
    elif not require_tag:
        checks.append(
            _check(
                "tag",
                "not_checked",
                f"tag {tag_name} was not required; --require-tag makes an absent "
                "tag fail",
            )
        )
    else:
        result = _run_git(["tag", "--points-at", "HEAD"], cwd=repo)
        if result is None:
            checks.append(
                _check(
                    "tag",
                    "fail",
                    f"tag {tag_name} cannot be verified: the git executable is "
                    "unavailable",
                )
            )
        else:
            code, out, err = result
            if code != 0:
                checks.append(
                    _check(
                        "tag",
                        "fail",
                        f"tag {tag_name} cannot be verified: "
                        f"{err.strip() or 'git tag failed'}",
                    )
                )
            elif tag_name in out.splitlines():
                checks.append(
                    _check("tag", "pass", f"tag {tag_name} points at HEAD")
                )
            else:
                checks.append(
                    _check("tag", "fail", f"tag {tag_name} does not point at HEAD")
                )

    optional_ids = set() if require_tag else {"tag"}
    return {
        "repo_root": str(repo),
        "version": version,
        "status": _overall_status(checks, optional_ids),
        "checks": checks,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _emit(result: dict, as_json: bool, summary_lines: list[str]) -> None:
    if as_json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    else:
        print("\n".join(summary_lines))


def _verify_summary(result: dict) -> list[str]:
    lines = [
        f"Release record: {result['record']}",
        f"Status: {result['status']}",
    ]
    for check in result["checks"]:
        lines.append(f"  [{check['status']}] {check['id']}: {check['detail']}")
    return lines


def _identity_summary(result: dict) -> list[str]:
    lines = [
        f"Repository: {result['repo_root']}",
        f"Version: {result['version']}",
        f"Status: {result['status']}",
    ]
    for check in result["checks"]:
        lines.append(f"  [{check['status']}] {check['id']}: {check['detail']}")
    return lines


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="release_record.py",
        description=(
            "Create, approve, and verify pe-sim release records "
            "(release governance v1)."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    create = subparsers.add_parser(
        "create",
        help="Create a pe-sim.release-record.v1 record from gate evidence and artifacts.",
        description=(
            "Combine local gate evidence, built artifacts, and scope documents "
            "into a pending, unsigned release record. Any failed validation "
            "exits 2 and writes no output file."
        ),
    )
    create.add_argument("--version", required=True, metavar="V", help="release version")
    create.add_argument(
        "--evidence",
        required=True,
        metavar="PATH",
        help="gate evidence directory, or its release-evidence.json",
    )
    create.add_argument("--wheel", required=True, metavar="PATH", help="built wheel")
    create.add_argument("--sdist", required=True, metavar="PATH", help="built sdist")
    create.add_argument(
        "--inventory",
        required=True,
        metavar="PATH",
        help="pe-sim.distribution-inventory.v1 JSON for the wheel",
    )
    create.add_argument(
        "--formal-demo-evidence",
        metavar="PATH",
        help="optional formal demo evidence file recorded as supporting evidence",
    )
    create.add_argument(
        "--upgrade-evidence",
        metavar="PATH",
        help="optional upgrade-gate evidence file recorded as supporting evidence",
    )
    create.add_argument(
        "--notes",
        metavar="PATH",
        help="release note (default: <repo>/docs/releases/v<V>.md)",
    )
    create.add_argument(
        "--tag",
        metavar="TAG",
        help="tag name recorded in the record (default: v<V>)",
    )
    create.add_argument(
        "--commit",
        metavar="SHA",
        help="must equal repository.source_commit in the gate evidence",
    )
    create.add_argument(
        "--timestamp",
        metavar="ISO",
        help="generated_at_utc value (default: current UTC time)",
    )
    create.add_argument(
        "--repo",
        metavar="ROOT",
        help="repository root for scope documents and release notes (default: cwd)",
    )
    create.add_argument(
        "--output", required=True, metavar="PATH", help="record file to write"
    )

    approve = subparsers.add_parser(
        "approve",
        help="Record human approval (or rejection) of a release record.",
        description=(
            "Record one accountable human decision. Refuses to run when CI is "
            "set. Re-approving archives the previous block in approval_history."
        ),
    )
    approve.add_argument("record", metavar="RECORD", help="release record to update")
    approve.add_argument(
        "--reviewer", required=True, metavar="NAME", help="accountable reviewer name"
    )
    approve.add_argument(
        "--statement", required=True, metavar="TEXT", help="review statement (>=10 chars)"
    )
    approve.add_argument(
        "--decision",
        choices=("approve", "reject"),
        default="approve",
        help="decision to record (default: approve)",
    )
    approve.add_argument(
        "--timestamp",
        metavar="ISO",
        help="reviewed_at_utc value (default: current UTC time)",
    )
    approve.add_argument(
        "--json", action="store_true", help="print the machine-readable result"
    )

    verify = subparsers.add_parser(
        "verify",
        help="Recompute record hashes and bindings; exit 0 pass, 1 fail, 2 partial.",
        description=(
            "Recompute every hash, re-derive the record digest, re-check the "
            "approval binding, and optionally verify artifacts and the Git tag."
        ),
    )
    verify.add_argument("record", metavar="RECORD", help="release record to verify")
    verify.add_argument(
        "--dist",
        metavar="DIR",
        help="directory containing the recorded release-artifact filenames",
    )
    verify.add_argument(
        "--evidence-dir",
        metavar="DIR",
        action="append",
        help=(
            "directory to search for recorded supporting-evidence filenames "
            "(gate, formal demo, upgrade); repeatable"
        ),
    )
    verify.add_argument(
        "--require-approved",
        action="store_true",
        help="fail unless approval.status is 'approved'",
    )
    verify.add_argument(
        "--require-tag",
        action="store_true",
        help="require the recorded tag to resolve to the recorded commit",
    )
    verify.add_argument(
        "--repo",
        metavar="ROOT",
        help="repository root for the tag check (default: cwd)",
    )
    verify.add_argument(
        "--json", action="store_true", help="print the machine-readable result"
    )

    identity = subparsers.add_parser(
        "check-identity",
        help="Check that pyproject, pe_sim.__version__, release note, and tag agree.",
        description=(
            "Guard repository-level version drift: pyproject.toml, "
            "pe_sim.__version__, the release note, and the CLI version source."
        ),
    )
    identity.add_argument(
        "--repo",
        metavar="ROOT",
        help="repository root to inspect (default: cwd)",
    )
    identity.add_argument(
        "--require-tag",
        action="store_true",
        help="fail unless v<version> points at HEAD",
    )
    identity.add_argument(
        "--json", action="store_true", help="print the machine-readable result"
    )

    return parser


def main(argv=None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "create":
            record = build_record(
                version=args.version,
                evidence_dir=args.evidence,
                wheel=args.wheel,
                sdist=args.sdist,
                inventory=args.inventory,
                formal_demo_evidence=args.formal_demo_evidence,
                upgrade_evidence=args.upgrade_evidence,
                notes=args.notes,
                tag=args.tag,
                commit=args.commit,
                repo_root=args.repo,
                generated_at_utc=args.timestamp,
            )
            output = Path(args.output)
            _write_json(output, record)
            print(f"Release record written: {output}")
            print(
                f"  version: {record['release']['version']} "
                f"(tag {record['release']['tag']})"
            )
            print(f"  commit: {record['release']['commit']} (gate_evidence)")
            wheel_entry = next(
                entry for entry in record["artifacts"] if entry["kind"] == "wheel"
            )
            print(f"  wheel: {wheel_entry['filename']} sha256={wheel_entry['sha256']}")
            print(f"  record_digest: {record['record_digest']}")
            print("  approval: pending; signature: not_signed")
            return EXIT_PASS

        if args.command == "approve":
            record_path = Path(args.record)
            record = _read_json(record_path)
            if not isinstance(record, Mapping):
                raise RecordError("record must be a JSON object")
            updated = approve_record(
                dict(record),
                reviewer=args.reviewer,
                statement=args.statement,
                decision=args.decision,
                reviewed_at_utc=args.timestamp,
            )
            _write_json(record_path, updated)
            approval = updated["approval"]
            result = {
                "record": str(record_path),
                "approval": approval,
                "record_digest": updated["record_digest"],
            }
            _emit(
                result,
                args.json,
                [
                    f"Release record updated: {record_path}",
                    f"  approval: {approval['status']}",
                    f"  reviewer: {approval['reviewer']}",
                    f"  decision: {approval['decision']}",
                    f"  reviewed_at_utc: {approval['reviewed_at_utc']}",
                    f"  reviewed_record_digest: {approval['reviewed_record_digest']}",
                    f"  record_digest: {updated['record_digest']}",
                ],
            )
            return EXIT_PASS

        if args.command == "verify":
            record_path = Path(args.record)
            try:
                record = _read_json(record_path)
            except RecordError as exc:
                result = {
                    "record": str(record_path),
                    "record_digest": None,
                    "status": "fail",
                    "checks": [_check("load", "fail", str(exc))],
                }
                _emit(result, args.json, _verify_summary(result))
                return EXIT_FAIL
            result = verify_record(
                record,
                record_path=record_path,
                dist_dir=args.dist,
                evidence_dir=args.evidence_dir,
                require_approved=args.require_approved,
                require_tag=args.require_tag,
                repo_root=args.repo,
            )
            _emit(result, args.json, _verify_summary(result))
            return {
                "pass": EXIT_PASS,
                "fail": EXIT_FAIL,
                "partial": EXIT_PARTIAL,
            }[result["status"]]

        if args.command == "check-identity":
            result = check_identity(repo_root=args.repo, require_tag=args.require_tag)
            _emit(result, args.json, _identity_summary(result))
            return {
                "pass": EXIT_PASS,
                "fail": EXIT_FAIL,
                "partial": EXIT_PARTIAL,
            }[result["status"]]

        parser.error(f"unknown command: {args.command}")
        return EXIT_PARTIAL  # pragma: no cover - argparse rejects unknown commands
    except RecordError as exc:
        print(f"release_record: error: {exc}", file=sys.stderr)
        return EXIT_PARTIAL


if __name__ == "__main__":
    raise SystemExit(main())

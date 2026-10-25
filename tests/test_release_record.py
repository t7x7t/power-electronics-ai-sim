"""Tests for ``scripts/release_record.py`` (release governance v1).

Every test builds a synthetic release repository under ``tmp_path``.  Nothing
here reads the developer worktree, the network, ``dist/``, or the repository's
Git state; the only Git interaction is simulated through the module's
``_run_git`` helper.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import zipfile

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "release_record.py"
_SPEC = importlib.util.spec_from_file_location("release_record_under_test", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
release_record = importlib.util.module_from_spec(_SPEC)
sys.modules["release_record_under_test"] = release_record
_SPEC.loader.exec_module(release_record)

VERSION = "9.8.7"
COMMIT = "a" * 40
STATEMENT = "I reviewed the gate evidence, the inventory, and the non-claims."
TIMESTAMP = "2026-10-25T00:00:00Z"


@pytest.fixture(autouse=True)
def _clear_ci(monkeypatch):
    # A real CI environment sets CI=true; every test starts as a local run.
    monkeypatch.delenv("CI", raising=False)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _write_wheel(path: Path, version: str = VERSION) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    dist_info = f"power_electronics_ai_sim-{version}.dist-info"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            f"{dist_info}/METADATA",
            "Metadata-Version: 2.1\n"
            "Name: power-electronics-ai-sim\n"
            f"Version: {version}\n"
            "\n"
            "Synthetic fixture metadata for release-record tests.\n",
        )
        archive.writestr("power_electronics_ai_sim/__init__.py", "")


def _build_fixture(root: Path) -> dict:
    repo = root / "repo"
    (repo / "docs" / "releases").mkdir(parents=True)
    (repo / "src" / "pe_sim").mkdir(parents=True)

    (repo / "pyproject.toml").write_text(
        '[build-system]\n'
        'requires = ["setuptools>=68"]\n'
        'build-backend = "setuptools.build_meta"\n'
        "\n"
        "[project]\n"
        'name = "power-electronics-ai-sim"\n'
        f'version = "{VERSION}"\n'
        'requires-python = ">=3.10"\n',
        encoding="utf-8",
    )
    for relative in (
        release_record.SCOPE_DOCUMENT,
        release_record.TOOLCHAIN_POLICY_DOCUMENT,
        release_record.GOVERNANCE_DOCUMENT,
    ):
        path = repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# Fixture scope document\n\nSynthetic {relative}.\n", encoding="utf-8")
    (repo / "docs" / "releases" / f"v{VERSION}.md").write_text(
        f"# Release {VERSION}\n\nSynthetic fixture release note for {VERSION}.\n",
        encoding="utf-8",
    )
    (repo / "src" / "pe_sim" / "_version.py").write_text(
        f'__version__ = "{VERSION}"\n', encoding="utf-8"
    )
    (repo / "src" / "pe_sim" / "cli.py").write_text(
        "from __future__ import annotations\n"
        "\n"
        "from ._version import __version__\n"
        "\n"
        "\n"
        "def main(argv=None) -> int:\n"
        "    print(__version__)\n"
        "    return 0\n",
        encoding="utf-8",
    )

    evidence_dir = root / "evidence"
    evidence_dir.mkdir()
    evidence_file = evidence_dir / "release-evidence.json"
    _write_json(
        evidence_file,
        {
            "schema_version": "pe-sim.local-release-evidence.v1",
            "outcome": "pass",
            "generated_at_utc": TIMESTAMP,
            "repository": {
                "source_commit": COMMIT,
                "source_commit_after_checks": COMMIT,
                "clean_commit_required": True,
            },
            "gates": [],
            "limitations": ["Synthetic fixture evidence; not a real gate run."],
            "human_review_required": True,
        },
    )
    _write_json(
        evidence_dir / "toolchain-observed.json",
        {
            "expected": {
                "python": "3.12.7",
                "pytest": "7.4.4",
                "jsonschema": "4.23.0",
                "numpy": "1.26.4",
            },
            "observed": {
                "python": "3.12.7",
                "pytest": "7.4.4",
                "jsonschema": "4.23.0",
                "numpy": "1.26.4",
            },
        },
    )

    dist = root / "dist"
    dist.mkdir()
    wheel = dist / f"power_electronics_ai_sim-{VERSION}-py3-none-any.whl"
    _write_wheel(wheel)
    sdist = dist / f"power_electronics_ai_sim-{VERSION}.tar.gz"
    sdist.write_bytes(b"synthetic sdist bytes for release-record tests\n")
    inventory = dist / "distribution-inventory.json"
    _write_json(
        inventory,
        {
            "schema_version": "pe-sim.distribution-inventory.v1",
            "generated_at_utc": None,
            "distribution": {
                "name": "power-electronics-ai-sim",
                "version": VERSION,
                "artifact": {
                    "filename": wheel.name,
                    "sha256": _sha256(wheel),
                    "size_bytes": wheel.stat().st_size,
                },
            },
            "contents": [],
        },
    )

    return {
        "root": root,
        "repo": repo,
        "evidence_dir": evidence_dir,
        "evidence_file": evidence_file,
        "toolchain_file": evidence_dir / "toolchain-observed.json",
        "dist": dist,
        "wheel": wheel,
        "sdist": sdist,
        "inventory": inventory,
        "record": root / "release-record.json",
        "release_note": repo / "docs" / "releases" / f"v{VERSION}.md",
    }


@pytest.fixture()
def fixture(tmp_path):
    return _build_fixture(tmp_path)


def _run_create(fx, *, output=None):
    return release_record.main(
        [
            "create",
            "--version",
            VERSION,
            "--evidence",
            str(fx["evidence_dir"]),
            "--wheel",
            str(fx["wheel"]),
            "--sdist",
            str(fx["sdist"]),
            "--inventory",
            str(fx["inventory"]),
            "--repo",
            str(fx["repo"]),
            "--timestamp",
            TIMESTAMP,
            "--output",
            str(output if output is not None else fx["record"]),
        ]
    )


def _run_approve(fx, *, reviewer="reviewer-a", statement=STATEMENT, decision=None):
    argv = [
        "approve",
        str(fx["record"]),
        "--reviewer",
        reviewer,
        "--statement",
        statement,
        "--timestamp",
        "2026-10-25T01:00:00Z",
    ]
    if decision is not None:
        argv += ["--decision", decision]
    return release_record.main(argv)


def _run_verify(fx, capsys, *extra):
    """Run ``verify`` with ``--evidence-dir`` supplied whenever ``--dist`` is.

    ``verify`` looks for release artifacts under ``--dist`` and for supporting
    evidence (gate, demo, upgrade) under ``--evidence-dir``, so nothing has to
    be copied into the build output directory.
    """

    capsys.readouterr()  # discard output from any earlier create/approve calls
    args = list(extra)
    if "--dist" in args and "--evidence-dir" not in args:
        args += ["--evidence-dir", str(fx["evidence_dir"])]
    code = release_record.main(
        ["verify", str(fx["record"]), *args, "--json"]
    )
    output = capsys.readouterr().out
    result = json.loads(output)
    checks = {check["id"]: check for check in result["checks"]}
    return code, result, checks


def _create_approve_stage(fx) -> None:
    assert _run_create(fx) == 0
    assert _run_approve(fx) == 0


def _assert_create_rejected(fx, capsys, **kwargs):
    output = fx["root"] / "rejected-record.json"
    code = _run_create(fx, output=output, **kwargs)
    assert code == 2
    assert not output.exists()
    return capsys.readouterr().err


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------


def test_create_produces_pending_unsigned_record_with_reproducible_digest(fixture):
    assert _run_create(fixture) == 0

    raw = fixture["record"].read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")
    assert raw.endswith(b"\n")
    record = json.loads(raw.decode("utf-8"))

    assert record["schema_version"] == "pe-sim.release-record.v1"
    assert record["rule_version"] == "release-record-v1"
    assert record["release"]["distribution"] == "power-electronics-ai-sim"
    assert record["release"]["version"] == VERSION
    assert record["release"]["tag"] == f"v{VERSION}"
    assert record["release"]["commit"] == COMMIT
    assert record["release"]["commit_source"] == "gate_evidence"
    assert record["release"]["generated_at_utc"] == TIMESTAMP
    assert record["release"]["release_notes"]["path"] == f"docs/releases/v{VERSION}.md"
    assert record["release"]["release_notes"]["sha256"] == _sha256(fixture["release_note"])

    assert record["scope"]["document"] == release_record.SCOPE_DOCUMENT
    assert record["scope"]["toolchain_policy_document"] == (
        release_record.TOOLCHAIN_POLICY_DOCUMENT
    )
    assert record["scope"]["governance_document"] == (
        release_record.GOVERNANCE_DOCUMENT
    )
    assert record["scope"]["supported_python"] == ">=3.10"
    assert record["scope"]["verified_toolchain"] == {
        "python": "3.12.7",
        "pytest": "7.4.4",
        "jsonschema": "4.23.0",
        "numpy": "1.26.4",
    }

    assert [(item["kind"], item["role"]) for item in record["artifacts"]] == [
        ("wheel", "release_artifact"),
        ("sdist", "release_artifact"),
        ("distribution_inventory", "release_artifact"),
        ("gate_evidence", "supporting_evidence"),
    ]
    assert record["artifacts"][0]["sha256"] == _sha256(fixture["wheel"])

    assert record["approval"] == {
        "status": "pending",
        "reviewer": None,
        "reviewed_at_utc": None,
        "decision": None,
        "statement": None,
        "reviewed_record_digest": None,
    }
    assert record["approval_history"] == []
    assert record["tag"] == {"status": "not_created", "name": f"v{VERSION}", "commit": None}
    assert record["signature"] == {"status": "not_signed", "method": None}

    limitations = record["limitations"]
    assert isinstance(limitations, list) and limitations
    joined = " ".join(limitations).lower()
    assert "accountability" in joined
    assert "no artifact and no tag is signed" in joined
    assert "no simulation" in joined

    assert record["record_digest"] == release_record.record_digest(record)
    # Deterministic UTF-8, sorted-key, two-space JSON with a trailing newline.
    assert raw.decode("utf-8") == (
        json.dumps(record, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    )


def test_create_fails_when_evidence_outcome_is_not_pass(fixture, capsys):
    evidence = _load_json(fixture["evidence_file"])
    evidence["outcome"] = "fail"
    _write_json(fixture["evidence_file"], evidence)

    err = _assert_create_rejected(fixture, capsys)

    assert "outcome" in err
    assert "fail" in err


def test_create_fails_when_commit_is_not_forty_hex(fixture, capsys):
    evidence = _load_json(fixture["evidence_file"])
    evidence["repository"]["source_commit"] = "not-a-commit"
    evidence["repository"]["source_commit_after_checks"] = "not-a-commit"
    _write_json(fixture["evidence_file"], evidence)

    err = _assert_create_rejected(fixture, capsys)

    assert "40-character" in err


def test_create_fails_when_toolchain_observed_is_missing(fixture, capsys):
    fixture["toolchain_file"].unlink()

    err = _assert_create_rejected(fixture, capsys)

    assert "toolchain-observed.json" in err


def test_create_fails_when_inventory_wheel_hash_is_mismatched(fixture, capsys):
    inventory = _load_json(fixture["inventory"])
    inventory["distribution"]["artifact"]["sha256"] = "0" * 64
    _write_json(fixture["inventory"], inventory)

    err = _assert_create_rejected(fixture, capsys)

    assert _sha256(fixture["wheel"]) in err
    assert "0" * 64 in err


def test_create_fails_when_release_note_is_missing(fixture, capsys):
    fixture["release_note"].unlink()

    err = _assert_create_rejected(fixture, capsys)

    assert "release note" in err
    assert f"v{VERSION}.md" in err


def test_create_fails_when_wheel_metadata_version_differs(fixture, capsys):
    _write_wheel(fixture["wheel"], version="9.9.9")

    err = _assert_create_rejected(fixture, capsys)

    assert "METADATA" in err
    assert "9.9.9" in err


def test_create_fails_when_explicit_commit_does_not_match_evidence(fixture, capsys):
    output = fixture["root"] / "rejected-record.json"
    argv = [
        "create",
        "--version",
        VERSION,
        "--evidence",
        str(fixture["evidence_dir"]),
        "--wheel",
        str(fixture["wheel"]),
        "--sdist",
        str(fixture["sdist"]),
        "--inventory",
        str(fixture["inventory"]),
        "--repo",
        str(fixture["repo"]),
        "--commit",
        "b" * 40,
        "--output",
        str(output),
    ]

    assert release_record.main(argv) == 2
    assert not output.exists()
    assert "does not match" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# approve
# ---------------------------------------------------------------------------


def test_approve_refuses_under_ci(fixture, capsys, monkeypatch):
    assert _run_create(fixture) == 0
    monkeypatch.setenv("CI", "true")

    assert _run_approve(fixture) == 2

    err = capsys.readouterr().err
    assert "human local action" in err
    assert _load_json(fixture["record"])["approval"]["status"] == "pending"

    # "0" is explicitly treated as local, not CI.
    monkeypatch.setenv("CI", "0")
    assert _run_approve(fixture) == 0
    assert _load_json(fixture["record"])["approval"]["status"] == "approved"


def test_approve_refuses_a_too_short_statement(fixture, capsys):
    assert _run_create(fixture) == 0

    assert _run_approve(fixture, statement="too short") == 2

    err = capsys.readouterr().err
    assert "10" in err
    assert _load_json(fixture["record"])["approval"]["status"] == "pending"


def test_approve_binds_the_reviewed_body_digest_without_changing_it(fixture):
    assert _run_create(fixture) == 0
    before = _load_json(fixture["record"])

    assert _run_approve(fixture) == 0
    after = _load_json(fixture["record"])

    approval = after["approval"]
    assert approval["status"] == "approved"
    assert approval["decision"] == "approve"
    assert approval["reviewer"] == "reviewer-a"
    assert approval["statement"] == STATEMENT
    assert approval["reviewed_at_utc"] == "2026-10-25T01:00:00Z"
    assert approval["reviewed_record_digest"] == before["record_digest"]
    assert after["record_digest"] == before["record_digest"]
    assert after["approval_history"] == []
    assert release_record.record_digest(after) == after["record_digest"]


def test_reapproval_archives_the_previous_block(fixture):
    assert _run_create(fixture) == 0
    assert _run_approve(fixture, reviewer="reviewer-a") == 0

    assert _run_approve(fixture, reviewer="reviewer-b") == 0
    record = _load_json(fixture["record"])

    assert len(record["approval_history"]) == 1
    assert record["approval_history"][0]["reviewer"] == "reviewer-a"
    assert record["approval_history"][0]["status"] == "approved"
    assert record["approval"]["reviewer"] == "reviewer-b"
    assert record["approval"]["reviewed_record_digest"] == record["record_digest"]
    assert release_record.record_digest(record) == record["record_digest"]


def test_reject_decision_sets_rejected_status(fixture):
    assert _run_create(fixture) == 0

    assert _run_approve(fixture, decision="reject") == 0
    approval = _load_json(fixture["record"])["approval"]

    assert approval["status"] == "rejected"
    assert approval["decision"] == "reject"


# ---------------------------------------------------------------------------
# verify
# ---------------------------------------------------------------------------


def test_verify_passes_for_an_intact_approved_record(fixture, capsys):
    _create_approve_stage(fixture)

    code, result, checks = _run_verify(
        fixture,
        capsys,
        "--dist",
        str(fixture["dist"]),
        "--require-approved",
        "--repo",
        str(fixture["repo"]),
    )

    assert code == 0
    assert result["status"] == "pass"
    assert checks["schema"]["status"] == "pass"
    assert checks["record-digest"]["status"] == "pass"
    assert checks["approval-binding"]["status"] == "pass"
    assert checks["approval-required"]["status"] == "pass"
    assert checks["artifacts-hashed"]["status"] == "pass"
    assert checks["inventory-binding"]["status"] == "pass"
    assert checks["signature-status"]["status"] == "pass"
    assert "not_signed" in checks["signature-status"]["detail"]
    assert checks["limitations"]["status"] == "pass"
    # The tag was not requested, so it is reported but never treated as a
    # blocking gap.
    assert checks["tag"]["status"] == "not_checked"
    # Release artifacts come from --dist, supporting evidence from
    # --evidence-dir; both were checked.
    assert "4 artifact(s) matched" in checks["artifacts-hashed"]["detail"]


def test_verify_without_an_evidence_directory_reports_the_skipped_role(
    fixture, capsys
):
    _create_approve_stage(fixture)

    capsys.readouterr()
    code = release_record.main(
        [
            "verify",
            str(fixture["record"]),
            "--dist",
            str(fixture["dist"]),
            "--require-approved",
            "--json",
        ]
    )
    result = json.loads(capsys.readouterr().out)
    checks = {check["id"]: check for check in result["checks"]}

    # The three release artifacts still match, so the record passes, but the
    # detail must say that the gate evidence was not compared.
    assert code == 0
    assert result["status"] == "pass"
    assert checks["artifacts-hashed"]["status"] == "not_checked"
    assert "3 artifact(s) matched" in checks["artifacts-hashed"]["detail"]
    assert "release-evidence.json" in checks["artifacts-hashed"]["detail"]


def test_verify_searches_every_supplied_evidence_directory(fixture, capsys):
    _create_approve_stage(fixture)
    elsewhere = fixture["root"] / "elsewhere"
    elsewhere.mkdir()
    empty = fixture["root"] / "empty"
    empty.mkdir()
    source = fixture["evidence_file"]
    target = elsewhere / source.name
    target.write_bytes(source.read_bytes())
    source.unlink()

    capsys.readouterr()
    code = release_record.main(
        [
            "verify",
            str(fixture["record"]),
            "--dist",
            str(fixture["dist"]),
            "--evidence-dir",
            str(empty),
            "--evidence-dir",
            str(elsewhere),
            "--require-approved",
            "--json",
        ]
    )
    result = json.loads(capsys.readouterr().out)
    checks = {check["id"]: check for check in result["checks"]}

    # The gate evidence was recorded in a different directory from the build
    # output; every supplied --evidence-dir is searched by filename.
    assert code == 0
    assert result["status"] == "pass"
    assert checks["artifacts-hashed"]["status"] == "pass"


def test_verify_fails_when_supporting_evidence_changes(fixture, capsys):
    _create_approve_stage(fixture)
    fixture["evidence_file"].write_bytes(
        fixture["evidence_file"].read_bytes() + b"\n"
    )

    code, result, checks = _run_verify(
        fixture,
        capsys,
        "--dist",
        str(fixture["dist"]),
        "--require-approved",
    )

    assert code == 1
    assert result["status"] == "fail"
    assert checks["artifacts-hashed"]["status"] == "fail"
    assert "release-evidence.json" in checks["artifacts-hashed"]["detail"]


def test_verify_fails_when_wheel_bytes_change(fixture, capsys):
    _create_approve_stage(fixture)
    fixture["wheel"].write_bytes(fixture["wheel"].read_bytes() + b"tampered")

    code, result, checks = _run_verify(
        fixture,
        capsys,
        "--dist",
        str(fixture["dist"]),
        "--require-approved",
    )

    assert code == 1
    assert result["status"] == "fail"
    assert checks["artifacts-hashed"]["status"] == "fail"
    assert fixture["wheel"].name in checks["artifacts-hashed"]["detail"]


def test_verify_fails_when_inventory_wheel_hash_changes(fixture, capsys):
    _create_approve_stage(fixture)
    inventory = _load_json(fixture["inventory"])
    inventory["distribution"]["artifact"]["sha256"] = "0" * 64
    _write_json(fixture["inventory"], inventory)

    code, result, checks = _run_verify(
        fixture,
        capsys,
        "--dist",
        str(fixture["dist"]),
        "--require-approved",
    )

    assert code == 1
    assert result["status"] == "fail"
    assert checks["inventory-binding"]["status"] == "fail"
    assert "0" * 64 in checks["inventory-binding"]["detail"]


def test_verify_fails_when_approval_is_required_but_pending(fixture, capsys):
    assert _run_create(fixture) == 0

    code, result, checks = _run_verify(
        fixture,
        capsys,
        "--dist",
        str(fixture["dist"]),
        "--require-approved",
    )

    assert code == 1
    assert result["status"] == "fail"
    assert checks["approval-required"]["status"] == "fail"


def test_verify_fails_when_record_digest_is_edited(fixture, capsys):
    assert _run_create(fixture) == 0
    record = _load_json(fixture["record"])
    record["record_digest"] = "f" * 64
    _write_json(fixture["record"], record)

    code, result, checks = _run_verify(
        fixture, capsys, "--dist", str(fixture["dist"])
    )

    assert code == 1
    assert result["status"] == "fail"
    assert checks["record-digest"]["status"] == "fail"


def test_verify_require_tag_outside_a_git_repository_is_partial(
    fixture, capsys, monkeypatch
):
    _create_approve_stage(fixture)

    def fake_git(args, *, cwd):
        return (
            128,
            "",
            "fatal: not a git repository (or any of the parent directories): .git\n",
        )

    monkeypatch.setattr(release_record, "_run_git", fake_git)

    code, result, checks = _run_verify(
        fixture,
        capsys,
        "--dist",
        str(fixture["dist"]),
        "--require-approved",
        "--require-tag",
        "--repo",
        str(fixture["repo"]),
    )

    assert code == 2
    assert result["status"] == "partial"
    assert checks["tag"]["status"] == "not_checked"
    assert f"v{VERSION}" in checks["tag"]["detail"]


# ---------------------------------------------------------------------------
# check-identity
# ---------------------------------------------------------------------------


def _check_identity(fx, capsys, *extra):
    capsys.readouterr()  # discard output from any earlier create/approve calls
    code = release_record.main(
        ["check-identity", "--repo", str(fx["repo"]), "--json", *extra]
    )
    result = json.loads(capsys.readouterr().out)
    checks = {check["id"]: check for check in result["checks"]}
    return code, result, checks


def test_check_identity_passes_on_the_synthetic_repository(fixture, capsys):
    code, result, checks = _check_identity(fixture, capsys)

    assert code == 0
    assert result["status"] == "pass"
    assert result["version"] == VERSION
    assert checks["pyproject-version"]["status"] == "pass"
    assert checks["module-version"]["status"] == "pass"
    assert checks["cli-version-source"]["status"] == "pass"
    assert checks["release-notes"]["status"] == "pass"
    assert checks["tag"]["status"] == "not_checked"
    assert f"v{VERSION}" in checks["tag"]["detail"]


def test_check_identity_fails_on_a_module_version_mismatch(fixture, capsys):
    (fixture["repo"] / "src" / "pe_sim" / "_version.py").write_text(
        '__version__ = "9.8.6"\n', encoding="utf-8"
    )

    code, result, checks = _check_identity(fixture, capsys)

    assert code == 1
    assert result["status"] == "fail"
    assert checks["pyproject-version"]["status"] == "pass"
    assert checks["module-version"]["status"] == "fail"
    assert "9.8.6" in checks["module-version"]["detail"]


def test_check_identity_fails_when_the_release_note_is_missing(fixture, capsys):
    fixture["release_note"].unlink()

    code, result, checks = _check_identity(fixture, capsys)

    assert code == 1
    assert result["status"] == "fail"
    assert checks["release-notes"]["status"] == "fail"


def test_check_identity_fails_when_cli_regains_a_hard_coded_version(fixture, capsys):
    (fixture["repo"] / "src" / "pe_sim" / "cli.py").write_text(
        "from ._version import __version__\n"
        "\n"
        "\n"
        'DEFAULT = version="9.8.7"\n',
        encoding="utf-8",
    )

    code, result, checks = _check_identity(fixture, capsys)

    assert code == 1
    assert result["status"] == "fail"
    assert checks["cli-version-source"]["status"] == "fail"
    assert "9.8.7" in checks["cli-version-source"]["detail"]


# ---------------------------------------------------------------------------
# missing git
# ---------------------------------------------------------------------------


def test_missing_git_never_raises_and_never_passes(fixture, capsys, monkeypatch):
    _create_approve_stage(fixture)
    monkeypatch.setattr(release_record, "_run_git", lambda args, *, cwd: None)

    code, result, checks = _run_verify(
        fixture,
        capsys,
        "--dist",
        str(fixture["dist"]),
        "--require-approved",
        "--require-tag",
        "--repo",
        str(fixture["repo"]),
    )

    assert code == 1
    assert result["status"] == "fail"
    assert checks["tag"]["status"] == "fail"
    assert "git" in checks["tag"]["detail"]

    code, result, checks = _check_identity(fixture, capsys, "--require-tag")

    assert code == 1
    assert result["status"] == "fail"
    assert checks["tag"]["status"] == "fail"
    assert "git" in checks["tag"]["detail"]

"""The shipped manifest schema must accept every value the runtime can write.

`evidence_level` drifted once already: the runtime started writing
`diagnostic_only` / `limited` / `comparable` while the schema still declared the
older Stage 12 vocabulary, so `scripts/user_acceptance.py` would have rejected a
legitimate formal-comparison package.  These tests derive the vocabulary from
the implementation instead of restating it.
"""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

from pe_sim.runtime import _derive_evidence_level

SCHEMA_PATH = Path(__file__).resolve().parents[1] / "schemas" / "manifest.schema.json"


def _schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


class _Worktree:
    def __init__(self, status: str) -> None:
        self.status = status


def _environment(status: str = "known") -> dict:
    return {
        "status": status,
        "backends": [{"status": "known"}] if status == "known" else [],
    }


def _reviewed_plant(verified: bool = True) -> dict:
    plant = {
        "identity": {
            "implementation": {
                "sha256": "a" * 64,
                "review": {"status": "reviewed", "backend_id": "buck"},
            }
        }
    }
    if verified:
        plant["verification"] = {"status": "verified", "backend_id": "buck"}
    return plant


def _producible_levels() -> dict[str, str]:
    """One concrete input per level ``_derive_evidence_level`` can return."""

    return {
        "diagnostic_only": _derive_evidence_level(
            status="RUN_FAILED",
            mode="exploratory",
            worktree=_Worktree("clean"),
            environment=_environment(),
            plant=_reviewed_plant(),
            qualification={"level": "full"},
        ),
        "functional": _derive_evidence_level(
            status="RUN_OK",
            mode="exploratory",
            worktree=_Worktree("clean"),
            environment=_environment(),
            plant=_reviewed_plant(),
            qualification={"level": "full"},
        ),
        "limited": _derive_evidence_level(
            status="RUN_OK",
            mode="formal_comparison",
            worktree=_Worktree("dirty"),
            environment=_environment(),
            plant=_reviewed_plant(),
            qualification={"level": "full"},
        ),
        "comparable": _derive_evidence_level(
            status="RUN_OK",
            mode="formal_comparison",
            worktree=_Worktree("clean"),
            environment=_environment(),
            plant=_reviewed_plant(),
            qualification={"level": "full"},
        ),
    }


def test_the_four_expected_levels_are_reachable():
    levels = _producible_levels()

    mismatched = {name: produced for name, produced in levels.items() if produced != name}
    assert not mismatched, f"scenario produced an unexpected level: {mismatched}"


@pytest.mark.parametrize("level", sorted(_producible_levels()))
def test_schema_accepts_every_runtime_evidence_level(level):
    schema = _schema()
    validator = jsonschema.Draft202012Validator(schema["properties"]["evidence_level"])

    validator.validate(_producible_levels()[level])


def test_schema_enum_matches_the_runtime_vocabulary():
    """Adding a level without updating the schema (or vice versa) fails here."""

    declared = set(_schema()["properties"]["evidence_level"]["enum"])
    produced = set(_producible_levels().values())

    assert produced <= declared, f"runtime emits levels the schema rejects: {sorted(produced - declared)}"
    assert declared == produced, (
        "the schema declares levels the runtime never emits: "
        f"{sorted(declared - produced)}"
    )


def test_plant_verification_schema_accepts_every_status():
    schema = _schema()
    validator = jsonschema.Draft202012Validator(schema["properties"]["plant"]["properties"]["verification"])

    for status in (
        "verified",
        "digest_mismatch",
        "review_status_mismatch",
        "module_outside_solve_path",
        "unverifiable",
    ):
        validator.validate({"schema_version": "pe-sim.adapter-verification.v1", "status": status, "backend_id": "buck"})

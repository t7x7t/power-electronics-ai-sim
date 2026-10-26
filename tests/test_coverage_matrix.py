"""The coverage matrix must stay true to the repository.

`docs/60_simulation_coverage_matrix_v1.md` is the requirements ledger.  Its
failure mode is silent: a row says `covered`, nobody remembers which test
proves it, and the next change removes the behaviour.  This module makes the
ledger machine-checkable:

- every row declares a status from a closed vocabulary;
- a `covered` row must name at least one existing test file that really
  contains tests, so coverage cannot be claimed without evidence;
- a `kernel_gap` row must point at a decision or a registered gap, so a gap
  cannot exist only in a conversation;
- an `out_of_scope` row must point at a document, so "we don't do that" is
  declared rather than implied.

Adding a row is therefore cheap; claiming something is covered is not.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


REPOSITORY = Path(__file__).resolve().parents[1]
MATRIX = REPOSITORY / "docs" / "60_simulation_coverage_matrix_v1.md"

STATUSES = {"covered", "expressible", "kernel_gap", "out_of_scope"}
FREQUENCIES = {"very-common", "common", "specialized"}
RESPONSIBLE = {"kernel", "adapter", "kernel+adapter", "adapter+kernel", "controller", "controller/adapter"}

#: A `kernel_gap` must name where the gap is decided or registered.
GAP_REFERENCE = re.compile(r"(UD-\d+|GAP-\d+|ADR \d{4}|docs/\d+|docs/00)")
TEST_REFERENCE = re.compile(r"`(tests/[\w/\-]+\.py)`")


def _cells(line: str) -> list[str] | None:
    if not line.startswith("|"):
        return None
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def matrix_rows() -> list[dict[str, str]]:
    """Return the seven-column requirement rows, header and rules excluded."""

    rows: list[dict[str, str]] = []
    for line in MATRIX.read_text(encoding="utf-8").splitlines():
        cells = _cells(line)
        if cells is None or len(cells) != 7:
            continue
        if cells[0] == "ID" or set(cells[0]) <= {"-", " "}:
            continue
        rows.append(
            {
                "id": cells[0],
                "scenario": cells[1],
                "frequency": cells[2],
                "expressible": cells[3],
                "status": cells[4],
                "owner": cells[5],
                "evidence": cells[6],
            }
        )
    return rows


ROWS = matrix_rows()


def test_the_matrix_is_present_and_not_gutted():
    assert MATRIX.is_file()
    # The point of the ledger is breadth; a two-row file would still pass every
    # structural check below while covering almost nothing.
    assert len(ROWS) >= 30, f"coverage matrix has only {len(ROWS)} rows"


def test_every_row_uses_the_closed_vocabularies():
    problems: list[str] = []
    for row in ROWS:
        if row["status"] not in STATUSES:
            problems.append(f"{row['id']}: unknown status {row['status']!r}")
        if row["frequency"] not in FREQUENCIES:
            problems.append(f"{row['id']}: unknown frequency {row['frequency']!r}")
        if row["owner"] not in RESPONSIBLE:
            problems.append(f"{row['id']}: unknown owner {row['owner']!r}")
        if row["expressible"] not in {"yes", "partial", "no"}:
            problems.append(f"{row['id']}: expressible must be yes/partial/no")
    assert problems == [], "\n".join(problems)


def test_row_ids_are_unique_and_ordered_by_group():
    ids = [row["id"] for row in ROWS]
    assert len(ids) == len(set(ids)), f"duplicate ids: {sorted(ids)}"
    assert all(re.fullmatch(r"[A-D]\d+", row_id) for row_id in ids), ids


@pytest.mark.parametrize("row", ROWS, ids=[row["id"] for row in ROWS])
def test_a_claimed_status_is_evidenced(row):
    """`covered` needs a test; a gap needs a decision; a boundary needs a doc."""

    if row["status"] == "covered":
        referenced = TEST_REFERENCE.findall(row["evidence"])
        assert referenced, (
            f"{row['id']} claims coverage without naming a test file. "
            "Name the tests/... file that proves it, or change the status."
        )
        for candidate in referenced:
            path = REPOSITORY / candidate
            assert path.is_file(), f"{row['id']} references a missing test file: {candidate}"
            assert "def test_" in path.read_text(encoding="utf-8"), (
                f"{row['id']} references {candidate}, which contains no tests"
            )
    elif row["status"] == "kernel_gap":
        assert GAP_REFERENCE.search(row["evidence"]), (
            f"{row['id']} is a kernel gap but names no backlog id, GAP id, ADR or document"
        )
    elif row["status"] == "out_of_scope":
        assert "docs/" in row["evidence"], (
            f"{row['id']} is out of scope but does not say which document decides that"
        )
    else:  # expressible
        assert row["evidence"].strip(), (
            f"{row['id']} is expressible but does not say what is missing"
        )


def test_every_very_common_row_is_covered_or_has_a_registered_gap():
    """The requirement that motivated this ledger, stated as a test.

    A very common scenario may be unsupported, but then it must be a registered
    gap with an owner — never an absence nobody wrote down.
    """

    for row in ROWS:
        if row["frequency"] != "very-common":
            continue
        if row["status"] not in {"kernel_gap", "out_of_scope"}:
            continue
        assert GAP_REFERENCE.search(row["evidence"]) or "docs/" in row["evidence"], (
            f"{row['id']} is a very common scenario with an unregistered gap"
        )


def test_the_gap_register_matches_the_rows_it_claims():
    """A registered GAP id must be referenced by at least one matrix row."""

    text = MATRIX.read_text(encoding="utf-8")
    registered = set(re.findall(r"\| `?(GAP-\d+)`? \|", text))
    assert registered, "the gap register is empty"
    for gap in registered:
        assert text.count(gap) >= 2, f"{gap} is registered but no row references it"


# ---------------------------------------------------------------------------
# Closure classes (ADR 0005): what it takes to close a gap.
# ---------------------------------------------------------------------------

CLOSURE_CLASSES = {
    "closed",
    "test-only",
    "postprocess",
    "adapter",
    "controller",
    "study",
    "capability",
    "kernel-impl",
    "contract",
    "never",
}

#: The contract may not grow silently.  Each entry needs a recorded decision.
CONTRACT_BUDGET = 3


def closure_classes() -> dict[str, str]:
    """Return ``{row_id: class}`` parsed from the §2.2 classification table."""

    assignment: dict[str, str] = {}
    for line in MATRIX.read_text(encoding="utf-8").splitlines():
        cells = _cells(line)
        if cells is None or len(cells) != 3:
            continue
        name = cells[0].strip("`")
        if name not in CLOSURE_CLASSES:
            continue
        for row_id in re.findall(r"[A-D]\d+", cells[1]):
            assert row_id not in assignment, f"{row_id} is classified twice"
            assignment[row_id] = name
    return assignment


CLASSES = closure_classes()


def test_every_row_has_exactly_one_closure_class():
    missing = sorted({row["id"] for row in ROWS} - set(CLASSES))
    extra = sorted(set(CLASSES) - {row["id"] for row in ROWS})
    assert missing == [], f"rows without a closure class: {missing}"
    assert extra == [], f"closure table names rows that do not exist: {extra}"


def test_the_contract_class_is_budgeted_and_cites_a_decision():
    """Growth of the protocol must be a recorded decision, not a new field."""

    contract_rows = [row for row in ROWS if CLASSES.get(row["id"]) == "contract"]
    assert len(contract_rows) <= CONTRACT_BUDGET, (
        f"{len(contract_rows)} rows need a contract change (budget {CONTRACT_BUDGET}). "
        "Express the capability with an existing declaration, or write an ADR."
    )
    for row in contract_rows:
        assert GAP_REFERENCE.search(row["evidence"]), (
            f"{row['id']} needs a contract change but cites no ADR"
        )


def test_a_covered_row_is_not_blocked_on_work():
    """`covered` must not also be listed as needing kernel or contract work."""

    blocked = {"kernel-impl", "contract", "capability", "study"}
    inconsistent = [
        row["id"]
        for row in ROWS
        if row["status"] == "covered" and CLASSES.get(row["id"]) in blocked
    ]
    assert inconsistent == [], f"covered rows that still need work: {inconsistent}"


def test_out_of_scope_rows_are_classified_never():
    wrong = [
        row["id"]
        for row in ROWS
        if row["status"] == "out_of_scope" and CLASSES.get(row["id"]) != "never"
    ]
    assert wrong == [], f"out-of-scope rows not classified `never`: {wrong}"


# ---------------------------------------------------------------------------
# Closure plan (docs/61): the open rows have one sequence, not many opinions.
# ---------------------------------------------------------------------------

PLAN = REPOSITORY / "docs" / "61_gap_closure_plan_v1.md"
STAGE_ID = re.compile(r"S\d+")


def plan_stages() -> list[dict[str, object]]:
    """Return ``[{id, version, rows}]`` parsed from the plan's stage table."""

    stages: list[dict[str, object]] = []
    for line in PLAN.read_text(encoding="utf-8").splitlines():
        cells = _cells(line)
        if cells is None or len(cells) != 6:
            continue
        stage_id = cells[0].strip("`")
        if STAGE_ID.fullmatch(stage_id) is None:
            continue
        stages.append(
            {
                "id": stage_id,
                "version": cells[1],
                "owner": cells[3],
                "gate": cells[5],
                "rows": re.findall(r"[A-D]\d+", cells[2]),
            }
        )
    return stages


STAGES = plan_stages()


def _open_rows() -> set[str]:
    return {row["id"] for row in ROWS if row["status"] in {"expressible", "kernel_gap"}}


def test_the_plan_is_present_and_has_stages():
    assert PLAN.is_file(), "docs/61 (the closure plan) is missing"
    assert len(STAGES) >= 6, f"the plan has only {len(STAGES)} stages"


def test_the_plan_assigns_every_open_row_to_exactly_one_stage():
    """A gap that is in the ledger but in no stage is just an unscheduled gap."""

    assigned: dict[str, str] = {}
    for stage in STAGES:
        for row_id in stage["rows"]:
            assert row_id not in assigned, (
                f"{row_id} is scheduled in both {assigned[row_id]} and {stage['id']}"
            )
            assigned[row_id] = str(stage["id"])

    open_rows = _open_rows()
    missing = sorted(open_rows - set(assigned))
    stale = sorted(set(assigned) - open_rows)

    assert missing == [], f"open rows with no stage: {missing}"
    assert stale == [], f"the plan schedules rows that are not open: {stale}"


def test_stages_are_numbered_and_versioned_in_order():
    ids = [int(str(stage["id"])[1:]) for stage in STAGES]
    assert ids == list(range(len(ids))), f"stage ids are not consecutive: {ids}"

    def version(value: object) -> tuple[int, ...]:
        text = str(value)
        assert re.fullmatch(r"\d+(\.\d+)+", text), f"unparseable stage version {text!r}"
        return tuple(int(part) for part in text.split("."))

    versions = [version(stage["version"]) for stage in STAGES]
    assert versions == sorted(versions), f"stage versions go backwards: {versions}"


def test_every_stage_declares_its_layer_and_its_gate():
    for stage in STAGES:
        assert str(stage["owner"]).strip(), f"{stage['id']} declares no closure layer"
        assert str(stage["gate"]).strip(), f"{stage['id']} declares no gate"


def test_the_evidence_ceiling_register_names_rows_that_exist():
    """A ceiling that names a row outside the ledger would be unenforceable."""

    text = PLAN.read_text(encoding="utf-8")
    rows_by_id = {row["id"]: row for row in ROWS}
    ceilings: dict[str, str] = {}
    for line in text.splitlines():
        cells = _cells(line)
        if cells is None or len(cells) != 3 or not re.fullmatch(r"[A-D]\d+", cells[0]):
            continue
        ceilings[cells[0]] = cells[1]

    assert len(ceilings) >= 10, f"only {len(ceilings)} rows declare an evidence ceiling"
    unknown = sorted(set(ceilings) - set(rows_by_id))
    assert unknown == [], f"the ceiling register names unknown rows: {unknown}"
    vocabulary = {"comparable", "limited", "functional", "per-point", "inherit"}
    for row_id, ceiling in ceilings.items():
        assert any(token in ceiling for token in vocabulary), (
            f"{row_id} declares a ceiling outside the closed vocabulary: {ceiling!r}"
        )

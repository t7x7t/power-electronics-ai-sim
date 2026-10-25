"""Detached parameter confirmations bound to one recorded parameter set.

A confirmation records that one named reviewer saw the parameter values of one
published run package.  It is deliberately a sibling artifact written *outside*
the package: a run package is immutable after publication, and the parameter
view is a projection, not a mutation.

The record is accountability, not approval.  It does not approve a result, a
release, or a downstream use; it does not change the recorded evidence or
qualification level; it does not change the claim conclusion; and it is not
signed or tamper-proof.  Its only value is that it binds one reviewer's
statement to one package identity and one parameter-set digest.

Verification compares the confirmation with the current package:

``current``
    every check passed; the confirmation describes this package.
``stale``
    the same run identity, but the recorded parameter set differs.
``mismatched_run``
    the confirmation belongs to a different package.
``invalid``
    the schema or the digest is broken, or required fields are missing.
``unknown``
    the package could not be inspected, so no verdict is possible.

Run identity is the identity the package *records* in ``manifest.json``
(``manifest_sha256`` / ``package_sha256``).  That is what makes ``stale``
reachable: a parameter edited without re-deriving the recorded hashes is the
same run with a different parameter set, which is exactly the case a reader
must be warned about.  ``inspect-run`` remains the surface that reports whether
those recorded hashes match the package contents.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from typing import Any

from .jsonutil import stable_sha256
from .parameter_view import (
    ParameterViewError,
    build_parameter_view,
    parameter_rows,
    parameter_set_digest,
)


CONFIRMATION_SCHEMA = "pe-sim.parameter-confirmation.v1"
CONFIRMATION_RULE_VERSION = "parameter-confirmation-v1"
VERIFICATION_SCHEMA = "pe-sim.parameter-confirmation-verification.v1"

_PASS = "pass"
_FAIL = "fail"

#: A confirmation is a human local action.  Only an unset/empty value, ``0``,
#: or ``false`` means "this is not an automated CI run".
_CI_ACCEPTED_VALUES = frozenset({"", "0", "false"})

_MINIMUM_STATEMENT_LENGTH = 10
_UTC_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

CONFIRMATION_LIMITATIONS = (
    "A confirmation is not an approval of a result, a release, or a"
    " downstream use; it records only that one reviewer saw these parameter"
    " values.",
    "A confirmation does not change the recorded evidence level or the"
    " qualification level, and it does not change the claim conclusion.",
    "A confirmation is not signed and not tamper-proof; anyone can write the"
    " same JSON, so its value is only the binding it records.",
)

VERIFICATION_LIMITATIONS = (
    "Verification compares recorded values with the current package; it does"
    " not re-execute a model or re-derive a physical quantity.",
    "A current verdict means the confirmation describes this package's"
    " recorded parameters; it is not an approval of a result, a release, or a"
    " downstream use.",
    "A confirmation is not signed; verification detects an edited body but"
    " cannot prove who wrote the file.",
)

class ParameterConfirmationError(ValueError):
    """Raised when a confirmation cannot be produced, written, or read safely."""


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _non_empty_text(value: Any) -> bool:
    return bool(_text(value))


def _check(check_id: str, status: str, detail: str) -> dict[str, str]:
    return {"id": check_id, "status": status, "detail": detail}


def _format_utc(value: Any) -> str:
    """Normalise a timestamp to ``YYYY-MM-DDTHH:MM:SSZ``.

    ``None`` defaults to the current UTC time.  ``Z`` is accepted, a naive
    value is read as UTC, and an aware value is converted to UTC.  The explicit
    override exists so a test can pin the recorded time reproducibly.
    """

    if value is None:
        return datetime.now(timezone.utc).strftime(_UTC_FORMAT)
    text = value.strip() if isinstance(value, str) else ""
    if not text:
        raise ParameterConfirmationError(
            "confirmed_at_utc must be a non-empty ISO 8601 timestamp"
        )
    normalized = text[:-1] + "+00:00" if text[-1] in {"Z", "z"} else text
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ParameterConfirmationError(
            f"confirmed_at_utc is not ISO 8601: {value!r}"
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).strftime(_UTC_FORMAT)


def _refuse_in_ci() -> None:
    ci_value = os.environ.get("CI")
    if ci_value is not None and ci_value.strip().lower() not in _CI_ACCEPTED_VALUES:
        raise ParameterConfirmationError(
            "refusing to record a parameter confirmation when CI is set: a"
            " confirmation is a human local action, not an automated step"
            f" (CI={ci_value!r})"
        )


def _build_view(run_dir: Any) -> Mapping[str, Any]:
    try:
        view = build_parameter_view(run_dir)
    except ParameterViewError as exc:
        raise ParameterConfirmationError(
            f"parameter view is unavailable for {run_dir}: {exc}"
        ) from exc
    if not isinstance(view, Mapping):
        raise ParameterConfirmationError("parameter view must be a JSON object")
    return view


def _view_run_id(view: Mapping[str, Any]) -> str | None:
    run = view.get("run")
    if not isinstance(run, Mapping):
        return None
    value = run.get("run_id")
    return value.strip() if isinstance(value, str) and value.strip() else None


def _view_package_identity(view: Mapping[str, Any]) -> tuple[str, str]:
    package = view.get("package")
    if isinstance(package, Mapping):
        manifest_sha256 = _text(package.get("manifest_sha256"))
        package_sha256 = _text(package.get("package_sha256"))
        if manifest_sha256 and package_sha256:
            return manifest_sha256, package_sha256
    raise ParameterConfirmationError(
        "parameter view does not record the package identity hashes"
    )


def _view_parameter_set(view: Mapping[str, Any]) -> Mapping[str, Any]:
    parameter_set = view.get("parameter_set")
    if not isinstance(parameter_set, Mapping):
        raise ParameterConfirmationError("parameter view has no parameter_set block")
    return parameter_set


def _group_ids(view: Mapping[str, Any]) -> list[str]:
    parameter_set = _view_parameter_set(view)
    groups = parameter_set.get("groups")
    if not isinstance(groups, Sequence) or isinstance(groups, (str, bytes)):
        raise ParameterConfirmationError(
            "parameter view parameter_set.groups must be a list"
        )
    identifiers: list[str] = []
    for group in groups:
        if not isinstance(group, Mapping):
            raise ParameterConfirmationError(
                "every parameter view group must be a JSON object"
            )
        identifier = _text(group.get("id"))
        if not identifier:
            raise ParameterConfirmationError(
                "every parameter view group must have a non-empty id"
            )
        if identifier not in identifiers:
            identifiers.append(identifier)
    return identifiers


def _view_claim_conclusion(view: Mapping[str, Any]) -> str:
    claim = view.get("claim")
    conclusion = claim.get("conclusion") if isinstance(claim, Mapping) else None
    if not isinstance(conclusion, str) or not conclusion.strip():
        raise ParameterConfirmationError(
            "parameter view does not record a claim conclusion"
        )
    return conclusion.strip()


def _view_limitations(view: Mapping[str, Any]) -> list[str]:
    limitations = view.get("limitations")
    if not isinstance(limitations, Sequence) or isinstance(limitations, (str, bytes)):
        return []
    return [item for item in limitations if isinstance(item, str)]


def _parameter_row_list(view: Mapping[str, Any]) -> list[dict[str, Any]]:
    try:
        rows = parameter_rows(view)
    except ParameterViewError as exc:
        raise ParameterConfirmationError(
            f"parameter rows are unavailable: {exc}"
        ) from exc
    result: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            raise ParameterConfirmationError("parameter rows must be JSON objects")
        result.append(dict(row))
    return result


def _bound_list(view: Mapping[str, Any]) -> list[dict[str, Any]]:
    bounds = _view_parameter_set(view).get("bounds")
    if bounds is None:
        return []
    if not isinstance(bounds, Sequence) or isinstance(bounds, (str, bytes)):
        raise ParameterConfirmationError(
            "parameter view parameter_set.bounds must be a list"
        )
    result: list[dict[str, Any]] = []
    for bound in bounds:
        if not isinstance(bound, Mapping):
            raise ParameterConfirmationError("every parameter bound must be a JSON object")
        result.append(dict(bound))
    return result


def _acknowledged_limitations(value: Any) -> list[str] | None:
    if value is None:
        return None
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ParameterConfirmationError(
            "acknowledged_limitations must be a sequence of non-empty strings"
        )
    result: list[str] = []
    for item in value:
        text = _text(item)
        if not text:
            raise ParameterConfirmationError(
                "acknowledged_limitations must contain only non-empty strings"
            )
        result.append(text)
    return result


def _scope(view: Mapping[str, Any], requested: Any) -> list[str]:
    identifiers = _group_ids(view)
    if requested is None:
        return identifiers
    if isinstance(requested, (str, bytes)) or not isinstance(requested, Sequence):
        raise ParameterConfirmationError(
            "scope must be a sequence of parameter group identifiers"
        )
    selected: list[str] = []
    for item in requested:
        identifier = _text(item)
        if not identifier:
            raise ParameterConfirmationError(
                "scope must contain only non-empty group identifiers"
            )
        if identifier not in identifiers:
            raise ParameterConfirmationError(
                f"scope entry {identifier!r} is not a parameter group of this"
                f" run; groups are {identifiers}"
            )
        selected.append(identifier)
    if "plant" in identifiers and "plant" not in selected:
        raise ParameterConfirmationError(
            "scope must include the 'plant' group when the run records one"
        )
    return selected


# ---------------------------------------------------------------------------
# Confirmation document
# ---------------------------------------------------------------------------


def confirmation_digest(confirmation: Mapping[str, Any]) -> str:
    """Return the canonical digest of ``confirmation`` without its own digest."""

    if not isinstance(confirmation, Mapping):
        raise ParameterConfirmationError("confirmation must be a JSON object")
    return stable_sha256(
        {
            key: value
            for key, value in confirmation.items()
            if key != "confirmation_digest"
        }
    )


def build_confirmation(
    run_dir,
    *,
    reviewer,
    statement,
    confirmed_at_utc=None,
    scope=None,
    acknowledged_limitations=None,
) -> dict:
    """Build a ``pe-sim.parameter-confirmation.v1`` document or refuse.

    Every refusal happens before a document is returned, and the package is
    only ever read.  A rejection raises :class:`ParameterConfirmationError`
    with the reason, so a caller never writes a partial confirmation.
    """

    _refuse_in_ci()

    reviewer_text = _text(reviewer)
    if not reviewer_text:
        raise ParameterConfirmationError("reviewer must be a non-empty name")
    statement_text = _text(statement)
    if len(statement_text) < _MINIMUM_STATEMENT_LENGTH:
        raise ParameterConfirmationError(
            "statement must be at least"
            f" {_MINIMUM_STATEMENT_LENGTH} characters of review text"
        )
    acknowledged = _acknowledged_limitations(acknowledged_limitations)
    confirmed_at = _format_utc(confirmed_at_utc)

    view = _build_view(run_dir)
    run_id = _view_run_id(view)
    if run_id is None:
        raise ParameterConfirmationError("parameter view does not record a run id")
    manifest_sha256, package_sha256 = _view_package_identity(view)
    try:
        digest = parameter_set_digest(view)
    except ParameterViewError as exc:
        raise ParameterConfirmationError(
            f"parameter set digest is unavailable: {exc}"
        ) from exc
    rows = _parameter_row_list(view)
    bounds = _bound_list(view)
    scope_ids = _scope(view, scope)
    view_limitations = _view_limitations(view)

    document: dict[str, Any] = {
        "schema_version": CONFIRMATION_SCHEMA,
        "rule_version": CONFIRMATION_RULE_VERSION,
        "run": {
            "run_id": run_id,
            "manifest_sha256": manifest_sha256,
            "package_sha256": package_sha256,
        },
        "parameter_set": {
            "digest": digest,
            "rows": rows,
            "bounds": bounds,
        },
        "review": {
            "reviewer": reviewer_text,
            "confirmed_at_utc": confirmed_at,
            "statement": statement_text,
            "scope": scope_ids,
            "acknowledged_limitations": (
                acknowledged if acknowledged is not None else list(view_limitations)
            ),
        },
        "claim_conclusion": _view_claim_conclusion(view),
        "limitations": list(CONFIRMATION_LIMITATIONS),
    }
    document["confirmation_digest"] = confirmation_digest(document)
    return document


def write_confirmation(
    path,
    confirmation: Mapping[str, Any],
    *,
    run_dir=None,
) -> Path:
    """Write ``confirmation`` as canonical JSON and return the path.

    Publication is immutable, so a caller that names the run directory cannot
    place the confirmation inside it.  The path check compares resolved paths,
    which also catches ``..`` traversal and different drive spellings.
    """

    if not isinstance(confirmation, Mapping):
        raise ParameterConfirmationError("confirmation must be a JSON object")
    target = Path(path)
    if run_dir is not None:
        try:
            run_root = Path(run_dir).expanduser().resolve()
        except OSError as exc:
            raise ParameterConfirmationError(
                f"cannot resolve the run directory {run_dir!r}: {exc}"
            ) from exc
        try:
            resolved_target = target.expanduser().resolve()
        except OSError as exc:
            raise ParameterConfirmationError(
                f"cannot resolve the output path {path!r}: {exc}"
            ) from exc
        try:
            resolved_target.relative_to(run_root)
        except ValueError:
            pass
        else:
            raise ParameterConfirmationError(
                "refusing to write a parameter confirmation inside the run"
                f" package: output '{resolved_target}' is inside run"
                f" directory '{run_root}'; publication is immutable"
            )

    text = json.dumps(dict(confirmation), ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(text.encode("utf-8"))
    except OSError as exc:
        raise ParameterConfirmationError(f"cannot write {target}: {exc}") from exc
    return target


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------


def _recorded_package_identity(run_dir: Any) -> tuple[str | None, str | None]:
    """Read the package identity that ``manifest.json`` records.

    A reader that recomputes the hashes answers "is this package intact?".
    This check answers "does the confirmation name this recorded run?", which
    is the identity a confirmation can be bound to.  A parameter edited without
    re-deriving the hashes therefore stays the same run (``stale``), while a
    rebuilt or republished package becomes a different package
    (``mismatched_run``).
    """

    path = Path(run_dir) / "manifest.json"
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None, None
    if not isinstance(manifest, Mapping):
        return None, None
    hashes = manifest.get("hashes")
    recorded = hashes if isinstance(hashes, Mapping) else {}

    def pick(name: str) -> str | None:
        for source in (manifest, recorded):
            value = _text(source.get(name))
            if value:
                return value
        return None

    return pick("manifest_sha256"), pick("package_sha256")


def _required_field_problems(confirmation: Mapping[str, Any]) -> list[str]:
    problems: list[str] = []
    run = confirmation.get("run")
    if not isinstance(run, Mapping):
        problems.append("run")
    else:
        for name in ("run_id", "manifest_sha256", "package_sha256"):
            if not _non_empty_text(run.get(name)):
                problems.append(f"run.{name}")

    parameter_set = confirmation.get("parameter_set")
    if not isinstance(parameter_set, Mapping):
        problems.append("parameter_set")
    else:
        if not _non_empty_text(parameter_set.get("digest")):
            problems.append("parameter_set.digest")
        rows = parameter_set.get("rows")
        if not isinstance(rows, list):
            problems.append("parameter_set.rows")
        else:
            for index, row in enumerate(rows):
                if (
                    not isinstance(row, Mapping)
                    or not _non_empty_text(row.get("group"))
                    or not _non_empty_text(row.get("name"))
                    or "value" not in row
                ):
                    problems.append(f"parameter_set.rows[{index}]")
        if not isinstance(parameter_set.get("bounds"), list):
            problems.append("parameter_set.bounds")

    review = confirmation.get("review")
    if not isinstance(review, Mapping):
        problems.append("review")
    else:
        for name in ("reviewer", "confirmed_at_utc", "statement"):
            if not _non_empty_text(review.get(name)):
                problems.append(f"review.{name}")
        selected = review.get("scope")
        if not isinstance(selected, list) or not all(
            _non_empty_text(item) for item in selected
        ):
            problems.append("review.scope")
        acknowledged = review.get("acknowledged_limitations")
        if not isinstance(acknowledged, list) or not all(
            isinstance(item, str) for item in acknowledged
        ):
            problems.append("review.acknowledged_limitations")

    limitations = confirmation.get("limitations")
    if not isinstance(limitations, list) or not all(
        isinstance(item, str) for item in limitations
    ):
        problems.append("limitations")
    if not _non_empty_text(confirmation.get("confirmation_digest")):
        problems.append("confirmation_digest")
    return problems


def _same_value(left: Any, right: Any) -> bool:
    # ``1 == True`` in Python, but they are different JSON values.
    if isinstance(left, bool) != isinstance(right, bool):
        return False
    return left == right


def _same_bound(left: Any, right: Any) -> bool:
    if left is None or right is None:
        return left is None and right is None
    return _same_value(left.get("minimum"), right.get("minimum")) and _same_value(
        left.get("maximum"), right.get("maximum")
    )


def _row_values(rows: Any) -> dict[tuple[str, str], tuple[Any, Any]]:
    values: dict[tuple[str, str], tuple[Any, Any]] = {}
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        return values
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        group = _text(row.get("group"))
        name = _text(row.get("name"))
        if group and name:
            values[(group, name)] = (row.get("value"), row.get("unit"))
    return values


def _bound_values(bounds: Any) -> dict[str, dict[str, Any]]:
    values: dict[str, dict[str, Any]] = {}
    if not isinstance(bounds, Sequence) or isinstance(bounds, (str, bytes)):
        return values
    for bound in bounds:
        if not isinstance(bound, Mapping):
            continue
        name = _text(bound.get("name"))
        if name:
            values[name] = {
                "minimum": bound.get("minimum"),
                "maximum": bound.get("maximum"),
            }
    return values


def _differences(
    confirmation: Mapping[str, Any],
    current_rows: Any,
    current_bounds: Any,
) -> list[dict[str, Any]]:
    parameter_set = confirmation.get("parameter_set")
    parameter_set = parameter_set if isinstance(parameter_set, Mapping) else {}
    confirmed_rows = _row_values(parameter_set.get("rows"))
    current_row_values = _row_values(current_rows)

    differences: list[dict[str, Any]] = []
    keys = sorted(
        set(confirmed_rows) | set(current_row_values),
        key=lambda item: (item[0], item[1]),
    )
    for key in keys:
        confirmed_row = confirmed_rows.get(key)
        current_row = current_row_values.get(key)
        if confirmed_row is None and current_row is None:
            continue
        if confirmed_row is None:
            differences.append(
                {
                    "group": key[0],
                    "name": key[1],
                    "confirmed": None,
                    "current": current_row[0],
                }
            )
        elif current_row is None:
            differences.append(
                {
                    "group": key[0],
                    "name": key[1],
                    "confirmed": confirmed_row[0],
                    "current": None,
                }
            )
        elif not _same_value(confirmed_row[0], current_row[0]) or (
            confirmed_row[1] != current_row[1]
        ):
            differences.append(
                {
                    "group": key[0],
                    "name": key[1],
                    "confirmed": confirmed_row[0],
                    "current": current_row[0],
                }
            )

    confirmed_bound_values = _bound_values(parameter_set.get("bounds"))
    current_bound_values = _bound_values(current_bounds)
    for name in sorted(set(confirmed_bound_values) | set(current_bound_values)):
        confirmed_bound = confirmed_bound_values.get(name)
        current_bound = current_bound_values.get(name)
        if _same_bound(confirmed_bound, current_bound):
            continue
        differences.append(
            {
                "group": "bounds",
                "name": name,
                "confirmed": confirmed_bound,
                "current": current_bound,
            }
        )
    differences.sort(key=lambda item: (item["group"], item["name"]))
    return differences


def verify_confirmation(
    run_dir,
    confirmation: Mapping[str, Any],
    *,
    confirmation_path=None,
) -> dict:
    """Compare ``confirmation`` with the current package and return a verdict.

    A package-side problem (for example a missing directory) yields a
    ``pe-sim.parameter-confirmation-verification.v1`` document with status
    ``unknown``; it never raises.  A malformed ``confirmation`` argument is a
    caller error and raises :class:`ParameterConfirmationError`.
    """

    if not isinstance(confirmation, Mapping):
        raise ParameterConfirmationError("confirmation must be a JSON object")

    path_text = None if confirmation_path is None else str(confirmation_path)
    confirmed_run = confirmation.get("run")
    confirmed_run = confirmed_run if isinstance(confirmed_run, Mapping) else {}
    confirmed_parameter_set = confirmation.get("parameter_set")
    confirmed_parameter_set = (
        confirmed_parameter_set
        if isinstance(confirmed_parameter_set, Mapping)
        else {}
    )
    confirmed_digest = _text(confirmed_parameter_set.get("digest")) or None

    result: dict[str, Any] = {
        "schema_version": VERIFICATION_SCHEMA,
        "run_id": _text(confirmed_run.get("run_id")) or None,
        "confirmation_path": path_text,
        "status": "unknown",
        "checks": [],
        "parameter_set_digest": {"confirmed": confirmed_digest, "current": None},
        "differences": [],
        "limitations": list(VERIFICATION_LIMITATIONS),
    }

    try:
        view = build_parameter_view(run_dir)
        if not isinstance(view, Mapping):
            raise ParameterViewError("parameter view must be a JSON object")
        current_run_id = _view_run_id(view)
        current_rows = _parameter_row_list(view)
        current_digest = parameter_set_digest(view)
        current_bounds = _bound_list(view)
        current_group_ids = _group_ids(view)
    except (ParameterViewError, ParameterConfirmationError) as exc:
        result["checks"] = [
            _check(
                "package",
                _FAIL,
                f"the run package could not be inspected: {exc}",
            )
        ]
        return result

    current_manifest_sha256, current_package_sha256 = _recorded_package_identity(
        run_dir
    )

    # 1. schema and required fields.  A missing required field makes the
    #    document unreadable, which is ``invalid`` rather than a mismatch.
    schema_details: list[str] = []
    if (
        confirmation.get("schema_version") != CONFIRMATION_SCHEMA
        or confirmation.get("rule_version") != CONFIRMATION_RULE_VERSION
    ):
        schema_details.append(
            f"expected {CONFIRMATION_SCHEMA!r}/{CONFIRMATION_RULE_VERSION!r},"
            f" found {confirmation.get('schema_version')!r}/"
            f"{confirmation.get('rule_version')!r}"
        )
    field_problems = _required_field_problems(confirmation)
    if field_problems:
        schema_details.append(
            "required fields are missing or unreadable: "
            + ", ".join(field_problems)
        )
    checks = [
        _check(
            "schema",
            _PASS if not schema_details else _FAIL,
            "; ".join(schema_details)
            if schema_details
            else "schema_version and rule_version match, and every required"
            " field is readable",
        )
    ]

    # 2. self-digest.
    stored_digest = confirmation.get("confirmation_digest")
    try:
        recomputed_digest = confirmation_digest(confirmation)
    except ParameterConfirmationError:
        recomputed_digest = ""
    digest_ok = (
        isinstance(stored_digest, str)
        and bool(stored_digest.strip())
        and stored_digest == recomputed_digest
    )
    checks.append(
        _check(
            "confirmation-digest",
            _PASS if digest_ok else _FAIL,
            f"confirmation_digest {stored_digest!r} matches the canonical body"
            if digest_ok
            else "confirmation_digest does not match the recomputed body"
            f" ({recomputed_digest or '<unreadable>'})",
        )
    )

    # 3. run identity: the confirmation names one run.
    confirmed_manifest_sha256 = _text(confirmed_run.get("manifest_sha256")) or None
    confirmed_package_sha256 = _text(confirmed_run.get("package_sha256")) or None
    identity_problems: list[str] = []
    if confirmed_run.get("run_id") != current_run_id:
        identity_problems.append(
            f"run_id {confirmed_run.get('run_id')!r} != current {current_run_id!r}"
        )
    checks.append(
        _check(
            "run-identity",
            _PASS if not identity_problems else _FAIL,
            "the confirmation names this run"
            if not identity_problems
            else "; ".join(identity_problems),
        )
    )

    # 3b. package identity: the same run, but not necessarily the same bytes.
    # A re-run with different parameters changes the package, and that is
    # ``stale`` rather than a different run.
    package_problems: list[str] = []
    if confirmed_manifest_sha256 != current_manifest_sha256:
        package_problems.append(
            "manifest_sha256"
            f" {confirmed_manifest_sha256!r} != current"
            f" {current_manifest_sha256!r}"
        )
    if confirmed_package_sha256 != current_package_sha256:
        package_problems.append(
            "package_sha256"
            f" {confirmed_package_sha256!r} != current"
            f" {current_package_sha256!r}"
        )
    checks.append(
        _check(
            "package-identity",
            _PASS if not package_problems else _FAIL,
            "both recorded package hashes match"
            if not package_problems
            else "; ".join(package_problems),
        )
    )

    # 4. parameter-set digest.
    digest_match = (
        confirmed_digest is not None and confirmed_digest == current_digest
    )
    checks.append(
        _check(
            "parameter-set-digest",
            _PASS if digest_match else _FAIL,
            f"confirmed digest {confirmed_digest!r} matches the current"
            f" {current_digest}"
            if digest_match
            else f"confirmed digest {confirmed_digest!r} != current"
            f" {current_digest}",
        )
    )

    # 5. scope coverage.
    review = confirmation.get("review")
    review = review if isinstance(review, Mapping) else {}
    scope = review.get("scope")
    if not isinstance(scope, list) or not all(
        _non_empty_text(item) for item in scope
    ):
        scope_status = _FAIL
        scope_detail = "review.scope is not a list of group identifiers"
    else:
        missing = [
            identifier for identifier in current_group_ids if identifier not in scope
        ]
        scope_status = _FAIL if missing else _PASS
        scope_detail = (
            "scope does not cover the current parameter groups: "
            + ", ".join(missing)
            if missing
            else f"scope covers all {len(current_group_ids)} current parameter"
            " groups"
        )
    checks.append(_check("scope-coverage", scope_status, scope_detail))

    # 6. claim conclusion was recorded.
    conclusion = confirmation.get("claim_conclusion")
    claim_ok = _non_empty_text(conclusion)
    checks.append(
        _check(
            "claim-recorded",
            _PASS if claim_ok else _FAIL,
            f"claim conclusion is recorded: {conclusion!r}"
            if claim_ok
            else "claim_conclusion is missing or empty",
        )
    )

    status_by_id = {check["id"]: check["status"] for check in checks}
    if (
        status_by_id["schema"] == _FAIL
        or status_by_id["confirmation-digest"] == _FAIL
    ):
        status = "invalid"
    elif status_by_id["run-identity"] == _FAIL:
        status = "mismatched_run"
    elif (
        status_by_id["package-identity"] == _FAIL
        or status_by_id["parameter-set-digest"] == _FAIL
    ):
        status = "stale"
    elif any(check["status"] == _FAIL for check in checks):
        # A scope that omits a current group, or a missing claim conclusion,
        # leaves the record unusable even though its digest is intact.
        status = "invalid"
    else:
        status = "current"

    result.update(
        {
            "run_id": current_run_id or result["run_id"],
            "status": status,
            "checks": checks,
            "parameter_set_digest": {
                "confirmed": confirmed_digest,
                "current": current_digest,
            },
            "differences": (
                _differences(confirmation, current_rows, current_bounds)
                if status == "stale"
                else []
            ),
        }
    )
    return result


def render_verification_text(result: Mapping[str, Any]) -> str:
    """Render a short, plain-text summary of a verification result."""

    if not isinstance(result, Mapping):
        raise ParameterConfirmationError(
            "verification result must be a JSON object"
        )
    status = _text(result.get("status")) or "unknown"
    lines = [f"parameter confirmation: {status}"]
    lines.append(f"run: {_text(result.get('run_id')) or '<unknown>'}")
    confirmation_path = result.get("confirmation_path")
    if confirmation_path:
        lines.append(f"confirmation: {confirmation_path}")
    digests = result.get("parameter_set_digest")
    digests = digests if isinstance(digests, Mapping) else {}
    lines.append(
        "confirmed parameter-set digest:"
        f" {_text(digests.get('confirmed')) or '<none>'}"
    )
    lines.append(
        f"current parameter-set digest: {_text(digests.get('current')) or '<none>'}"
    )
    checks = result.get("checks")
    if isinstance(checks, Sequence) and not isinstance(checks, (str, bytes)):
        for check in checks:
            if not isinstance(check, Mapping) or check.get("status") == _PASS:
                continue
            lines.append(
                f"failed check {check.get('id')}: {check.get('detail')}"
            )
    differences = result.get("differences")
    differences = (
        [item for item in differences if isinstance(item, Mapping)]
        if isinstance(differences, Sequence) and not isinstance(differences, (str, bytes))
        else []
    )
    if differences:
        for item in differences:
            lines.append(
                f"difference {item.get('group')}.{item.get('name')}:"
                f" confirmed {item.get('confirmed')!r}"
                f" -> current {item.get('current')!r}"
            )
    else:
        lines.append("differences: none")
    return "\n".join(lines) + "\n"


__all__ = [
    "CONFIRMATION_RULE_VERSION",
    "CONFIRMATION_SCHEMA",
    "VERIFICATION_SCHEMA",
    "ParameterConfirmationError",
    "build_confirmation",
    "confirmation_digest",
    "render_verification_text",
    "verify_confirmation",
    "write_confirmation",
]

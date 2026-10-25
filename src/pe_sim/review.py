"""Text-first review surface over one published run package.

The review is a *reading surface*: it reorders and labels data that the
package already records.  It never executes a model, never re-derives a
physical quantity, never changes a verdict, and never writes into the
package.  The whole "UI" of this version is one deterministic document per
run.

The fixed reading hierarchy is:

    identity -> claim -> parameters -> checks -> signals -> provenance ->
    artifacts -> limitations

`build_run_review` returns the ``pe-sim.run-review.v1`` envelope and
``render_text`` / ``render_markdown`` turn that envelope into ASCII text.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from .inspection import RunInspectionError, inspect_run
from .parameter_view import ParameterViewError, build_parameter_view


#: Schema tag for the review envelope.
REVIEW_SCHEMA = "pe-sim.run-review.v1"

#: Rule version recorded with every review.
REVIEW_RULE_VERSION = "review-v1"

#: Fixed section order.  Every review always carries all eight sections.
SECTION_ORDER = (
    "integrity",
    "claim",
    "parameters",
    "checks",
    "signals",
    "provenance",
    "artifacts",
    "limitations",
)

_TITLES = {
    "integrity": "Package identity and integrity",
    "claim": "Claim",
    "parameters": "Parameters",
    "checks": "Checks",
    "signals": "Signals",
    "provenance": "Provenance",
    "artifacts": "Artifacts",
    "limitations": "Limitations",
}

#: Fixed envelope limitations.  They describe what this reading surface is
#: and is not; they never restate or replace the package's own limitations.
_REVIEW_LIMITATIONS = (
    "The review is a reading surface over already-recorded package data; it "
    "adds no evidence and re-executes no model.",
    "The review changes no verdict: evidence level, qualification level, "
    "credibility, and the claim conclusion stay exactly as recorded.",
    "The review contains no graphics, topology rendering, waveform plot, or "
    "interactive control.",
    "The checks section caps each finding group; a note reports how many "
    "recorded findings are not shown.",
)

_FINDING_SOURCES = ("applicability", "safety", "qualification")

_ROW_VERDICT_STATUSES = frozenset({"ok", "attention", "fail", "not_recorded"})
_ROW_MARKER_STATUSES = frozenset({"attention", "fail", "not_recorded"})


class ReviewError(ValueError):
    """Raised when a run directory cannot be reviewed at all."""


# ---------------------------------------------------------------------------
# Small value helpers
# ---------------------------------------------------------------------------


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _value(value: Any) -> str:
    """Render one recorded scalar as a deterministic string."""

    if value is None:
        return "not recorded"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, str):
        return value if value else "not recorded"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (Mapping, list, tuple)):
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
    return str(value)


def _text_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _row(label: str, value: Any, status: str | None = None) -> dict[str, Any]:
    if status is not None and status not in _ROW_VERDICT_STATUSES:
        raise ReviewError(f"unsupported row status: {status!r}")
    return {"label": label, "value": _value(value), "status": status}


def _recorded(label: str, value: Any, status: str | None = None) -> dict[str, Any]:
    """A row that says ``not recorded`` when the source field is absent."""

    if value is None or (isinstance(value, str) and not value):
        return _row(label, "not recorded", "not_recorded")
    return _row(label, value, status)


def _first16(value: Any) -> str | None:
    text = _text_or_none(value)
    return text[:16] if text is not None else None


def _combine_statuses(statuses: Sequence[str | None]) -> str:
    if "fail" in statuses:
        return "fail"
    if "attention" in statuses:
        return "attention"
    return "ok"


def _section(
    section_id: str,
    *,
    rows: list[dict[str, Any]],
    children: list[dict[str, Any]] | None = None,
    notes: list[str] | None = None,
    available: bool = True,
) -> dict[str, Any]:
    children = list(children or [])
    notes = list(notes or [])
    statuses = [row["status"] for row in rows]
    statuses.extend(child["status"] for child in children)
    status = _combine_statuses(statuses)
    if status == "ok" and not available:
        status = "unavailable"
    return {
        "id": section_id,
        "title": _TITLES[section_id],
        "level": 1,
        "status": status,
        "rows": rows,
        "notes": notes,
        "children": children,
    }


def _child(
    child_id: str,
    title: str,
    *,
    rows: list[dict[str, Any]],
    notes: list[str] | None = None,
    children: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    notes = list(notes or [])
    children = list(children or [])
    statuses = [row["status"] for row in rows]
    statuses.extend(child["status"] for child in children)
    return {
        "id": child_id,
        "title": title,
        "level": 2,
        "status": _combine_statuses(statuses),
        "rows": rows,
        "notes": notes,
        "children": children,
    }


# ---------------------------------------------------------------------------
# Manifest access
# ---------------------------------------------------------------------------


def _load_manifest(run_dir: Path) -> Mapping[str, Any]:
    try:
        payload = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReviewError(
            f"manifest.json is unreadable: {type(exc).__name__}: {exc}"
        ) from exc
    if not isinstance(payload, Mapping):
        raise ReviewError("manifest.json must be a JSON object")
    return payload


def _findings_at(document: Mapping[str, Any], *path: str) -> list[Any] | None:
    node: Any = document
    for key in path:
        if not isinstance(node, Mapping):
            return None
        node = node.get(key)
    return node if isinstance(node, list) else None


def _recorded_findings(
    manifest: Mapping[str, Any], summary: Mapping[str, Any]
) -> dict[str, list[Any] | None]:
    """Return the recorded findings of each check source, if any."""

    qualification = _findings_at(manifest, "qualification", "checks", "findings")
    if qualification is None:
        qualification = _findings_at(summary, "qualification", "checks", "findings")
    return {
        "applicability": _findings_at(manifest, "applicability", "checks", "findings"),
        "safety": _findings_at(
            manifest, "safety", "applicability", "checks", "findings"
        ),
        "qualification": qualification,
    }


# ---------------------------------------------------------------------------
# Section builders
# ---------------------------------------------------------------------------


def _integrity_section(
    summary: Mapping[str, Any],
    *,
    integrity: str,
    artifact_count: Any,
) -> dict[str, Any]:
    hashes = _mapping(summary.get("hashes"))
    compatibility = _mapping(summary.get("compatibility"))
    rows = [
        _recorded("run id", summary.get("run_id")),
        _recorded("run mode", summary.get("run_mode")),
        _recorded("status", summary.get("run_status")),
        _row("package integrity", integrity, integrity),
        _recorded("artifact count", artifact_count),
        _recorded("compatibility", compatibility.get("status")),
        _recorded("manifest digest", _first16(hashes.get("manifest_sha256"))),
        _recorded("package digest", _first16(hashes.get("package_sha256"))),
    ]
    return _section("integrity", rows=rows)


def _claim_section(claim: Mapping[str, Any]) -> dict[str, Any]:
    available = bool(claim)
    rows: list[dict[str, Any]] = []
    if available:
        rows.append(_recorded("conclusion", claim.get("conclusion")))
        rows.append(_recorded("statement", claim.get("statement")))
        for key, label in (
            ("may_claim", "may claim"),
            ("may_not_claim", "may not claim"),
            ("narrowed_by", "narrowed by"),
        ):
            entries = claim.get(key)
            if not isinstance(entries, (list, tuple)):
                continue
            for entry in entries:
                if isinstance(entry, str) and entry:
                    rows.append(_row(label, entry))
    notes = [] if available else ["the package records no claim payload"]
    return _section("claim", rows=rows, notes=notes, available=available)


def _parameter_value(row: Mapping[str, Any]) -> str:
    text = _value(row.get("value"))
    unit = _text_or_none(row.get("unit"))
    if unit is not None:
        text = f"{text} {unit}"
    source = _text_or_none(row.get("source"))
    review_status = _text_or_none(row.get("review_status"))
    if source is not None or review_status is not None:
        text += (
            "  (source: "
            + (source if source is not None else "not recorded")
            + ", review: "
            + (review_status if review_status is not None else "not recorded")
            + ")"
        )
    return text


def _parameters_section(view: Mapping[str, Any]) -> dict[str, Any]:
    parameter_set = _mapping(view.get("parameter_set"))
    counts = _mapping(parameter_set.get("counts"))
    groups = parameter_set.get("groups")
    group_list = [group for group in groups if isinstance(group, Mapping)] if isinstance(
        groups, (list, tuple)
    ) else []
    available = bool(parameter_set) and (bool(counts) or bool(group_list))
    rows = [
        _recorded("parameter count", counts.get("parameters")),
        _recorded("group count", counts.get("groups")),
        _recorded("parameters without unit", counts.get("without_unit")),
        _recorded("parameters without source", counts.get("without_source")),
    ]
    children: list[dict[str, Any]] = []
    for group in group_list:
        group_id = _text_or_none(group.get("id")) or "unnamed group"
        parameters = group.get("parameters")
        parameter_list = (
            [item for item in parameters if isinstance(item, Mapping)]
            if isinstance(parameters, (list, tuple))
            else []
        )
        child_rows = [
            _row(
                _text_or_none(item.get("name")) or "unnamed parameter",
                _parameter_value(item),
            )
            for item in parameter_list
        ]
        children.append(_child(group_id, group_id, rows=child_rows))
    notes: list[str] = []
    if not available:
        notes.append("the package records no parameter projection")
    elif not group_list:
        notes.append("the recorded parameter set has no groups")
    return _section("parameters", rows=rows, children=children, notes=notes, available=available)


def _finding_row_status(finding: Mapping[str, Any]) -> str | None:
    if finding.get("passed") is False:
        severity = finding.get("severity")
        if severity == "error":
            return "fail"
        if severity == "warning":
            return "attention"
    return None


def _finding_label(finding: Mapping[str, Any]) -> str:
    """Name the thing a finding is about, falling back to the rule id."""

    evidence = finding.get("evidence")
    if isinstance(evidence, Mapping):
        parameter = _text_or_none(evidence.get("parameter"))
        if parameter:
            return parameter
    return _text_or_none(finding.get("rule_id")) or "finding"


def _finding_value(finding: Mapping[str, Any]) -> str:
    """One short line per finding; the message belongs to the group note."""

    passed = finding.get("passed")
    passed_text = "yes" if passed is True else "no" if passed is False else "not recorded"
    severity = _text_or_none(finding.get("severity")) or "not recorded"
    parts: list[str] = []
    evidence = finding.get("evidence")
    if isinstance(evidence, Mapping) and _text_or_none(evidence.get("parameter")):
        value = evidence.get("value")
        unit = _text_or_none(evidence.get("unit")) or ""
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            parts.append(f"{value} {unit}".strip())
        source = _text_or_none(evidence.get("source")) or "not recorded"
        review = _text_or_none(evidence.get("review_status")) or "not recorded"
        parts.append(f"source: {source}, review: {review}")
    else:
        rule = _text_or_none(finding.get("rule_id"))
        if rule:
            parts.append(f"rule: {rule}")
    parts.extend([f"passed: {passed_text}", f"severity: {severity}"])
    return ", ".join(parts)


def _group_notes(group: list[Mapping[str, Any]]) -> list[str]:
    """Distinct finding messages, collapsed so a repeated line prints once."""

    messages: list[str] = []
    for item in group:
        message = _text_or_none(item.get("message"))
        if message and message not in messages:
            messages.append(message)
    notes = messages[:2]
    if len(messages) > 2:
        notes.append(f"{len(messages) - 2} more distinct message(s)")
    return notes


def _checks_section(
    findings: Mapping[str, list[Any] | None],
    *,
    max_findings_per_group: int,
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    children: list[dict[str, Any]] = []
    section_notes: list[str] = []
    available = False
    applicability = findings.get("applicability")
    duplicate_safety = (
        isinstance(applicability, list)
        and isinstance(findings.get("safety"), list)
        and findings.get("safety") == applicability
    )
    for source in _FINDING_SOURCES:
        recorded = findings.get(source)
        if recorded is None:
            rows.extend(
                [
                    _row(f"{source} passed", "not recorded", "not_recorded"),
                    _row(f"{source} failed", "not recorded", "not_recorded"),
                    _row(f"{source} total", "not recorded", "not_recorded"),
                ]
            )
            continue
        available = True
        entries = [item for item in recorded if isinstance(item, Mapping)]
        passed = sum(1 for item in entries if item.get("passed") is True)
        not_passed = sum(1 for item in entries if item.get("passed") is False)
        # Only a not-passed warning or error blocks; an informational finding
        # explains provenance and must not read as a failure in the summary.
        blocking = sum(1 for item in entries if _finding_row_status(item) in {"attention", "fail"})
        rows.append(
            _row(
                f"{source} findings",
                f"{len(entries)} total, {passed} passed, {not_passed} not passed,"
                f" {blocking} blocking",
            )
        )
        if source == "safety" and duplicate_safety:
            section_notes.append(
                "the safety section repeats the applicability findings; they are"
                " listed once, under applicability"
            )
            continue
        by_category: dict[str, list[Mapping[str, Any]]] = {}
        for item in entries:
            category = _text_or_none(item.get("category")) or "uncategorised"
            by_category.setdefault(category, []).append(item)
        for category in sorted(by_category):
            group = by_category[category]
            all_statuses = [_finding_row_status(item) for item in group]
            shown = group[: max(0, max_findings_per_group)]
            group_rows = [
                _row(
                    _finding_label(item),
                    _finding_value(item),
                    _finding_row_status(item),
                )
                for item in shown
            ]
            notes: list[str] = _group_notes(group)
            if len(group) > len(shown):
                notes.append(
                    f"{len(group) - len(shown)} more finding(s) not shown"
                )
            children.append(
                {
                    "id": f"{source}:{category}",
                    "title": f"{source}: {category}",
                    "level": 2,
                    "status": _combine_statuses(all_statuses),
                    "rows": group_rows,
                    "notes": notes,
                    "children": [],
                }
            )
    notes = list(section_notes) if available else ["no check findings were recorded"]
    return _section("checks", rows=rows, children=children, notes=notes, available=available)


def _signals_section(summary: Mapping[str, Any]) -> dict[str, Any]:
    samples = _mapping(summary.get("samples"))
    available = bool(samples)
    non_finite = samples.get("non_finite")
    non_finite_count = len(non_finite) if isinstance(non_finite, (list, tuple)) else None
    monotonic = samples.get("time_monotonic")
    monotonic_status = (
        "fail" if monotonic is False else "ok" if monotonic is True else None
    )
    non_finite_status = (
        "fail" if isinstance(non_finite_count, int) and non_finite_count > 0 else None
    )
    rows = [
        _recorded("sample count", samples.get("count")),
        _recorded("first time", samples.get("first_time_s")),
        _recorded("last time", samples.get("last_time_s")),
        _row(
            "time monotonic",
            "not recorded" if monotonic is None else _value(monotonic),
            monotonic_status,
        ),
        _row(
            "non-finite count",
            "not recorded" if non_finite_count is None else _value(non_finite_count),
            non_finite_status,
        ),
    ]
    signals = _mapping(samples.get("signals"))
    children: list[dict[str, Any]] = []
    for name in sorted(signals):
        if name == "time_s":
            continue
        stats = _mapping(signals[name])
        child_rows = [
            _recorded("n", stats.get("count")),
            _recorded("min", stats.get("min")),
            _recorded("max", stats.get("max")),
            _recorded("mean", stats.get("mean")),
            _recorded("last", stats.get("last")),
        ]
        children.append(_child(name, name, rows=child_rows))
    notes: list[str] = []
    if not available:
        notes.append("the package records no sample summary")
    elif not children:
        notes.append("the package records no per-signal statistics")
    return _section("signals", rows=rows, children=children, notes=notes, available=available)


def _provenance_section(
    manifest: Mapping[str, Any], summary: Mapping[str, Any]
) -> dict[str, Any]:
    source_commit = _text_or_none(summary.get("source_commit"))
    commit_value = source_commit[:12] if source_commit is not None else None
    credibility = _mapping(summary.get("credibility"))
    environment = _mapping(manifest.get("environment"))
    rows = [
        _recorded("source commit", commit_value),
        _recorded("worktree status", summary.get("working_tree_status")),
        _recorded("evidence level", summary.get("evidence_level")),
        _recorded("qualification level", summary.get("qualification_level")),
        _recorded("credibility status", credibility.get("status")),
        _recorded("environment status", environment.get("status")),
    ]
    available = any(row["status"] != "not_recorded" for row in rows)
    children: list[dict[str, Any]] = []
    backends = summary.get("backend")
    backend_list = backends if isinstance(backends, (list, tuple)) else []
    for index, backend in enumerate(backend_list):
        if not isinstance(backend, Mapping):
            continue
        name = _text_or_none(backend.get("name")) or f"backend {index + 1}"
        child_rows = [
            _recorded("version", backend.get("version")),
            _recorded("status", backend.get("status")),
        ]
        settings = _mapping(backend.get("solver_settings"))
        for setting in sorted(settings):
            child_rows.append(_recorded(setting, settings[setting]))
        children.append(_child(name, name, rows=child_rows))
    notes: list[str] = []
    if not available:
        notes.append("the package records no provenance facts")
    elif not children:
        notes.append("the package records no backend provenance")
    return _section("provenance", rows=rows, children=children, notes=notes, available=available)


def _artifact_size(run_dir: Path, relative: str, metadata: Mapping[str, Any]) -> str | None:
    for key in ("size", "size_bytes"):
        recorded = metadata.get(key)
        if isinstance(recorded, int) and not isinstance(recorded, bool):
            return str(recorded)
    try:
        candidate = (run_dir / relative).resolve()
        candidate.relative_to(run_dir.resolve())
        if candidate.is_file():
            return str(candidate.stat().st_size)
    except (OSError, ValueError):
        return None
    return None


def _artifact_entries(artifacts: Any) -> list[tuple[str, Mapping[str, Any]]] | None:
    """Normalise the two documented artifact-index shapes, or return None."""

    if isinstance(artifacts, Mapping):
        entries: list[tuple[str, Mapping[str, Any]]] = []
        for raw_path in sorted(artifacts, key=str):
            metadata = artifacts[raw_path]
            entries.append(
                (str(raw_path), metadata if isinstance(metadata, Mapping) else {})
            )
        return entries
    if isinstance(artifacts, list):
        entries = []
        for index, item in enumerate(artifacts):
            if isinstance(item, Mapping):
                path = _text_or_none(item.get("path")) or f"artifact {index + 1}"
                entries.append((path, item))
            else:
                entries.append((f"artifact {index + 1}", {}))
        return entries
    return None


def _artifacts_section(
    manifest: Mapping[str, Any], summary: Mapping[str, Any], run_dir: Path
) -> dict[str, Any]:
    artifacts = manifest.get("artifacts")
    entries = _artifact_entries(artifacts)
    available = entries is not None
    rows = [_recorded("artifact count", summary.get("artifact_count"))]
    children: list[dict[str, Any]] = []
    notes: list[str] = []
    if entries is None:
        notes.append(
            "the recorded artifact index has an unreadable shape; "
            "artifact metadata is unavailable"
        )
    elif not entries:
        notes.append("the recorded artifact index is empty")
    else:
        for path, metadata in entries:
            size = _artifact_size(run_dir, path, metadata)
            digest = _first16(metadata.get("sha256"))
            if size is None:
                value = f"size not recorded  sha256:{digest or 'not recorded'}"
                status = "not_recorded"
            else:
                value = f"{size} bytes  sha256:{digest or 'not recorded'}"
                status = None if digest else "not_recorded"
            children.append(
                _child(path, path, rows=[_row(path, value, status)])
            )
    return _section(
        "artifacts", rows=rows, children=children, notes=notes, available=available
    )


def _limitations_section(view: Mapping[str, Any], summary: Mapping[str, Any]) -> dict[str, Any]:
    credibility = _mapping(summary.get("credibility"))
    recorded: list[str] = []
    for source in (credibility.get("limitations"), view.get("limitations")):
        if not isinstance(source, (list, tuple)):
            continue
        for item in source:
            if isinstance(item, str) and item:
                recorded.append(item)
    seen: set[str] = set()
    unique: list[str] = []
    for item in recorded:
        if item not in seen:
            seen.add(item)
            unique.append(item)
    rows = [_row("limitation", item) for item in unique]
    notes = [] if rows else ["the package records no limitations"]
    return _section(
        "limitations", rows=rows, notes=notes, available=bool(rows)
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_run_review(
    run_dir: str | Path, *, max_findings_per_group: int = 20
) -> dict[str, Any]:
    """Build the deterministic review envelope for one published package.

    Integrity and the parameter projection are verified before anything is
    read for display.  A package that cannot be verified raises
    :class:`ReviewError` instead of being reviewed.
    """

    try:
        inspection = inspect_run(run_dir)
    except RunInspectionError as exc:
        raise ReviewError(str(exc)) from exc
    if inspection.status != "ok":
        detail = "; ".join(inspection.problems) or "unknown integrity problem"
        raise ReviewError(
            f"run package integrity status is {inspection.status!r}: {detail}"
        )

    try:
        view = build_parameter_view(run_dir)
    except ParameterViewError as exc:
        raise ReviewError(str(exc)) from exc

    summary = _mapping(inspection.summary)
    manifest = _load_manifest(Path(inspection.run_dir))

    verified = summary.get("artifact_index_verified") is True
    integrity = "ok" if inspection.status == "ok" and verified else "fail"
    claim = _mapping(summary.get("claim"))
    if not claim:
        raise ReviewError("run inspection did not report a claim")

    findings = _recorded_findings(manifest, summary)
    sections = [
        _integrity_section(
            summary,
            integrity=integrity,
            artifact_count=summary.get("artifact_count"),
        ),
        _claim_section(claim),
        _parameters_section(view),
        _checks_section(
            findings, max_findings_per_group=max_findings_per_group
        ),
        _signals_section(summary),
        _provenance_section(manifest, summary),
        _artifacts_section(manifest, summary, Path(inspection.run_dir)),
        _limitations_section(view, summary),
    ]
    return {
        "schema_version": REVIEW_SCHEMA,
        "rule_version": REVIEW_RULE_VERSION,
        "run_id": summary.get("run_id"),
        "summary": {
            "integrity": integrity,
            "claim": claim.get("conclusion"),
            "qualification": summary.get("qualification_level"),
            "evidence": summary.get("evidence_level"),
        },
        "sections": sections,
        "limitations": list(_REVIEW_LIMITATIONS),
    }


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

#: Control characters that have a short, readable escape.  Backslash comes
#: first so the escapes introduced afterwards stay unambiguous.
_CONTROL_ESCAPES = (
    ("\\", "\\\\"),
    ("\n", "\\n"),
    ("\r", "\\r"),
    ("\t", "\\t"),
)


def _escape_controls(value: Any) -> str:
    """Escape control characters so one recorded string stays one line.

    The review is the one surface a human reads, and every run id, section
    title, row label, row value, note, and child title in it can come from the
    package.  An unescaped newline would let a recorded string write a fake
    section header or a fake ``claim:`` line, so every dynamic string goes
    through this helper before it reaches the output.

    The order is fixed: backslash, then ``\\n``, ``\\r``, and ``\\t``, then
    every remaining C0 control character and ``\\x7f`` as ``\\xNN`` with
    lowercase hex.  Printable characters, non-ASCII ones included, are never
    touched: the text renderer's final ASCII step still owns those.  A value
    without control characters is returned unchanged, so the rendering of an
    ordinary package stays byte-identical.
    """

    text = value if isinstance(value, str) else str(value)
    if not any(
        ord(character) < 0x20 or ord(character) == 0x7F for character in text
    ):
        return text
    escaped = text
    for character, replacement in _CONTROL_ESCAPES:
        escaped = escaped.replace(character, replacement)
    return "".join(
        f"\\x{ord(character):02x}"
        if ord(character) < 0x20 or ord(character) == 0x7F
        else character
        for character in escaped
    )


def _ascii(text: str) -> str:
    return text.encode("ascii", "backslashreplace").decode("ascii")


def _row_marker(row: Mapping[str, Any]) -> str:
    status = row.get("status")
    if status in _ROW_MARKER_STATUSES:
        return f"  [!] {status}"
    return ""


def _text_section_lines(section: Mapping[str, Any], number: int) -> list[str]:
    header = f"== {number}. {_escape_controls(section.get('title'))} =="
    status = section.get("status")
    if status is not None and status != "ok":
        header += f"  [{_escape_controls(status)}]"
    lines = [header]
    for row in section.get("rows") or []:
        lines.append(
            f"  - {_escape_controls(row.get('label'))}: "
            f"{_escape_controls(row.get('value'))}{_row_marker(row)}"
        )
    for note in section.get("notes") or []:
        lines.append(f"  (note: {_escape_controls(note)})")
    lines.extend(_text_child_lines(section.get("children") or [], depth=2))
    return lines


def _text_child_lines(children: Sequence[Mapping[str, Any]], *, depth: int) -> list[str]:
    lines: list[str] = []
    indent = 2 * (depth - 1)
    prefix = "-- " if depth == 2 else "- "
    row_indent = indent + 3
    for child in children:
        header = f"{' ' * indent}{prefix}{_escape_controls(child.get('title'))}"
        status = child.get("status")
        if status is not None and status != "ok":
            header += f"  [{_escape_controls(status)}]"
        lines.append(header)
        for row in child.get("rows") or []:
            lines.append(
                f"{' ' * row_indent}- {_escape_controls(row.get('label'))}: "
                f"{_escape_controls(row.get('value'))}{_row_marker(row)}"
            )
        for note in child.get("notes") or []:
            lines.append(f"{' ' * row_indent}(note: {_escape_controls(note)})")
        lines.extend(
            _text_child_lines(child.get("children") or [], depth=depth + 1)
        )
    return lines


def render_text(review: Mapping[str, Any]) -> str:
    """Render the review as deterministic, ASCII-only text."""

    summary = _mapping(review.get("summary"))
    lines = [
        f"pe-sim run review: {_escape_controls(_value(review.get('run_id')))}",
        "claim: {claim} | qualification: {qualification} | evidence: {evidence}"
        " | integrity: {integrity}".format(
            claim=_escape_controls(_value(summary.get("claim"))),
            qualification=_escape_controls(_value(summary.get("qualification"))),
            evidence=_escape_controls(_value(summary.get("evidence"))),
            integrity=_escape_controls(_value(summary.get("integrity"))),
        ),
    ]
    sections = review.get("sections")
    if isinstance(sections, (list, tuple)):
        for number, section in enumerate(sections, start=1):
            if not isinstance(section, Mapping):
                continue
            lines.append("")
            lines.extend(_text_section_lines(section, number))
    return _ascii("\n".join(lines) + "\n")


def _markdown_row_marker(row: Mapping[str, Any]) -> str:
    status = row.get("status")
    if status in _ROW_MARKER_STATUSES:
        return f" _{status}_"
    return ""


def _markdown_section_lines(section: Mapping[str, Any], number: int) -> list[str]:
    header = f"## {number}. {_escape_controls(section.get('title'))}"
    status = section.get("status")
    if status is not None and status != "ok":
        header += f" - {_escape_controls(status)}"
    lines = [header]
    for row in section.get("rows") or []:
        lines.append(
            f"- **{_escape_controls(row.get('label'))}**: "
            f"{_escape_controls(row.get('value'))}{_markdown_row_marker(row)}"
        )
    for note in section.get("notes") or []:
        lines.append(f"- _note_: {_escape_controls(note)}")
    lines.extend(
        _markdown_child_lines(section.get("children") or [], depth=2)
    )
    return lines


def _markdown_child_lines(
    children: Sequence[Mapping[str, Any]], *, depth: int
) -> list[str]:
    lines: list[str] = []
    heading = "#" * (depth + 1)
    indent = "  " * (depth - 2)
    for child in children:
        header = f"{heading} {_escape_controls(child.get('title'))}"
        status = child.get("status")
        if status is not None and status != "ok":
            header += f" - {_escape_controls(status)}"
        lines.append("")
        lines.append(header)
        for row in child.get("rows") or []:
            lines.append(
                f"{indent}- **{_escape_controls(row.get('label'))}**: "
                f"{_escape_controls(row.get('value'))}{_markdown_row_marker(row)}"
            )
        for note in child.get("notes") or []:
            lines.append(f"{indent}- _note_: {_escape_controls(note)}")
        lines.extend(
            _markdown_child_lines(child.get("children") or [], depth=depth + 1)
        )
    return lines


def render_markdown(review: Mapping[str, Any]) -> str:
    """Render the same hierarchy as markdown, with ASCII content only."""

    summary = _mapping(review.get("summary"))
    lines = [
        f"# pe-sim run review: {_escape_controls(_value(review.get('run_id')))}",
        "",
        "claim: {claim} | qualification: {qualification} | evidence: {evidence}"
        " | integrity: {integrity}".format(
            claim=_escape_controls(_value(summary.get("claim"))),
            qualification=_escape_controls(_value(summary.get("qualification"))),
            evidence=_escape_controls(_value(summary.get("evidence"))),
            integrity=_escape_controls(_value(summary.get("integrity"))),
        ),
    ]
    sections = review.get("sections")
    if isinstance(sections, (list, tuple)):
        for number, section in enumerate(sections, start=1):
            if not isinstance(section, Mapping):
                continue
            lines.append("")
            lines.extend(_markdown_section_lines(section, number))
    return _ascii("\n".join(lines) + "\n")


__all__ = [
    "REVIEW_RULE_VERSION",
    "REVIEW_SCHEMA",
    "SECTION_ORDER",
    "ReviewError",
    "build_run_review",
    "render_markdown",
    "render_text",
]

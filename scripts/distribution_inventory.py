#!/usr/bin/env python3
"""Record a deterministic inventory of a built wheel.

The inventory answers one narrow question: *what exactly is in the artifact that
we ship?*  It records the hash of the wheel file, every archive member with its
own hash, the requirements the wheel declares, and the license string the wheel
metadata carries.

It deliberately answers nothing else.  It does not resolve, download, install,
or audit dependencies, it never reads or classifies a third-party license, it
does not claim that two builds are byte-identical, and it does not tie a file to
a source commit.  Those exclusions are written into the artifact itself as
``limitations`` so that a reader of the JSON cannot mistake a recorded value for
a verification.

Standard library only: no network access, no subprocess, and no import of the
project package, so a wheel that cannot be installed can still be inventoried.

CLI::

    distribution_inventory.py WHEEL [-o OUTPUT] [--license-file PATH]
                                    [--timestamp ISO8601] [--check]

Exit codes: ``0`` success, ``1`` check mismatch or write failure, ``2`` invalid
input (missing file, not a zip archive, no ``*.dist-info/METADATA``, unreadable
metadata, unusable arguments).
"""

from __future__ import annotations

import argparse
import email.message
import email.parser
import hashlib
import json
import re
import sys
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

SCHEMA_VERSION = "pe-sim.distribution-inventory.v1"
RULE_VERSION = "inventory-v1"
DEFAULT_OUTPUT_NAME = "distribution-inventory.json"
DEFAULT_LICENSE_NAME = "LICENSE"

_METADATA_SUFFIX = ".dist-info/METADATA"
_RECORD_SUFFIX = ".dist-info/RECORD"

_PEP503_RUNS = re.compile(r"[-_.]+")
_NAME_BOUNDARY = re.compile(r"[\s\[(<>=!~;]")
_CONNECTOR = re.compile(r"\s+(and|or)\s+", re.IGNORECASE)
_EXTRA_CLAUSE = re.compile(r'^extra\s*==\s*"([^"]*)"$')
_EXTRA_CLAUSE_REVERSED = re.compile(r'^"([^"]*)"\s*==\s*extra$')
_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:")

#: Non-claims that every inventory carries.  They are part of the artifact
#: rather than of an API, so they are written as plain sentences a reviewer can
#: read next to the values they qualify.
_LIMITATIONS = (
    "the inventory does not resolve, download, or verify dependencies or their "
    "licenses: dependency entries come from declared Requires-Dist metadata only, and "
    "every dependency license entry is 'not_verified' with source 'not_recorded'",
    "two builds of the same source are not claimed to be byte-identical, so this "
    "inventory describes one wheel file and not a reproducible build of a commit",
    "the artifact sha256 identifies one wheel file, not a source commit; tying a file "
    "to a commit requires separate provenance evidence",
    "member hashes are read from the archive as stored; the inventory does not check "
    "that the archive is installable, importable, or safe",
    "requested dependency sub-extras written as name[sub-extra] in Requires-Dist are "
    "not recorded; only the extras declared by this distribution are captured",
)
_LIMITATION_UNDECLARED_LICENSE = (
    "the wheel declares no license string; any supplied LICENSE file hash is recorded "
    "separately and no equivalence between that file and a declared license is claimed"
)
_LIMITATION_NO_LICENSE_FILE = (
    "no license file was read, so licenses.project.license_file records nulls and no "
    "license file content or hash is claimed"
)


class InventoryError(Exception):
    """Input the inventory refuses to describe.

    The CLI turns this into exit code ``2``.  It is raised only for problems the
    caller can fix, and never for a surprising-but-readable artifact.
    """


# ---------------------------------------------------------------------------
# name and path normalisation
# ---------------------------------------------------------------------------


def _normalise_name(value: str) -> str:
    """Return the PEP 503 normalised form of a project name."""

    return _PEP503_RUNS.sub("-", value.strip().lower())


def _collapse(value: str) -> str:
    return " ".join(value.split())


def _portable_path(value: str) -> str:
    """Return ``value`` with ``/`` separators and no root or drive prefix."""

    text = value.replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    if _WINDOWS_DRIVE.match(text):
        text = text[2:]
    return text.lstrip("/")


def _display_path(path: Path) -> str:
    """Return a location-free label for a license file.

    Only the file name is recorded.  A relative path would depend on the
    working directory and an absolute path would leak the machine layout, so
    neither may reach the JSON.
    """

    return _portable_path(Path(path).name)


# ---------------------------------------------------------------------------
# Requires-Dist parsing
# ---------------------------------------------------------------------------


def _normalise_quotes(value: str) -> str:
    """Rewrite every marker string literal with double quotes."""

    out: list[str] = []
    quote: str | None = None
    for char in value:
        if quote is None:
            if char in "\"'":
                quote = char
                out.append('"')
            else:
                out.append(char)
        elif char == quote:
            quote = None
            out.append('"')
        elif char == '"' and quote == "'":
            out.append('\\"')
        else:
            out.append(char)
    return "".join(out)


def _split_requirement(value: str) -> tuple[str, str | None]:
    """Split ``name<extras><specifier>; marker`` at the first bare semicolon."""

    depth = 0
    quote: str | None = None
    for index, char in enumerate(value):
        if quote is not None:
            if char == quote:
                quote = None
            continue
        if char in "\"'":
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth = max(depth - 1, 0)
        elif char == ";" and depth == 0:
            return value[:index], value[index + 1 :]
    return value, None


def _strip_outer_parens(value: str) -> str:
    """Remove parentheses that enclose the whole string."""

    text = value.strip()
    while len(text) >= 2 and text[0] == "(" and text[-1] == ")":
        depth = 0
        enclosed = True
        for index, char in enumerate(text):
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0 and index != len(text) - 1:
                    enclosed = False
                    break
        if not enclosed:
            break
        text = text[1:-1].strip()
    return text


def _split_top_level(value: str) -> list[tuple[str | None, str]]:
    """Split a marker on ``and``/``or`` outside parentheses and string literals."""

    parts: list[tuple[str | None, str]] = []
    depth = 0
    quote: str | None = None
    connector: str | None = None
    start = 0
    index = 0
    length = len(value)
    while index < length:
        char = value[index]
        if quote is not None:
            if char == quote:
                quote = None
            index += 1
            continue
        if char in "\"'":
            quote = char
            index += 1
            continue
        if char == "(":
            depth += 1
        elif char == ")":
            depth = max(depth - 1, 0)
        elif depth == 0:
            match = _CONNECTOR.match(value, index)
            if match is not None:
                parts.append((connector, value[start:index]))
                connector = match.group(1).lower()
                index = match.end()
                start = index
                continue
        index += 1
    parts.append((connector, value[start:]))
    return parts


def _flatten_clauses(value: str) -> list[tuple[str | None, str]]:
    """Return ``(connector, clause)`` pairs, looking through parentheses."""

    flattened: list[tuple[str | None, str]] = []
    for connector, chunk in _split_top_level(value):
        clause = _strip_outer_parens(chunk)
        if not clause:
            continue
        if len(_split_top_level(clause)) > 1:
            for index, (inner_connector, inner) in enumerate(_flatten_clauses(clause)):
                flattened.append((connector if index == 0 else inner_connector, inner))
        else:
            flattened.append((connector, clause))
    return flattened


def _extra_clause_name(clause: str) -> str | None:
    """Return the extra name when ``clause`` is exactly ``extra == "..."``."""

    match = _EXTRA_CLAUSE.match(clause) or _EXTRA_CLAUSE_REVERSED.match(clause)
    return match.group(1) if match is not None else None


def _parse_marker(value: str) -> tuple[str | None, str | None]:
    """Return ``(marker, extra)`` for a declared environment marker."""

    text = _normalise_quotes(_collapse(value))
    if not text:
        return None, None
    extra: str | None = None
    kept: list[tuple[str | None, str]] = []
    for connector, clause in _flatten_clauses(text):
        name = _extra_clause_name(clause)
        if name is None:
            kept.append((connector, clause))
        elif extra is None:
            extra = name
    if not kept:
        return None, extra
    marker = kept[0][1]
    for connector, clause in kept[1:]:
        marker = f"{marker} {connector or 'and'} {clause}"
    return marker or None, extra


def _parse_specifier(value: str) -> str | None:
    """Return the version constraint text, or ``None`` when there is none."""

    text = _collapse(value)
    if text.startswith("(") and text.endswith(")"):
        text = _collapse(text[1:-1])
    return text or None


def _parse_requirement(value: str) -> dict[str, Any] | None:
    requirement, marker_text = _split_requirement(_collapse(str(value)))
    match = _NAME_BOUNDARY.search(requirement)
    if match is None:
        name, rest = requirement, ""
    else:
        name, rest = requirement[: match.start()], requirement[match.start() :]
    name = _normalise_name(name)
    if not name:
        # A requirement without a usable name cannot be grouped or reported.
        return None
    rest = rest.strip()
    if rest.startswith("["):
        end = rest.find("]")
        rest = "" if end == -1 else rest[end + 1 :]
    marker, extra = (None, None) if marker_text is None else _parse_marker(marker_text)
    return {
        "name": name,
        "specifier": _parse_specifier(rest),
        "marker": marker,
        "extra": extra,
    }


def _dependency_sort_key(entry: dict[str, Any]) -> tuple[str, str, str, str]:
    """Sort key where ``None`` sorts before any text."""

    return (
        entry["name"],
        entry["specifier"] or "",
        entry["marker"] or "",
        entry["extra"] or "",
    )


def parse_requires_dist(values: list[str]) -> list[dict]:
    """Parse ``Requires-Dist`` header values into sorted dependency entries.

    Each entry is ``{"name", "specifier", "marker", "extra"}``.  The marker is
    the declared marker with double-quoted literals and with the
    ``extra == "..."`` clause removed.  Malformed values are skipped rather
    than raising, because a header the parser cannot read must not abort an
    inventory of an otherwise readable archive.
    """

    entries: list[dict[str, Any]] = []
    for value in values or []:
        parsed = _parse_requirement(str(value))
        if parsed is not None:
            entries.append(parsed)
    entries.sort(key=_dependency_sort_key)
    return entries


# ---------------------------------------------------------------------------
# archive reading
# ---------------------------------------------------------------------------


def _hash_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _name_from_filename(filename: str) -> str:
    stem = filename[: -len(".whl")] if filename.lower().endswith(".whl") else filename
    return _normalise_name(stem.split("-", 1)[0]) or _portable_path(stem)


def _load_wheel(path: Path) -> tuple[list[dict[str, Any]], str]:
    """Return ``(sorted contents, METADATA text)`` for one wheel file."""

    if not path.is_file():
        raise InventoryError(f"wheel not found: {path}")
    if not zipfile.is_zipfile(path):
        raise InventoryError(f"not a zip archive: {path}")
    contents: list[dict[str, Any]] = []
    try:
        with zipfile.ZipFile(path) as archive:
            metadata_names = sorted(
                info.filename
                for info in archive.infolist()
                if _portable_path(info.filename).endswith(_METADATA_SUFFIX)
            )
            if not metadata_names:
                raise InventoryError(f"no *{_METADATA_SUFFIX} found in {path.name}")
            for info in archive.infolist():
                if info.is_dir():
                    continue
                member = _portable_path(info.filename)
                if not member or member.endswith(_RECORD_SUFFIX):
                    continue
                data = archive.read(info)
                contents.append(
                    {
                        "path": member,
                        "sha256": hashlib.sha256(data).hexdigest(),
                        "size_bytes": len(data),
                    }
                )
            raw_metadata = archive.read(metadata_names[0])
    except InventoryError:
        raise
    except (zipfile.BadZipFile, OSError, RuntimeError, KeyError, NotImplementedError) as exc:
        raise InventoryError(f"unreadable wheel archive {path.name}: {exc}") from exc
    try:
        metadata_text = raw_metadata.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise InventoryError(f"metadata in {path.name} is not valid UTF-8: {exc}") from exc
    contents.sort(key=lambda item: item["path"])
    return contents, metadata_text


def _read_headers(text: str) -> email.message.Message:
    """Parse only the header block of a ``METADATA`` file."""

    try:
        return email.parser.HeaderParser().parsestr(text)
    except Exception as exc:  # pragma: no cover - defensive
        raise InventoryError(f"unreadable wheel metadata: {exc}") from exc


def _header(message: email.message.Message, name: str) -> str | None:
    value = message.get(name)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _canonical_digest(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _license_file_entry(license_file: Path | str | None) -> dict[str, Any]:
    if license_file is None:
        return {"path": None, "sha256": None}
    candidate = Path(license_file)
    if not candidate.is_file():
        # A missing file is recorded as absent, never as an unverified guess.
        return {"path": None, "sha256": None}
    digest, _ = _hash_file(candidate)
    return {"path": _display_path(candidate), "sha256": digest}


# ---------------------------------------------------------------------------
# inventory assembly
# ---------------------------------------------------------------------------


def build_inventory(
    wheel_path: Path | str,
    *,
    license_file: Path | str | None = None,
    generated_at_utc: str | None = None,
) -> dict:
    """Build the ``pe-sim.distribution-inventory.v1`` mapping for one wheel.

    ``generated_at_utc`` is copied into the artifact verbatim and defaults to
    ``None``, so the same wheel yields an identical mapping on every call.

    Raises ``InventoryError`` when the input is not a readable wheel, which is
    the only failure the CLI reports as invalid input.
    """

    path = Path(wheel_path)
    contents, metadata_text = _load_wheel(path)
    message = _read_headers(metadata_text)

    declared_name = _header(message, "Name") or _name_from_filename(path.name)
    declared_license = _header(message, "License-Expression") or _header(message, "License")
    dependencies = parse_requires_dist(list(message.get_all("Requires-Dist") or []))

    runtime = [entry for entry in dependencies if entry["extra"] is None]
    grouped: dict[str, list[dict[str, Any]]] = {}
    for entry in dependencies:
        extra = entry["extra"]
        if extra is None:
            continue
        grouped.setdefault(extra, []).append(entry)
    extras = {
        extra: sorted(grouped[extra], key=_dependency_sort_key)
        for extra in sorted(grouped)
    }

    wheel_sha256, wheel_size = _hash_file(path)
    license_entry = _license_file_entry(license_file)

    limitations = list(_LIMITATIONS)
    if declared_license is None:
        limitations.append(_LIMITATION_UNDECLARED_LICENSE)
    if license_entry["sha256"] is None:
        limitations.append(_LIMITATION_NO_LICENSE_FILE)

    runtime_names = sorted({entry["name"] for entry in runtime})

    return {
        "schema_version": SCHEMA_VERSION,
        "rule_version": RULE_VERSION,
        "generated_at_utc": generated_at_utc,
        "distribution": {
            "name": _normalise_name(declared_name),
            "version": _header(message, "Version"),
            "requires_python": _header(message, "Requires-Python"),
            "metadata_license": declared_license,
            "artifact": {
                "filename": path.name,
                "sha256": wheel_sha256,
                "size_bytes": wheel_size,
            },
        },
        "contents": contents,
        "contents_digest": _canonical_digest(contents),
        "dependencies": {"runtime": runtime, "extras": extras},
        "licenses": {
            "project": {
                "declared": declared_license,
                "declared_source": (
                    "artifact_metadata" if declared_license is not None else "not_recorded"
                ),
                "license_file": license_entry,
            },
            "dependencies": [
                {
                    "name": name,
                    "declared": None,
                    "source": "not_recorded",
                    "status": "not_verified",
                }
                for name in runtime_names
            ],
        },
        "limitations": limitations,
    }


def _render(data: Any) -> bytes:
    return (
        json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")


def write_inventory(data: dict, path: Path | str) -> Path:
    """Write ``data`` as canonical JSON without a BOM and return the path."""

    target = Path(path)
    payload = _render(data)
    parent = target.parent
    if str(parent):
        parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)
    return target


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _error(message: str, code: int) -> int:
    sys.stderr.write(f"distribution_inventory: error: {message}\n")
    return code


def _write_stdout(payload: bytes) -> None:
    buffer = getattr(sys.stdout, "buffer", None)
    if buffer is not None:
        buffer.write(payload)
        buffer.flush()
    else:  # pragma: no cover - only for text-only stdout replacements
        sys.stdout.write(payload.decode("utf-8"))


def _valid_timestamp(value: str) -> bool:
    text = value.strip()
    if not text:
        return False
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        datetime.fromisoformat(text)
    except ValueError:
        return False
    return True


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="distribution_inventory.py",
        description=(
            "Record a deterministic inventory of a built wheel: the wheel hash, every "
            "archive member hash, the declared Requires-Dist entries, and the declared "
            "license. Dependencies and their licenses are never resolved or verified."
        ),
        epilog=(
            "Exit codes: 0 success, 1 check mismatch or write failure, 2 invalid input."
        ),
    )
    parser.add_argument("wheel", metavar="WHEEL", help="built wheel file to inventory")
    parser.add_argument(
        "-o",
        "--output",
        default=None,
        metavar="OUTPUT",
        help=(
            "output path, or '-' for stdout; defaults to "
            f"<wheel directory>/{DEFAULT_OUTPUT_NAME}"
        ),
    )
    parser.add_argument(
        "--license-file",
        default=None,
        metavar="PATH",
        help=(
            "license file whose hash is recorded; defaults to ./"
            f"{DEFAULT_LICENSE_NAME} when that file exists, otherwise nulls are recorded"
        ),
    )
    parser.add_argument(
        "--timestamp",
        default=None,
        metavar="ISO8601",
        help="value for generated_at_utc; omitted means null and a fully deterministic file",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="rebuild the inventory and compare it byte-for-byte with the existing output file",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.timestamp is not None and not _valid_timestamp(args.timestamp):
        return _error(f"--timestamp is not an ISO 8601 date-time: {args.timestamp!r}", 2)

    license_path: Path | None
    if args.license_file is None:
        candidate = Path.cwd() / DEFAULT_LICENSE_NAME
        license_path = candidate if candidate.is_file() else None
    else:
        license_path = Path(args.license_file)
        if not license_path.is_file():
            return _error(f"license file not found: {license_path}", 2)

    wheel_path = Path(args.wheel)
    if args.output is None:
        output: Path | None = wheel_path.parent / DEFAULT_OUTPUT_NAME
    elif args.output == "-":
        output = None
    else:
        output = Path(args.output)

    if args.check and output is None:
        return _error("--check needs a file output; '-' cannot be compared", 2)

    try:
        data = build_inventory(
            wheel_path,
            license_file=license_path,
            generated_at_utc=args.timestamp,
        )
    except InventoryError as exc:
        return _error(str(exc), 2)

    payload = _render(data)

    if args.check:
        assert output is not None  # narrowed by the guard above
        try:
            existing = output.read_bytes()
        except OSError:
            return _error(f"no existing inventory to check at {output}", 1)
        if existing != payload:
            return _error(
                f"{output} does not match a fresh inventory of {wheel_path.name}",
                1,
            )
        return 0

    if output is None:
        try:
            _write_stdout(payload)
        except OSError:
            # A reader that closes the pipe early must not surface as a
            # traceback; the inventory itself was built successfully.
            return 1
        return 0

    try:
        write_inventory(data, output)
    except OSError as exc:
        return _error(f"cannot write {output}: {exc}", 1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

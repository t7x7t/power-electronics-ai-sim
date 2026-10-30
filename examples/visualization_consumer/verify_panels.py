#!/usr/bin/env python3
"""Validate the visualization consumer's panel manifest (``demo-panels-v1``).

Read-only and standard library only.  Exit 0 when the manifest is usable, 1
otherwise, so a caller can use it as a gate.

The manifest decides how many panels exist and what each one may read.  It is
deliberately marked ``demo-panels-v1`` rather than ``pe-sim.*``: it is the
example host's own configuration, not a kernel envelope, and a tag nobody
produces would be a promise nobody keeps (``docs/68`` section 4).

A panel module is display code.  It may only render the context it is handed,
so the scan here refuses network and storage tokens rather than trying to
sandbox them: the boundary is stated, not enforced by isolation
(``docs/57`` section 3).

Usage::

    python examples/visualization_consumer/verify_panels.py [panels.json]
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
MARKER = "demo-panels-v1"
NEEDS = ("trace", "topology", "outputs", "parameters", "review")
ID_RE = re.compile(r"^[a-z][a-z0-9-]{1,39}$")
ENTRY_RE = re.compile(r"^[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*\.js$")
FORBIDDEN = (
    "fetch(",
    "http://",
    "https://",
    "method:",
    "POST",
    "PUT",
    "DELETE",
    "XMLHttpRequest",
    "sendBeacon",
    "localStorage",
    "sessionStorage",
    "document.cookie",
    "writeFile",
    "appendFile",
)


def check(path: Path) -> list[str]:
    """Return every problem found in ``path``; an empty list means usable."""

    if not path.is_file():
        return [f"{path}: missing manifest (the host falls back to the built-in panels)"]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        return [f"{path}: not valid UTF-8 JSON: {exc}"]
    if not isinstance(data, dict):
        return [f"{path}: top level must be an object; got {type(data).__name__}"]
    problems: list[str] = []
    if data.get("marker") != MARKER:
        problems.append(f"marker: expected {MARKER!r}; got {data.get('marker')!r}")
    panels = data.get("panels")
    if not isinstance(panels, list) or not panels:
        return problems + [f"panels: expected a non-empty list; got {panels!r}"]
    seen: set[str] = set()
    for index, panel in enumerate(panels):
        label = f"panels[{index}]"
        if not isinstance(panel, dict):
            problems.append(f"{label}: expected an object; got {type(panel).__name__}")
            continue
        ident = panel.get("id")
        if not isinstance(ident, str) or not ID_RE.match(ident):
            problems.append(f"{label}.id: expected a lowercase slug; got {ident!r}")
        elif ident in seen:
            problems.append(f"{label}.id: duplicate {ident!r}")
        else:
            seen.add(ident)
        if not (isinstance(panel.get("title"), str) and panel["title"].strip()):
            problems.append(f"{label}.title: expected a non-empty string")
        needs = panel.get("needs")
        if not isinstance(needs, list) or not needs:
            problems.append(f"{label}.needs: expected a non-empty list; got {needs!r}")
        else:
            unknown = sorted({need for need in needs if need not in NEEDS})
            if unknown or len(set(needs)) != len(needs):
                problems.append(
                    f"{label}.needs: unknown {unknown} / repeated {needs!r}; vocabulary {list(NEEDS)}"
                )
        extra = sorted(set(panel) - {"id", "title", "needs", "entry", "_comment"})
        if extra:
            problems.append(f"{label}: unknown keys {extra}")
        entry = panel.get("entry")
        if entry is None:
            continue
        if not isinstance(entry, str) or not ENTRY_RE.match(entry):
            problems.append(f"{label}.entry: expected a relative .js path; got {entry!r}")
            continue
        target = (path.parent / entry).resolve()
        if path.parent.resolve() not in target.parents:
            problems.append(f"{label}.entry: must stay inside {path.parent}")
        elif not target.is_file():
            problems.append(f"{label}.entry: missing file {target}")
        else:
            text = target.read_text(encoding="utf-8", errors="replace")
            hits = [token for token in FORBIDDEN if token in text]
            if hits:
                problems.append(
                    f"{label}.entry {entry}: forbidden tokens {hits}; panels must render only"
                )
    return problems


def main(argv: list[str]) -> int:
    path = Path(argv[1]) if len(argv) > 1 else HERE / "panels.json"
    problems = check(path)
    for problem in problems:
        print(f"FAIL {problem}")
    if problems:
        return 1
    count = len(json.loads(path.read_text(encoding="utf-8"))["panels"])
    print(f"OK {path}: {count} panels, marker {MARKER}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

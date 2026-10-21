"""Fake ngspice executable used only by process-runner tests.

The script never simulates anything.  It exercises the failure classification
of ``NgspiceProcess`` without requiring a real simulator installation.
"""

from __future__ import annotations

import os
from pathlib import Path
import re
import sys
import time


_TRAN_RE = re.compile(r"^\.tran\s+(\S+)\s+(\S+)", re.MULTILINE)


def _window_duration(argv: list[str]) -> float:
    for argument in reversed(argv):
        if argument.endswith(".cir"):
            path = Path(argument)
            if path.is_file():
                match = _TRAN_RE.search(path.read_text(encoding="utf-8", errors="replace"))
                if match:
                    return float(match.group(2))
    return 1e-6


def _write_success_table(duration: float) -> None:
    points = 21
    rows = ["v(vout) i(l1) v(sw)"]
    for index in range(points):
        time_s = duration * index / (points - 1)
        rows.append(
            f"{time_s:.12e} {3.0 + 2.0 * index / (points - 1):.12e} "
            f"{1.0 + 1.0 * index / (points - 1):.12e} "
            f"{(12.0 if index % 4 < 2 else 0.0):.12e}"
        )
    Path("output.dat").write_text("\n".join(rows) + "\n", encoding="utf-8")


def main(argv: list[str]) -> int:
    if "--version" in argv:
        print("** ngspice-44.2 : Circuit level simulation program (fake)")
        return 0

    mode = os.environ.get("FAKE_NGSPICE_MODE", "success")
    log_path: Path | None = None
    if "-o" in argv:
        log_path = Path(argv[argv.index("-o") + 1])

    if mode == "timeout":
        time.sleep(30)
        return 0
    if mode == "convergence":
        if log_path is not None:
            log_path.write_text(
                "doAnalyses: timestep too small; iteration limit reached\n",
                encoding="utf-8",
            )
        return 0
    if mode == "process_error":
        if log_path is not None:
            log_path.write_text("Error: unknown subckt: xfoo\n", encoding="utf-8")
        return 1
    if mode == "no_output":
        return 0
    if mode == "garbage":
        Path("output.dat").write_text("this is not a table\n", encoding="utf-8")
        return 0

    _write_success_table(_window_duration(argv))
    if log_path is not None:
        log_path.write_text("ngspice fake success\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

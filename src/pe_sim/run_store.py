"""Resolve the directory discovery searches for run packages.

A run package is always *written* where its experiment document says
(``output.directory``); this module does not change that.  It answers the
narrower reader question - "which directory should discovery search?" - so
the answer is one documented order instead of whatever the process happened
to inherit:

1. an explicit path supplied by the caller;
2. ``PE_SIM_RUNS_ROOT`` when it holds a non-blank value;
3. ``runs`` relative to the working directory.

Resolution only: nothing here creates, repairs, enumerates, or verifies a
directory.  A caller that receives an unreadable path reports that error
itself.
"""

from __future__ import annotations

import os
from pathlib import Path

#: Environment variable that relocates discovery without a CLI argument.
RUNS_ROOT_ENV = "PE_SIM_RUNS_ROOT"

#: Working-directory default, kept relative so a checkout stays portable.
DEFAULT_RUNS_DIRNAME = "runs"


def discovery_root(explicit: str | Path | None = None) -> Path:
    """Return the directory discovery should scan, without touching the disk.

    An explicit path wins and keeps the caller's spelling (after ``~``
    expansion) so error messages echo what the user typed.  A blank explicit
    value is treated as "not provided".  An environment value is stripped
    first, because a trailing newline from a shell script must not become part
    of the path; a blank value means "not configured".
    """

    if explicit is not None and str(explicit).strip():
        return Path(explicit).expanduser()
    configured = os.environ.get(RUNS_ROOT_ENV, "")
    if configured.strip():
        return Path(configured.strip()).expanduser()
    return Path(DEFAULT_RUNS_DIRNAME)


__all__ = ["DEFAULT_RUNS_DIRNAME", "RUNS_ROOT_ENV", "discovery_root"]

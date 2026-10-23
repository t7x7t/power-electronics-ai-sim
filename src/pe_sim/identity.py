"""Reviewed implementation identity for the shipped solve paths.

The digest covers the source files that determine how a reviewed window is
constructed, executed, and parsed.  It is intentionally narrower than a package
digest: it answers "did the numerical solve path change?" rather than "did any
file in the repository change?".

The same mechanism covers the external-simulator adapter (``ngspice-buck``)
and the project-owned averaged reference plants (``buck``/``boost``), so the
default example path is reviewed with the same strength as the simulator path.

``reviewed_identities.json`` is a review pin, not a security boundary.  A local
process with write access can change both the code and the pin; the pin makes
such a change visible in Git review.  See ``docs/38_kernel_review_v0.md``.
"""

from __future__ import annotations

import hashlib
import json
from importlib import resources
from pathlib import Path, PurePosixPath
from typing import Any, Mapping


REVIEW_IDENTITIES_SCHEMA = "pe-sim.reviewed-identities.v1"

# Solve paths are declared relative to the ``pe_sim`` package root, so the
# native reference models and the external-simulator adapter use one
# declaration shape.  The returned mapping is still keyed by the plain module
# file name: those keys are part of the aggregate digest that was already
# reviewed, and renaming them would silently invalidate existing pins.
#
# This module lives at the package root on purpose.  It is a general review
# mechanism, not an adapter detail: core modules (``reference_plants``,
# ``inspection``) need it, and they must not depend on the external-simulator
# package to get it.
_PACKAGE_ROOT = Path(__file__).resolve().parent

# Adding or removing a module here is a model-review decision: it changes the
# implementation digest for every backend that uses this solve path.
_NGSPICE_SOLVE_PATH = (
    "backends/ngspice_plants.py",
    "backends/netlist.py",
    "backends/netlist_builder.py",
    "backends/process.py",
    "backends/settings.py",
    "backends/wrdata.py",
)

# The native averaged reference plants are the default example and test path.
# Both directions share one module, so they intentionally share one digest.
_REFERENCE_PLANT_SOLVE_PATH = ("reference_plants.py",)

_SOLVE_PATHS: dict[str, tuple[str, ...]] = {
    "ngspice-buck": _NGSPICE_SOLVE_PATH,
    "buck": _REFERENCE_PLANT_SOLVE_PATH,
    "boost": _REFERENCE_PLANT_SOLVE_PATH,
}


def implementation_sources(backend_id: str) -> dict[str, str]:
    """Return the normalized SHA-256 of every module in the solve path."""

    modules = _SOLVE_PATHS.get(backend_id)
    if modules is None:
        raise KeyError(f"no reviewed solve path is declared for backend {backend_id!r}")
    result: dict[str, str] = {}
    for relative in modules:
        path = _PACKAGE_ROOT / relative
        if not path.is_file():
            raise FileNotFoundError(f"declared solve-path module is missing: {path}")
        name = PurePosixPath(relative).name
        if name in result:
            raise ValueError(f"duplicate solve-path module name in backend {backend_id!r}: {name}")
        # A Git checkout may use CRLF on Windows.  The identity is a source
        # identity, not a checkout-format identity.
        content = path.read_bytes().replace(b"\r\n", b"\n")
        result[name] = hashlib.sha256(content).hexdigest()
    return result


def implementation_digest(backend_id: str) -> str:
    """Return the aggregate digest of the declared solve path."""

    sources = implementation_sources(backend_id)
    payload = json.dumps(sources, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def load_reviewed_identities() -> dict[str, Any]:
    """Read the checked-in review pin without making it a hard dependency."""

    try:
        text = resources.files("pe_sim").joinpath("reviewed_identities.json").read_text(encoding="utf-8")
    except (FileNotFoundError, ModuleNotFoundError):
        return {"schema": REVIEW_IDENTITIES_SCHEMA, "backends": {}}
    document = json.loads(text)
    if not isinstance(document, dict) or document.get("schema") != REVIEW_IDENTITIES_SCHEMA:
        raise ValueError("reviewed_identities.json has an unsupported schema")
    backends = document.get("backends")
    if not isinstance(backends, dict):
        raise ValueError("reviewed_identities.json backends must be an object")
    return document


def review_status(backend_id: str, digest: str | None = None) -> dict[str, Any]:
    """Compare one implementation digest with the checked-in review pin.

    ``not_pinned`` means no reviewed value exists for this backend.  It is not
    an approval and must not be presented as one.
    """

    actual = digest if digest is not None else implementation_digest(backend_id)
    entry = load_reviewed_identities().get("backends", {}).get(backend_id)
    expected = entry.get("implementation_sha256") if isinstance(entry, Mapping) else None
    if not isinstance(expected, str) or len(expected) != 64:
        status = "not_pinned"
    elif expected.lower() == actual.lower():
        status = "reviewed"
    else:
        status = "unreviewed"
    return {
        "backend_id": backend_id,
        "implementation_sha256": actual,
        "reviewed_implementation_sha256": expected,
        "status": status,
    }


__all__ = [
    "REVIEW_IDENTITIES_SCHEMA",
    "implementation_digest",
    "implementation_sources",
    "load_reviewed_identities",
    "review_status",
]

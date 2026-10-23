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
import inspect
import json
from importlib import resources
from pathlib import Path, PurePosixPath
from typing import Any, Mapping


REVIEW_IDENTITIES_SCHEMA = "pe-sim.reviewed-identities.v1"
ADAPTER_VERIFICATION_SCHEMA = "pe-sim.adapter-verification.v1"

# Statuses that mean the adapter's own claim is contradicted by what the
# kernel can recompute.  They are never reported as `verified`.
CONTRADICTED_VERIFICATION_STATUSES = frozenset(
    {"digest_mismatch", "review_status_mismatch", "module_outside_solve_path"}
)

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


def verify_adapter_identity(component: Any, identity: Mapping[str, Any]) -> dict[str, Any]:
    """Recompute a Plant adapter's implementation identity instead of trusting it.

    ``manifest_identity()`` is *producer-declared*: whatever object is passed to
    the Runner supplies its own ``implementation.sha256`` and review record.  A
    kernel that only reads those fields cannot distinguish a reviewed adapter
    from an object that merely claims to be one.

    This function closes that gap for any adapter that declares ``backend_id``
    and whose declaring module is covered by the reviewed solve path:

    - ``verified``: the recorded digest equals the digest recomputed from the
      files on disk, the recorded review status equals the kernel's own lookup,
      and the adapter's class is defined inside the declared solve path.
    - ``digest_mismatch`` / ``review_status_mismatch``: the claim contradicts
      the checkout.
    - ``module_outside_solve_path``: the adapter's module is not part of the
      reviewed solve path, so the pin does not cover the code that runs.
    - ``unverifiable``: no ``backend_id`` or no declared solve path; the kernel
      says so instead of pretending the claim was checked.

    The check is a consistency check, not a sandbox: a process that can rewrite
    ``identity.py`` can also rewrite this function.
    """

    backend_id = getattr(component, "backend_id", None)
    if not isinstance(backend_id, str) or not backend_id.strip():
        return {
            "schema_version": ADAPTER_VERIFICATION_SCHEMA,
            "status": "unverifiable",
            "backend_id": None,
            "reasons": ["adapter does not declare backend_id"],
        }

    result: dict[str, Any] = {
        "schema_version": ADAPTER_VERIFICATION_SCHEMA,
        "status": "unverifiable",
        "backend_id": backend_id,
        "declared_module": type(component).__module__,
        "declared_class": type(component).__qualname__,
        "recorded_sha256": None,
        "recomputed_sha256": None,
        "recorded_review_status": None,
        "recomputed_review_status": None,
        "reasons": [],
    }

    implementation = identity.get("implementation") if isinstance(identity, Mapping) else None
    implementation = implementation if isinstance(implementation, Mapping) else {}
    recorded = implementation.get("sha256")
    result["recorded_sha256"] = recorded if isinstance(recorded, str) else None
    review = implementation.get("review")
    review = review if isinstance(review, Mapping) else {}
    recorded_status = review.get("status")
    result["recorded_review_status"] = recorded_status if isinstance(recorded_status, str) else None

    try:
        sources = implementation_sources(backend_id)
    except (KeyError, FileNotFoundError):
        result["reasons"].append(f"no reviewed solve path is declared for backend {backend_id!r}")
        return result

    result["solve_path_modules"] = sorted(sources)
    recomputed = implementation_digest(backend_id)
    result["recomputed_sha256"] = recomputed
    recomputed_review = review_status(backend_id, recomputed)
    result["recomputed_review_status"] = recomputed_review.get("status")

    if result["recorded_sha256"] is None:
        result["reasons"].append("adapter does not record an implementation digest")
        return result
    if result["recorded_sha256"].lower() != recomputed.lower():
        result["status"] = "digest_mismatch"
        result["reasons"].append(
            "the recorded implementation digest does not match the solve-path source on disk"
        )
        return result
    if result["recorded_review_status"] != result["recomputed_review_status"]:
        result["status"] = "review_status_mismatch"
        result["reasons"].append(
            "the recorded review status does not match the checked-in pin"
        )
        return result

    try:
        module_file = Path(inspect.getfile(type(component))).name
    except (TypeError, OSError):  # pragma: no cover - builtins and frozen modules
        module_file = None
    result["module_file"] = module_file
    if module_file is None or module_file not in sources:
        result["status"] = "module_outside_solve_path"
        result["reasons"].append(
            "the adapter class is defined outside the declared solve path, "
            "so the review pin does not cover the code that runs"
        )
        return result

    result["status"] = "verified"
    return result


__all__ = [
    "ADAPTER_VERIFICATION_SCHEMA",
    "CONTRADICTED_VERIFICATION_STATUSES",
    "REVIEW_IDENTITIES_SCHEMA",
    "implementation_digest",
    "implementation_sources",
    "load_reviewed_identities",
    "review_status",
    "verify_adapter_identity",
]

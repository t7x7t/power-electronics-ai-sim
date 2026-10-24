"""Reviewed implementation identity for declared solve paths.

The digest covers the source files that determine how a reviewed window is
constructed, executed, and parsed.  It is intentionally narrower than a package
digest: it answers "did the numerical solve path change?" rather than "did any
file in the repository change?".

This module does not name any engine or model.  A backend declares its solve
path as data, either in the checked-in review pin (``reviewed_identities.json``)
or at runtime through :func:`register_solve_path`, and the digest is computed
from whatever the declaration lists.

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
from types import MappingProxyType
from typing import Any, Iterable, Mapping


REVIEW_IDENTITIES_SCHEMA = "pe-sim.reviewed-identities.v1"
ADAPTER_VERIFICATION_SCHEMA = "pe-sim.adapter-verification.v1"

# Statuses that mean the adapter's own claim is contradicted by what the
# kernel can recompute.  They are never reported as `verified`.
CONTRADICTED_VERIFICATION_STATUSES = frozenset(
    {"digest_mismatch", "review_status_mismatch", "module_outside_solve_path"}
)

# Solve paths are declared relative to the ``pe_sim`` package root, so a native
# model and an external-simulator adapter use one declaration shape.  The
# returned mapping is still keyed by the plain module file name: those keys are
# part of the aggregate digest that was already reviewed, and renaming them
# would silently invalidate existing pins.
#
# This module lives at the package root on purpose.  It is a general review
# mechanism, not an adapter detail: core modules (``reference_plants``,
# ``inspection``) need it, and they must not depend on any adapter package.
_PACKAGE_ROOT = Path(__file__).resolve().parent

# Field name a review-pin entry uses to declare its solve path.
_SOLVE_PATH_FIELD = "solve_path"

# Runtime declarations.  They are memory only: writing to the checked-in pin is
# a review decision, so a third party declares its path in its own process.
_MEMORY_SOLVE_PATHS: dict[str, tuple[str, ...]] = {}


def _normalized_modules(backend_id: str, modules: Iterable[str]) -> tuple[str, ...]:
    """Validate one declaration into package-root-relative POSIX paths."""

    if isinstance(modules, str):
        raise TypeError(
            f"solve path for backend {backend_id!r} must be a sequence of module paths, not a string"
        )
    normalized: list[str] = []
    for module in modules:
        if not isinstance(module, str) or not module.strip():
            raise ValueError(f"backend {backend_id!r} solve path contains an empty module name")
        relative = PurePosixPath(module.strip().replace("\\", "/"))
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError(
                f"backend {backend_id!r} solve path must be relative to the package root: {module!r}"
            )
        normalized.append(relative.as_posix())
    if not normalized:
        raise ValueError(f"backend {backend_id!r} solve path must list at least one module")
    return tuple(normalized)


def register_solve_path(backend_id: str, modules: Iterable[str]) -> None:
    """Declare a backend's solve path for this process only.

    The checked-in pin is deliberately not modified: a runtime declaration is
    enough to compute and compare a digest, but pinning a value is a review
    decision that belongs in version control.
    """

    if not isinstance(backend_id, str) or not backend_id.strip():
        raise ValueError("backend_id must be a non-empty string")
    _MEMORY_SOLVE_PATHS[backend_id] = _normalized_modules(backend_id, modules)


def _reviewed_solve_paths() -> dict[str, tuple[str, ...]]:
    """Read the declared solve path of every entry in the checked-in pin."""

    declared: dict[str, tuple[str, ...]] = {}
    backends = load_reviewed_identities().get("backends", {})
    for backend_id, entry in backends.items():
        if not isinstance(entry, Mapping):
            continue
        modules = entry.get(_SOLVE_PATH_FIELD)
        if modules is None:
            # A pin entry without a solve path still carries review metadata;
            # it simply cannot be recomputed from source.
            continue
        if not isinstance(modules, (list, tuple)):
            raise ValueError(
                f"reviewed_identities.json backend {backend_id!r} {_SOLVE_PATH_FIELD} "
                "must be an array of module paths"
            )
        declared[str(backend_id)] = _normalized_modules(str(backend_id), modules)
    return declared


def declared_solve_paths() -> Mapping[str, tuple[str, ...]]:
    """Return the checked-in declarations merged with runtime registrations.

    A runtime registration wins over the pin, so a process can describe code it
    has not pinned yet without editing the reviewed file.
    """

    merged = _reviewed_solve_paths()
    merged.update(_MEMORY_SOLVE_PATHS)
    return MappingProxyType(merged)


def implementation_sources(backend_id: str) -> dict[str, str]:
    """Return the normalized SHA-256 of every module in the solve path."""

    modules = declared_solve_paths().get(backend_id)
    if modules is None:
        raise KeyError(
            f"no solve path is declared for backend {backend_id!r}; declare one with "
            "pe_sim.identity.register_solve_path() or add a 'solve_path' array to the "
            "backend entry in reviewed_identities.json"
        )
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
    "declared_solve_paths",
    "implementation_digest",
    "implementation_sources",
    "load_reviewed_identities",
    "register_solve_path",
    "review_status",
    "verify_adapter_identity",
]

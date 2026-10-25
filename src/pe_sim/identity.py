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

import functools
import hashlib
import inspect
import json
import sys
from importlib import resources
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any, Iterable, Iterator, Mapping


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


def _declared_solve_path_files(modules: Iterable[str]) -> tuple[str, ...]:
    """Return the declared solve-path files as package-relative POSIX paths.

    The digest mapping above is keyed by the plain module file name because
    those keys are part of an already-reviewed digest.  A name is not a
    location, though: a file with the same name in another directory would
    pass a basename check.  Verification therefore resolves each declared file
    and compares resolved paths, while the recorded evidence stays portable:
    the manifest must not embed an absolute, machine-specific path.
    """

    return tuple(sorted({PurePosixPath(relative).as_posix() for relative in modules}))


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


def _bound_class(component_type: Any) -> Any:
    """Return the object actually bound at ``__module__``/``__qualname__``.

    ``__module__`` is a plain writable attribute, and ``inspect.getfile()`` reads
    it, so a class defined anywhere can *claim* to live in a reviewed module and
    make a path check agree with the claim.  Resolving the qualified name
    through ``sys.modules`` is an independent source of truth: forging a name
    cannot make the reviewed module bind a foreign class object.

    ``None`` means the name does not resolve to any object (including a class
    defined inside a function, whose ``<locals>`` segment no module binds).
    """

    module = sys.modules.get(getattr(component_type, "__module__", None))
    if module is None:
        return None
    bound: Any = module
    for part in str(getattr(component_type, "__qualname__", "")).split("."):
        if part == "<locals>":
            return None
        bound = getattr(bound, part, None)
        if bound is None:
            return None
    return bound


def _callable_source_file(value: Any) -> Path | None:
    """Return the file a class-level callable's code was compiled from.

    Only shapes that carry a real code object are reported.  Wrapping shapes
    (``staticmethod``, ``classmethod``, ``property``, ``functools.partial``, a
    bound method) are unwrapped first, because the wrapper object is not where
    the code lives.  Anything without a code object yields ``None`` and is not
    judged: a C-level callable has no source file to compare.
    """

    seen: set[int] = set()
    stack: list[Any] = [value]
    while stack:
        current = stack.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, (staticmethod, classmethod)):
            stack.append(current.__func__)
        elif isinstance(current, property):
            stack.extend(part for part in (current.fget, current.fset, current.fdel) if part is not None)
        elif isinstance(current, functools.partial):
            stack.append(current.func)
        else:
            code = getattr(getattr(current, "__func__", current), "__code__", None)
            if code is not None:
                return Path(code.co_filename)
    return None


def _own_code_entries(component_type: Any) -> Iterator[tuple[str, Path]]:
    """Yield ``(attribute, source file)`` for the class's own code.

    Only the concrete class's ``__dict__`` is inspected.  Inherited attributes
    belong to base classes; a reviewed adapter is a specific class, and its
    bases are either part of the same declared file or are reached through the
    same rule when they are the declared adapter.
    """

    for name, value in vars(component_type).items():
        source = _callable_source_file(value)
        if source is not None:
            yield name, source


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
      and the adapter's class is defined in one of the declared solve-path
      files.
    - ``digest_mismatch`` / ``review_status_mismatch``: the claim contradicts
      the checkout.
    - ``module_outside_solve_path``: the pin does not cover the code that runs.
      Three independent conditions decide this, and all three must hold:

      1. ``inspect.getfile()`` resolves into a declared solve-path file.  The
         comparison is between resolved paths, not module file names: a
         same-named file elsewhere (even with identical content) does not count.
      2. The declared module really binds this class object under its declared
         qualified name.  ``__module__`` is a writable attribute that
         ``inspect.getfile()`` trusts, so condition 1 alone can be satisfied by
         a class that merely names a reviewed module.
      3. Every function-like attribute the class defines itself was compiled
         from a declared file.  This rejects a foreign class rebound into a
         reviewed module's namespace, and a reviewed class whose methods were
         replaced at runtime.

      A class that fails only because it lives outside the package records
      ``module_path = null``; a class that fails condition 2 or 3 records the
      claimed path plus ``class_bound_in_declared_module`` and
      ``class_code_outside_solve_path`` so the contradiction is visible.
    - ``unverifiable``: no ``backend_id`` or no declared solve path; the kernel
      says so instead of pretending the claim was checked.

    ``module_file`` (the file name) and ``solve_path_modules`` (the digest keys)
    are kept for compatibility.  ``module_path`` and ``solve_path_files`` show
    the resolved paths the location check actually compared, so an auditor can
    reproduce the decision.  ``class_bound_in_declared_module`` and
    ``class_code_files`` / ``class_code_outside_solve_path`` do the same for the
    module-namespace and class-body checks, and stay portable: they carry
    declared relative paths and attribute names, never absolute paths.

    The check is a consistency check, not a sandbox: a process that can rewrite
    ``identity.py`` can also rewrite this function.  Two adjacent cases are
    deliberately outside the boundary.  Instance attributes are not judged:
    the caller supplies the object, so rebinding ``plant.step`` after
    construction is the caller's own act, equivalent to passing a different
    object.  Callables without a code object (C extensions, some descriptors)
    have no source file to compare and are skipped rather than guessed at.
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
        "module_file": None,
        "module_path": None,
        "solve_path_modules": [],
        "solve_path_files": [],
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
    declared_files = _declared_solve_path_files(declared_solve_paths().get(backend_id, ()))
    # Resolved path -> declared relative path, so a decision can name the file
    # it accepted without ever recording an absolute, machine-specific path.
    resolved_files = {
        (_PACKAGE_ROOT / relative).resolve(strict=False): relative
        for relative in declared_files
    }
    result["solve_path_files"] = list(declared_files)
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

    component_type = type(component)
    try:
        module_path = Path(inspect.getfile(component_type)).resolve(strict=False)
    except (TypeError, OSError):  # pragma: no cover - builtins and frozen modules
        module_path = None
    result["module_file"] = module_path.name if module_path is not None else None
    try:
        result["module_path"] = (
            module_path.relative_to(_PACKAGE_ROOT.resolve()).as_posix()
            if module_path is not None
            else None
        )
    except ValueError:
        result["module_path"] = None

    # The path above is the one ``__module__`` *claims*.  Confirm independently
    # that the claimed module really binds this class object, then that the
    # class's own code was compiled from declared files.
    bound_is_component_type = _bound_class(component_type) is component_type
    result["class_bound_in_declared_module"] = bound_is_component_type
    accepted_files: set[str] = set()
    foreign_attributes: list[str] = []
    for attribute, source in _own_code_entries(component_type):
        relative = resolved_files.get(source.resolve(strict=False))
        if relative is None:
            foreign_attributes.append(attribute)
        else:
            accepted_files.add(relative)
    result["class_code_files"] = sorted(accepted_files)
    result["class_code_outside_solve_path"] = sorted(set(foreign_attributes))

    if module_path is None or module_path not in resolved_files:
        result["status"] = "module_outside_solve_path"
        result["reasons"].append(
            "the adapter class is defined outside the declared solve path, "
            "so the review pin does not cover the code that runs"
        )
    if not bound_is_component_type:
        result["status"] = "module_outside_solve_path"
        result["reasons"].append(
            "the adapter class is not the object bound as "
            f"{result['declared_class']!r} in module {result['declared_module']!r}; "
            "a class can name any module, so the name alone is not evidence of "
            "where its code lives"
        )
    if foreign_attributes:
        result["status"] = "module_outside_solve_path"
        result["reasons"].append(
            "class attributes "
            f"{sorted(set(foreign_attributes))} are compiled from files outside the "
            "declared solve path, so the review pin does not cover the code that runs"
        )
    if result["status"] == "module_outside_solve_path":
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

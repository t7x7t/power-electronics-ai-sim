"""Small JSON-normalisation helpers shared by evidence-producing modules.

Evidence must never contain live objects, non-finite floats, machine-local
paths, or values that change between processes.  These helpers deliberately
return ``None`` for anything they cannot represent instead of raising, so a
poorly behaved plugin cannot break a run merely by returning an unusual
diagnostic value.
"""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
import hashlib
import json
import math
from typing import Any, Mapping


_MAX_DEPTH = 8


def json_safe(value: Any, *, _depth: int = 0) -> Any:
    """Return a JSON-serialisable view of ``value``.

    ``float('nan')`` and infinities become ``None``; unknown objects are
    dropped.  Mapping keys are converted to strings.
    """

    if _depth > _MAX_DEPTH:
        return None
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Mapping):
        return {
            str(key): json_safe(item, _depth=_depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [json_safe(item, _depth=_depth + 1) for item in value]
    if is_dataclass(value) and not isinstance(value, type):
        return json_safe(asdict(value), _depth=_depth + 1)
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        try:
            return json_safe(to_dict(), _depth=_depth + 1)
        except Exception:
            return None
    return None


def stable_sha256(value: Any) -> str:
    """Hash a JSON-safe value with the project's canonical encoding."""

    payload = json.dumps(
        json_safe(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


__all__ = ["json_safe", "stable_sha256"]

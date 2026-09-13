"""Source-controlled identities for the built-in reference check contracts.

These are deliberately small infrastructure contracts.  Their hashes are
derived from the reviewable definitions below using the same canonical JSON
encoding used by run manifests; they are not arbitrary non-zero sentinels.
"""

from __future__ import annotations

from typing import Any

from .artifacts import canonical_json, sha256_bytes


DEFAULT_CONTRACT_DEFINITIONS: dict[str, dict[str, Any]] = {
    "safety": {
        "contract_id": "default-safety",
        "version": "1",
        "scope": "project-owned averaged-reference safety checks",
        "rules": [
            "finite observations",
            "bounded duty in [0,1]",
            "positive finite model parameters",
        ],
    },
    "qualification": {
        "contract_id": "default-qualification",
        "version": "1",
        "scope": "project-owned averaged-reference sample qualification",
        "rules": ["non-empty samples", "finite numeric sample values"],
    },
}


def default_contract_ref(name: str) -> dict[str, str]:
    """Return the stable id/hash reference for a built-in contract."""

    try:
        definition = DEFAULT_CONTRACT_DEFINITIONS[name]
    except KeyError as exc:
        raise KeyError(f"unknown default contract: {name}") from exc
    return {
        "id": str(definition["contract_id"]),
        "hash": sha256_bytes(canonical_json(definition)),
    }


def default_contract_refs() -> dict[str, dict[str, str]]:
    return {name: default_contract_ref(name) for name in DEFAULT_CONTRACT_DEFINITIONS}


__all__ = ["DEFAULT_CONTRACT_DEFINITIONS", "default_contract_ref", "default_contract_refs"]

"""Read-only credibility assessment of a published run package.

The assessment aggregates independent factors; it never produces a single
numeric score and never writes into the package.  A factor answers one
question, and the overall status follows an explicit precedence:

``not_credible`` > ``limited`` > ``unknown`` > ``credible``.

This module deliberately does not import a Plant, a backend adapter, or a
solver.  It consumes recorded evidence only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import math
from pathlib import Path
from typing import Any, Mapping, Sequence

from .jsonutil import json_safe


FACTOR_IDS = (
    "integrity",
    "source_state",
    "run_mode",
    "environment",
    "backend",
    "plant_identity",
    "implementation_review",
    "qualification",
    "applicability",
    "numerical_health",
    "safety",
    "evidence_level",
)


@dataclass(frozen=True)
class CredibilityFactor:
    factor_id: str
    status: str  # pass | limited | fail | unknown
    evidence: Mapping[str, Any] = field(default_factory=dict)
    limitation: str | None = None

    def __post_init__(self) -> None:
        if self.status not in {"pass", "limited", "fail", "unknown"}:
            raise ValueError(f"unsupported credibility factor status: {self.status!r}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "factor_id": self.factor_id,
            "status": self.status,
            "evidence": json_safe(self.evidence),
            "limitation": self.limitation,
        }


@dataclass(frozen=True)
class CredibilityPolicy:
    require_declared_envelope: bool = False
    require_reviewed_implementation: bool = False
    require_known_backend: bool = False
    include_authenticity: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "require_declared_envelope": bool(self.require_declared_envelope),
            "require_reviewed_implementation": bool(self.require_reviewed_implementation),
            "require_known_backend": bool(self.require_known_backend),
            "include_authenticity": bool(self.include_authenticity),
        }


@dataclass(frozen=True)
class CredibilityAssessment:
    status: str  # credible | limited | unknown | not_credible
    factors: tuple[CredibilityFactor, ...]
    blocking: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    rule_version: str = "credibility-v1"
    run_id: str | None = None
    manifest_sha256: str | None = None
    package_sha256: str | None = None
    policy: CredibilityPolicy = CredibilityPolicy()

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "credibility-v1",
            "status": self.status,
            "rule_version": self.rule_version,
            "run_id": self.run_id,
            "manifest_sha256": self.manifest_sha256,
            "package_sha256": self.package_sha256,
            "policy": self.policy.to_dict(),
            "factors": [factor.to_dict() for factor in self.factors],
            "blocking": list(self.blocking),
            "limitations": list(self.limitations),
        }


def _load_optional_json(path: Path) -> Mapping[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, Mapping) else None


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _hex_digest(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdefABCDEF" for character in value)
    )


def _factor(
    factor_id: str,
    status: str,
    evidence: Mapping[str, Any],
    limitation: str | None = None,
) -> CredibilityFactor:
    return CredibilityFactor(factor_id, status, evidence=evidence, limitation=limitation)


def _integrity_factor(manifest: Mapping[str, Any], problems: Sequence[str]) -> CredibilityFactor:
    if problems:
        return _factor(
            "integrity",
            "fail",
            {"problems": list(problems)},
            "the package does not match its own recorded hashes",
        )
    if _hex_digest(manifest.get("manifest_sha256")) and _hex_digest(manifest.get("package_sha256")):
        return _factor(
            "integrity",
            "pass",
            {
                "manifest_sha256": manifest.get("manifest_sha256"),
                "package_sha256": manifest.get("package_sha256"),
            },
        )
    return _factor(
        "integrity",
        "unknown",
        {},
        "the package records no verifiable manifest/package digest",
    )


def _source_state_factor(manifest: Mapping[str, Any]) -> CredibilityFactor:
    status_value = manifest.get("working_tree_status")
    commit = manifest.get("source_commit")
    provenance = _mapping(manifest.get("provenance"))
    provenance_status = provenance.get("status")
    evidence = {
        "working_tree_status": status_value,
        "source_commit": commit,
        "provenance_status": provenance_status,
    }
    if status_value == "clean" and isinstance(commit, str) and len(commit) >= 40:
        return _factor("source_state", "pass", evidence)
    if status_value == "dirty":
        return _factor(
            "source_state",
            "limited",
            evidence,
            "the producing working tree contained uncommitted changes",
        )
    return _factor(
        "source_state",
        "unknown",
        evidence,
        "the source revision or working-tree state is unknown",
    )


def _run_mode_factor(manifest: Mapping[str, Any]) -> CredibilityFactor:
    mode = manifest.get("run_mode")
    if mode == "formal_comparison":
        return _factor("run_mode", "pass", {"run_mode": mode})
    if mode == "exploratory":
        return _factor(
            "run_mode",
            "limited",
            {"run_mode": mode},
            "the run was exploratory and was not gated for formal comparison",
        )
    return _factor("run_mode", "unknown", {"run_mode": mode}, "the run mode is missing")


def _environment_factor(manifest: Mapping[str, Any]) -> CredibilityFactor:
    environment = _mapping(manifest.get("environment"))
    status_value = environment.get("status")
    if status_value == "known":
        return _factor("environment", "pass", {"status": status_value})
    if status_value == "partial":
        return _factor(
            "environment",
            "limited",
            {"status": status_value, "limitations": environment.get("limitations", [])},
            "the environment provenance is partial",
        )
    return _factor(
        "environment",
        "unknown",
        {"status": status_value},
        "the environment provenance is unknown or missing",
    )


def _backend_factor(manifest: Mapping[str, Any], policy: CredibilityPolicy) -> CredibilityFactor:
    environment = _mapping(manifest.get("environment"))
    backends = environment.get("backends")
    if not isinstance(backends, (list, tuple)) or not backends:
        return _factor("backend", "unknown", {}, "no backend provenance was recorded")
    normalized = [dict(item) if isinstance(item, Mapping) else {"status": str(item)} for item in backends]
    statuses = {str(item.get("status")) for item in normalized}
    evidence = {"backends": normalized}
    if statuses <= {"known"}:
        return _factor("backend", "pass", evidence)
    if any(status in {"unknown", "unavailable"} for status in statuses):
        return _factor(
            "backend",
            "fail" if policy.require_known_backend else "limited",
            evidence,
            "a backend identity is unavailable",
        )
    return _factor(
        "backend",
        "fail" if policy.require_known_backend else "limited",
        evidence,
        "the backend provenance is partial or not declared",
    )


def _plant_identity_factor(manifest: Mapping[str, Any]) -> CredibilityFactor:
    plant = _mapping(manifest.get("plant"))
    identity = _mapping(plant.get("identity"))
    if not plant or not identity:
        return _factor("plant_identity", "unknown", {}, "the Plant identity is missing")
    evidence = {
        "plant_hash": plant.get("hash"),
        "identity_sha256": identity.get("sha256"),
        "has_parameters": isinstance(identity.get("parameters"), Mapping),
    }
    if _hex_digest(plant.get("hash")) and _hex_digest(identity.get("sha256")) and isinstance(
        identity.get("parameters"), Mapping
    ):
        return _factor("plant_identity", "pass", evidence)
    if _hex_digest(plant.get("hash")):
        return _factor(
            "plant_identity",
            "limited",
            evidence,
            "the Plant identity does not declare its parameters",
        )
    return _factor("plant_identity", "unknown", evidence, "the Plant identity digest is missing")


def _implementation_review_factor(
    manifest: Mapping[str, Any],
    current_review: Mapping[str, Any] | None,
    policy: CredibilityPolicy,
) -> CredibilityFactor:
    plant = _mapping(manifest.get("plant"))
    identity = _mapping(plant.get("identity"))
    implementation = _mapping(identity.get("implementation"))
    if not implementation:
        return _factor(
            "implementation_review",
            "unknown",
            {},
            "the Plant does not record a reviewed implementation identity",
        )
    recorded = _mapping(implementation.get("review"))
    recorded_status = recorded.get("status")
    current_status = current_review.get("status") if isinstance(current_review, Mapping) else None
    effective = current_status or recorded_status
    evidence = {
        "recorded_status": recorded_status,
        "recorded_sha256": implementation.get("sha256"),
        "current_checkout_status": current_status,
    }
    if effective == "reviewed":
        return _factor("implementation_review", "pass", evidence)
    if effective in {"unreviewed", "not_pinned"}:
        return _factor(
            "implementation_review",
            "fail" if policy.require_reviewed_implementation else "limited",
            evidence,
            "the solve-path implementation does not match a reviewed pin",
        )
    return _factor(
        "implementation_review",
        "unknown",
        evidence,
        "the implementation review status is unknown",
    )


def _qualification_factor(
    manifest: Mapping[str, Any],
    qualification: Mapping[str, Any] | None,
) -> CredibilityFactor:
    document = qualification if isinstance(qualification, Mapping) else _mapping(manifest.get("qualification"))
    level = document.get("level")
    passed = document.get("passed")
    checks = _mapping(document.get("checks"))
    findings = checks.get("findings") if isinstance(checks.get("findings"), list) else []
    evidence = {"level": level, "passed": passed, "finding_count": len(findings)}
    if level == "full":
        return _factor("qualification", "pass", evidence)
    if level == "limited":
        return _factor(
            "qualification",
            "limited",
            evidence,
            "the sample set is usable only with declared limitations",
        )
    if level == "failed" or passed is False:
        return _factor("qualification", "fail", evidence, "the run did not pass qualification")
    if passed is True:
        derived = "limited" if any(
            isinstance(item, Mapping) and item.get("severity") == "warning"
            for item in findings
        ) else "pass"
        return _factor(
            "qualification",
            derived,
            {**evidence, "derived_from_legacy_fields": True},
            None if derived == "pass" else "the qualification level was derived from legacy fields",
        )
    return _factor("qualification", "unknown", evidence, "qualification evidence is missing")


def _applicability_factor(
    manifest: Mapping[str, Any],
    preflight: Mapping[str, Any] | None,
    policy: CredibilityPolicy,
) -> CredibilityFactor:
    applicability = _mapping(manifest.get("applicability"))
    if not applicability:
        preflight_applicability = _mapping((preflight or {}).get("applicability"))
        applicability = preflight_applicability
    if not applicability:
        return _factor(
            "applicability",
            "limited" if policy.require_declared_envelope else "unknown",
            {},
            "no applicability evidence was recorded",
        )
    claim = _mapping(applicability.get("claim"))
    checks = _mapping(applicability.get("checks"))
    findings = checks.get("findings") if isinstance(checks.get("findings"), list) else []
    categories = {
        str(item.get("category"))
        for item in findings
        if isinstance(item, Mapping) and item.get("passed") is False
    }
    has_error = any(
        isinstance(item, Mapping) and item.get("severity") in {"error", "fatal"}
        for item in findings
    )
    evidence = {
        "status": applicability.get("status"),
        "claim_status": claim.get("status"),
        "claim_level": claim.get("claim_level"),
        "envelope_sha256": claim.get("envelope_sha256"),
        "finding_count": len(findings),
        "categories": sorted(categories),
    }
    if has_error:
        return _factor("applicability", "fail", evidence, "an applicability limit was violated")
    if claim.get("status") == "unknown":
        if policy.require_declared_envelope:
            return _factor(
                "applicability",
                "limited",
                evidence,
                "the model does not declare a validity envelope",
            )
        return _factor(
            "applicability",
            "pass",
            evidence,
            "the model does not declare a validity envelope",
        )
    if claim.get("status") == "declaration_invalid":
        return _factor(
            "applicability",
            "limited",
            evidence,
            "the validity envelope could not be interpreted",
        )
    if categories & {"out_of_envelope", "ccm_boundary_violated", "external_input_changed"}:
        return _factor(
            "applicability",
            "limited",
            evidence,
            "the run entered a declared or assumed out-of-envelope region",
        )
    return _factor("applicability", "pass", evidence)


def _numerical_health_factor(manifest: Mapping[str, Any]) -> CredibilityFactor:
    plant = _mapping(manifest.get("plant"))
    evidence = _mapping(plant.get("evidence"))
    health = _mapping(evidence.get("numerical_health"))
    severity = health.get("severity")
    identity = _mapping(plant.get("identity"))
    if severity == "clean":
        return _factor("numerical_health", "pass", {"severity": severity})
    if severity == "warning":
        return _factor(
            "numerical_health",
            "limited",
            {"severity": severity, "categories": health.get("categories", {})},
            "the solver recovered from numerical warnings",
        )
    if severity == "fatal":
        return _factor(
            "numerical_health",
            "fail",
            {"severity": severity, "categories": health.get("categories", {})},
            "the solver log contains a fatal numerical category",
        )
    if identity.get("kind") == "ideal_averaged_reference":
        return _factor(
            "numerical_health",
            "pass",
            {"applicable": False, "reason": "deterministic_reference_model"},
            "the deterministic reference model has no external solver log",
        )
    return _factor(
        "numerical_health",
        "unknown",
        {},
        "no numerical-health evidence was recorded",
    )


def _safety_factor(manifest: Mapping[str, Any]) -> CredibilityFactor:
    safety = _mapping(manifest.get("safety"))
    passed = safety.get("passed")
    if passed is True:
        return _factor("safety", "pass", {"passed": True})
    if passed is False:
        return _factor("safety", "fail", {"passed": False}, "the run did not pass safety checks")
    return _factor("safety", "unknown", {}, "safety evidence is missing")


def _evidence_level_factor(manifest: Mapping[str, Any]) -> CredibilityFactor:
    level = manifest.get("evidence_level")
    if level == "comparable":
        return _factor("evidence_level", "pass", {"evidence_level": level})
    if level == "functional":
        return _factor(
            "evidence_level",
            "limited",
            {"evidence_level": level},
            "the recorded evidence supports functional use, not formal comparability",
        )
    if level == "limited":
        return _factor(
            "evidence_level",
            "limited",
            {"evidence_level": level},
            "the recorded evidence is limited by the run mode, source state, backend, or review",
        )
    return _factor(
        "evidence_level",
        "unknown",
        {"evidence_level": level},
        "the evidence level is missing or diagnostic only",
    )


def _authenticity_factor(authenticity: Mapping[str, Any] | None) -> CredibilityFactor:
    if not isinstance(authenticity, Mapping):
        return _factor(
            "authenticity",
            "unknown",
            {},
            "no attestation was supplied; local hashes prove integrity, not authorship",
        )
    trust = authenticity.get("trust_level")
    evidence = {
        "trust_level": trust,
        "signature_status": authenticity.get("signature_status"),
        "status": authenticity.get("status"),
        "anchor": authenticity.get("anchor"),
    }
    if authenticity.get("status") == "invalid":
        return _factor(
            "authenticity",
            "fail",
            evidence,
            "the attestation is inconsistent with the package",
        )
    if trust == "L1_anchored":
        return _factor("authenticity", "pass", evidence)
    if trust == "L1_pending":
        return _factor(
            "authenticity",
            "limited",
            evidence,
            "the attestation is locally consistent but is not anchored in a Git commit",
        )
    return _factor(
        "authenticity",
        "unknown",
        evidence,
        "the attestation trust level is unknown",
    )


def _aggregate(factors: Sequence[CredibilityFactor]) -> str:
    if any(factor.status == "fail" for factor in factors):
        return "not_credible"
    if any(factor.status == "limited" for factor in factors):
        return "limited"
    if any(factor.status == "unknown" for factor in factors):
        return "unknown"
    return "credible"


def assess_manifest(
    manifest: Mapping[str, Any],
    *,
    qualification: Mapping[str, Any] | None = None,
    preflight: Mapping[str, Any] | None = None,
    safety: Mapping[str, Any] | None = None,
    current_review: Mapping[str, Any] | None = None,
    integrity_problems: Sequence[str] = (),
    authenticity: Mapping[str, Any] | None = None,
    policy: CredibilityPolicy | None = None,
) -> CredibilityAssessment:
    """Assess recorded evidence without reading or executing any Plant."""

    resolved_policy = policy or CredibilityPolicy()
    del safety  # safety is already summarized in the manifest
    factors: list[CredibilityFactor] = [
        _integrity_factor(manifest, integrity_problems),
        _source_state_factor(manifest),
        _run_mode_factor(manifest),
        _environment_factor(manifest),
        _backend_factor(manifest, resolved_policy),
        _plant_identity_factor(manifest),
        _implementation_review_factor(manifest, current_review, resolved_policy),
        _qualification_factor(manifest, qualification),
        _applicability_factor(manifest, preflight, resolved_policy),
        _numerical_health_factor(manifest),
        _safety_factor(manifest),
        _evidence_level_factor(manifest),
    ]
    if resolved_policy.include_authenticity:
        factors.append(_authenticity_factor(authenticity))

    status = _aggregate(factors)
    blocking = tuple(factor.factor_id for factor in factors if factor.status == "fail")
    limitations = tuple(
        f"{factor.factor_id}: {factor.limitation}"
        for factor in factors
        if factor.limitation and factor.status in {"limited", "unknown"}
    )
    return CredibilityAssessment(
        status=status,
        factors=tuple(factors),
        blocking=blocking,
        limitations=limitations,
        run_id=manifest.get("run_id") if isinstance(manifest.get("run_id"), str) else None,
        manifest_sha256=manifest.get("manifest_sha256") if _hex_digest(manifest.get("manifest_sha256")) else None,
        package_sha256=manifest.get("package_sha256") if _hex_digest(manifest.get("package_sha256")) else None,
        policy=resolved_policy,
    )


def assess_credibility(
    run_dir: str | Path,
    *,
    manifest: Mapping[str, Any] | None = None,
    qualification: Mapping[str, Any] | None = None,
    preflight: Mapping[str, Any] | None = None,
    safety: Mapping[str, Any] | None = None,
    current_review: Mapping[str, Any] | None = None,
    integrity_problems: Sequence[str] | None = None,
    authenticity: Mapping[str, Any] | None = None,
    policy: CredibilityPolicy | None = None,
) -> CredibilityAssessment:
    """Read a published package and return its credibility assessment."""

    directory = Path(run_dir)
    manifest_value = manifest if isinstance(manifest, Mapping) else _load_optional_json(directory / "manifest.json")
    if manifest_value is None:
        raise ValueError(f"manifest.json is missing or unreadable in {directory}")
    qualification_value = (
        qualification
        if isinstance(qualification, Mapping)
        else _load_optional_json(directory / "qualification.json")
    )
    preflight_value = (
        preflight
        if isinstance(preflight, Mapping)
        else _load_optional_json(directory / "preflight.json")
    )
    safety_value = (
        safety
        if isinstance(safety, Mapping)
        else _load_optional_json(directory / "safety.json")
    )
    return assess_manifest(
        manifest_value,
        qualification=qualification_value,
        preflight=preflight_value,
        safety=safety_value,
        current_review=current_review,
        integrity_problems=() if integrity_problems is None else integrity_problems,
        authenticity=authenticity,
        policy=policy,
    )


__all__ = [
    "FACTOR_IDS",
    "CredibilityAssessment",
    "CredibilityFactor",
    "CredibilityPolicy",
    "assess_credibility",
    "assess_manifest",
]

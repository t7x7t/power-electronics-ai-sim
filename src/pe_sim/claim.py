"""One read-only conclusion: what may a reader claim from this package?

A published package records three independent facts:

* ``manifest.evidence_level`` -- how strong the recorded evidence is;
* ``manifest.qualification.level`` -- how completely the configured checks ran;
* the credibility assessment -- whether the recorded evidence is intact,
  attributable, and reviewed.

Answering "what may I claim?" by reading three fields invites over-claiming,
because each field alone can look stronger than their combination allows.  This
module derives a single conclusion that is **never stronger than any input
allows**.  The conclusion is reader-side: nothing is written into the package,
no schema is changed, and evidence is never upgraded.

The conclusion ladder, weakest to strongest::

    no_claim < exploratory < bounded < comparison_eligible

``comparison_eligible`` is reachable only when every input is at its strongest:
intact package, successful run, ``comparable`` evidence, ``assessed``
qualification, ``credible`` credibility, and a ``current`` schema verdict.
Every other combination is narrowed, and ``narrowed_by`` names the reason.

The module deliberately imports no Plant, backend, or solver.  It is safe to
call from an evidence reader that has already verified the package.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .checks import normalize_check_level
from .time_model import RUNTIME_EVIDENCE_LEVELS


#: Schema tag for the claim document emitted by ``inspect-run``.
CLAIM_SCHEMA = "pe-sim.claim.v1"

#: Rule version recorded with every conclusion.
CLAIM_RULE_VERSION = "claim-v1"

#: Closed conclusion vocabulary, ordered weakest to strongest.
CLAIM_CONCLUSIONS: tuple[str, ...] = (
    "no_claim",
    "exploratory",
    "bounded",
    "comparison_eligible",
)

_RANK = {conclusion: rank for rank, conclusion in enumerate(CLAIM_CONCLUSIONS)}
_EVIDENCE_RANK = {level: rank for rank, level in enumerate(RUNTIME_EVIDENCE_LEVELS)}

_RUN_OK_STATUSES = frozenset({"RUN_OK", "QUALIFIED", "COMPARABLE"})
_COMPATIBILITY_STATUSES = frozenset({"current", "read_only", "incompatible"})

_STATEMENTS: Mapping[str, str] = {
    "no_claim": (
        "No claim may be made from this package; the recorded evidence is not "
        "usable for a statement about the run."
    ),
    "exploratory": (
        "The recorded evidence supports exploratory use only; it may not be "
        "used for a formal or numerical comparison."
    ),
    "bounded": (
        "The recorded evidence supports a bounded claim only; use the result "
        "with the recorded limitations and do not compare it formally."
    ),
    "comparison_eligible": (
        "The package is eligible for formal comparison at the recorded schema "
        "version and source commit, subject to the stated limits."
    ),
}

_MAY_CLAIM: Mapping[str, tuple[str, ...]] = {
    "no_claim": (),
    "exploratory": (
        "the configured routine executed and produced an internally complete record",
        "the recorded signals may be inspected for exploratory analysis",
    ),
    "bounded": (
        "the recorded result may be used within the limitations named in this document",
        "the package is internally consistent at the recorded schema version",
    ),
    "comparison_eligible": (
        "the configured checks were evaluated and passed",
        "the recorded evidence is comparable at the recorded schema version",
        "the credibility factors passed under the declared policy",
    ),
}

_MAY_NOT_CLAIM: Mapping[str, tuple[str, ...]] = {
    "no_claim": (
        "that the run executed successfully",
        "any numerical result or trend",
        "any physical, thermal, safety, or hardware behaviour",
        "comparison with another run",
    ),
    "exploratory": (
        "formal or numerical comparison with another run",
        "physical, thermal, safety, or hardware validity",
        "behaviour outside the recorded operating conditions",
    ),
    "bounded": (
        "formal or numerical comparison with another run",
        "the absence of the recorded limitations",
        "physical, thermal, safety, or hardware validity",
    ),
    "comparison_eligible": (
        "comparison across a different schema version or source commit",
        "physical, thermal, safety, or hardware validity",
        "behaviour outside the recorded operating conditions",
    ),
}


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _qualification(value: Any) -> str | None:
    try:
        return normalize_check_level(value)
    except ValueError:
        return None


def _evidence_conclusion(level: str | None) -> str:
    """The strongest conclusion the recorded evidence level can support."""

    if level == "comparable":
        return "comparison_eligible"
    if level == "limited":
        return "bounded"
    if level == "functional":
        return "exploratory"
    return "no_claim"


@dataclass(frozen=True)
class ClaimConclusion:
    """A single read-only statement of what the recorded evidence supports.

    ``conclusion`` is always one of :data:`CLAIM_CONCLUSIONS`.  ``inputs``
    copies the values the derivation read, so a caller never has to reconstruct
    which fields produced the result.  ``narrowed_by`` names every input that
    falls below the ``comparison_eligible`` threshold; it is empty only when
    every input is at its strongest.
    """

    conclusion: str
    inputs: Mapping[str, Any]
    narrowed_by: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    rule_version: str = CLAIM_RULE_VERSION

    def __post_init__(self) -> None:
        if self.conclusion not in CLAIM_CONCLUSIONS:
            raise ValueError(
                f"unsupported claim conclusion: {self.conclusion!r}; "
                f"expected one of {list(CLAIM_CONCLUSIONS)}"
            )

    @property
    def statement(self) -> str:
        return _STATEMENTS[self.conclusion]

    @property
    def may_claim(self) -> tuple[str, ...]:
        return _MAY_CLAIM[self.conclusion]

    @property
    def may_not_claim(self) -> tuple[str, ...]:
        return _MAY_NOT_CLAIM[self.conclusion]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": CLAIM_SCHEMA,
            "rule_version": self.rule_version,
            "conclusion": self.conclusion,
            "statement": self.statement,
            "inputs": dict(self.inputs),
            "narrowed_by": list(self.narrowed_by),
            "may_claim": list(self.may_claim),
            "may_not_claim": list(self.may_not_claim),
            "limitations": list(self.limitations),
        }


def _cap(candidate: str, ceiling: str) -> str:
    """Lower ``candidate`` to ``ceiling`` when it is stronger."""

    return ceiling if _RANK[candidate] > _RANK[ceiling] else candidate


def derive_claim(
    *,
    integrity: str,
    run_status: Any = None,
    evidence_level: Any = None,
    qualification_level: Any = None,
    credibility: Any = None,
    compatibility: Any = None,
) -> ClaimConclusion:
    """Derive the single conclusion from already-recorded evidence.

    ``integrity`` is ``"ok"`` or ``"invalid"`` as reported by the reader; it is
    required because a claim about a package whose hashes do not verify would
    be meaningless.  Every other input is optional and is narrowed, never
    widened, when it is missing or carries an unrecognised token.

    The function never raises for malformed input and never writes.
    """

    integrity_text = _text(integrity)
    run_status_text = _text(run_status)
    evidence_text = _text(evidence_level)
    qualification_text = _qualification(qualification_level)
    credibility_text = _text(credibility)
    compatibility_text = _text(compatibility)

    inputs: dict[str, Any] = {
        "integrity": integrity_text,
        "run_status": run_status_text,
        "evidence_level": evidence_text,
        "qualification_level": qualification_text,
        "credibility": credibility_text,
        "compatibility": compatibility_text,
    }

    limitations: list[str] = [
        "derived from recorded metadata only; this view adds no new evidence",
        "read-only: the package and its recorded levels are unchanged",
    ]

    # Hard stops: no conclusion of any strength is supported.
    hard_stop: str | None = None
    if integrity_text != "ok":
        hard_stop = "the package integrity was not verified"
    elif run_status_text not in _RUN_OK_STATUSES:
        hard_stop = f"the recorded run status is {run_status_text!r}, not a successful run"
    elif evidence_text not in _EVIDENCE_RANK:
        hard_stop = "the evidence level is missing or outside the evidence ladder"
    elif qualification_text == "failed":
        hard_stop = "qualification failed"
    elif credibility_text == "not_credible":
        hard_stop = "the credibility assessment is not_credible"
    elif compatibility_text == "incompatible":
        hard_stop = "the package schema is not readable by this release"

    if hard_stop is not None:
        if compatibility_text not in _COMPATIBILITY_STATUSES:
            limitations.append("the compatibility verdict was missing or unrecognised")
        return ClaimConclusion(
            conclusion="no_claim",
            inputs=inputs,
            narrowed_by=(hard_stop,),
            limitations=tuple(limitations),
        )

    # Start from the strongest conclusion the evidence level can support.
    candidate = _evidence_conclusion(evidence_text)
    reasons: list[str] = []
    if evidence_text != "comparable":
        reasons.append(f"evidence level {evidence_text!r} is below comparable")

    # Qualification describes check completeness; it can lower the conclusion
    # but never raise it.
    if qualification_text == "partial":
        reasons.append(
            "qualification is partial: at least one configured check was not evaluated and passed"
        )
        candidate = _cap(candidate, "bounded")
    elif qualification_text != "assessed":
        reasons.append("qualification evidence is missing or uses an unrecognised level")
        candidate = _cap(candidate, "exploratory")

    # Credibility is an independent assessment of the recorded evidence.
    if credibility_text == "limited":
        reasons.append("the credibility assessment is limited")
        candidate = _cap(candidate, "bounded")
    elif credibility_text != "credible":
        reasons.append("the credibility assessment is missing, unknown, or unrecognised")
        candidate = _cap(candidate, "exploratory")

    # A read-only (or unassessed) schema verdict forbids formal comparison:
    # absent fields and renamed vocabulary are not new evidence.
    if compatibility_text != "current":
        reasons.append(
            "the package schema is not current for this reader; cross-version comparison is not assessed"
        )
        candidate = _cap(candidate, "bounded")
        limitations.append(
            "the package was read under a compatibility policy; a renamed value is not new evidence"
        )

    if candidate != "comparison_eligible":
        limitations.append(
            "the conclusion is capped below comparison_eligible; narrowed_by names every applied cap"
        )

    return ClaimConclusion(
        conclusion=candidate,
        inputs=inputs,
        narrowed_by=tuple(reasons),
        limitations=tuple(limitations),
    )


__all__ = [
    "CLAIM_CONCLUSIONS",
    "CLAIM_RULE_VERSION",
    "CLAIM_SCHEMA",
    "ClaimConclusion",
    "derive_claim",
]

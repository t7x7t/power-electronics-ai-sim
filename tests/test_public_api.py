"""Machine-checkable public API stability contract for ``pe_sim``.

The contract is intentionally dumb: two frozen name sets, one digest, and a
baseline file under ``tests/fixtures``. Any change to the stable surface fails
a test until the baseline is updated in the same commit, which is what makes
the change reviewable.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pe_sim
from pe_sim.public_api import (
    API_STABILITY_SCHEMA,
    EXPERIMENTAL_API,
    STABLE_API,
    public_api,
)

FIXTURE_PATH = Path(__file__).with_name("fixtures") / "public_api.json"

UPDATE_HINT = (
    "公开稳定面发生变化，请确认这是有意的破坏性变更/新增，并同步更新 "
    "tests/fixtures/public_api.json（stable 名单与 sha256 必须一起改）。"
)


def _load_fixture() -> dict:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def _stable_digest(names) -> str:
    """Digest of the sorted stable name list, as stored in the baseline file."""

    payload = json.dumps(sorted(names), separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def test_stable_and_experimental_are_disjoint_and_non_empty():
    assert STABLE_API, "stable surface must not be empty"
    assert EXPERIMENTAL_API, "experimental surface must not be empty"
    assert STABLE_API.isdisjoint(EXPERIMENTAL_API), (
        "a name cannot be both stable and experimental: "
        f"{sorted(STABLE_API & EXPERIMENTAL_API)}"
    )


def test_dunder_all_is_exactly_the_classified_surface():
    expected = sorted(STABLE_API | EXPERIMENTAL_API)
    assert list(pe_sim.__all__) == expected
    assert len(pe_sim.__all__) == len(set(pe_sim.__all__)), "__all__ has duplicates"


def test_every_exported_name_is_reachable_and_not_none():
    missing = [name for name in pe_sim.__all__ if not hasattr(pe_sim, name)]
    assert not missing, f"names advertised in __all__ but not defined: {missing}"

    nulls = [name for name in pe_sim.__all__ if getattr(pe_sim, name) is None]
    assert not nulls, f"names exported from pe_sim but bound to None: {nulls}"


def test_stable_surface_matches_frozen_baseline():
    fixture = _load_fixture()
    frozen = set(fixture["stable"])

    added = sorted(STABLE_API - frozen)
    removed = sorted(frozen - STABLE_API)
    assert frozen == set(STABLE_API), (
        f"{UPDATE_HINT}\n新增的稳定名称: {added}\n移除的稳定名称: {removed}"
    )


def test_frozen_baseline_is_sorted_and_self_consistent():
    fixture = _load_fixture()
    stable = fixture["stable"]

    assert fixture["schema"] == API_STABILITY_SCHEMA
    assert stable == sorted(stable), "fixture stable list must stay sorted"
    assert len(stable) == len(set(stable)), "fixture stable list has duplicates"
    assert fixture["sha256"] == _stable_digest(stable), (
        f"{UPDATE_HINT}\nfixture 中的 sha256 与 stable 名单不一致。"
    )


def test_public_api_digest_is_deterministic():
    first = public_api()
    second = public_api()

    assert first == second
    assert first["sha256"] == second["sha256"]
    assert first["schema"] == API_STABILITY_SCHEMA
    assert first["stable"] == sorted(STABLE_API)
    assert first["experimental"] == sorted(EXPERIMENTAL_API)


def test_new_capabilities_are_importable_from_top_level():
    # Names shipped since 0.5.0 that used to be module-only.
    from pe_sim import (  # noqa: F401
        CredibilityAssessment,
        ModelClaim,
        ToleranceSpec,
        attest_run,
        inspect_run,
        verify_attestation,
    )

    expected = {
        "CredibilityAssessment",
        "ModelClaim",
        "ToleranceSpec",
        "attest_run",
        "inspect_run",
        "verify_attestation",
    }
    assert expected <= set(pe_sim.__all__)
    assert expected <= STABLE_API, (
        "the 0.5.0 verification capabilities are the reason the stable surface "
        "exists and must not be classified as experimental"
    )


def test_experimental_surface_is_real():
    # Reverse guard: prevents "mark everything stable" from passing silently.
    assert EXPERIMENTAL_API - STABLE_API
    assert {"TopologyDescriptor", "NgspiceBuckPlant", "parse_log"} & EXPERIMENTAL_API

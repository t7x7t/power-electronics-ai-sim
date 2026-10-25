"""The recorded install-time extras policy (decision E1, ADR 0003).

The policy exists because a packaging extra is not a neutral detail here: it
would cross the license boundary, break the declared Python range, and change
the meaning of `environment-check`.  These tests make the mechanical half of
the decision fail closed, so the prose cannot drift away from the metadata.
"""

from __future__ import annotations

from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

from pe_sim.capabilities import capability_report
from pe_sim.environment import _read_supported


REPOSITORY = Path(__file__).resolve().parents[1]
PYPROJECT = REPOSITORY / "pyproject.toml"
ADR = REPOSITORY / "docs" / "adr" / "0003-install-time-extras.md"

#: Distribution names that must never become declared dependencies or extras.
_OPTIONAL_SIMULATOR_NAMES = frozenset(
    {"pyspice", "ngspice", "ahkab", "pyspice-ngspice", "ngspyce", "ltspice"}
)


def _project() -> dict:
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]


def _normalise(name: str) -> str:
    return name.strip().lower().replace("_", "-").replace(".", "-")


def _requirement_name(value: str) -> str:
    name = value.split(";", 1)[0]
    for separator in ("[", "<", ">", "=", "!", "~", " "):
        name = name.split(separator, 1)[0]
    return _normalise(name)


def test_only_the_test_extra_is_declared():
    extras = _project().get("optional-dependencies", {})

    assert set(extras) == {"test"}, extras


def test_no_optional_simulator_is_a_declared_dependency_or_extra():
    project = _project()
    declarations = list(project.get("dependencies", []))
    for values in project.get("optional-dependencies", {}).values():
        declarations.extend(values)

    names = {_requirement_name(value) for value in declarations}
    assert not (names & _OPTIONAL_SIMULATOR_NAMES), sorted(names & _OPTIONAL_SIMULATOR_NAMES)


def test_environment_check_does_not_expect_pyspice():
    _, supported = _read_supported(PYPROJECT)

    assert "pyspice" not in supported
    assert "numpy" in supported


def test_capability_report_points_at_the_pinned_handoff_not_an_extra():
    entry = next(
        item
        for item in capability_report()["capabilities"]
        if item["capability_id"] == "pyspice_netlist_builder"
    )

    assert "requirements-pyspice-tested.txt" in entry["install_hint"]
    assert "[" not in entry["install_hint"], entry["install_hint"]
    assert any("not a package extra" in item for item in entry["limitations"])


def test_the_decision_is_recorded_in_the_adr():
    text = ADR.read_text(encoding="utf-8")

    assert "## Status" in text
    assert "Accepted" in text
    assert "## Decision" in text
    assert "only** declared extra is `test`" in text
    assert "Python 3.13" in text
    assert "environment-check" in text
    assert "tests/test_release_policy.py" in text


def test_policy_documents_name_the_decision():
    documents = (
        REPOSITORY / "docs" / "28_release_toolchain_policy_v0.md",
        REPOSITORY / "docs" / "43_kernel_roadmap_v1.md",
        REPOSITORY / "docs" / "47_compatibility_policy_v1.md",
        REPOSITORY / "docs" / "49_optional_capabilities_v1.md",
    )

    for document in documents:
        text = document.read_text(encoding="utf-8")
        assert "decision E1" in text or "Decision E1" in text, document.name
        assert "0003-install-time-extras.md" in text, document.name


def test_pyspice_handoff_keeps_the_verified_pins():
    text = (REPOSITORY / "requirements-pyspice-tested.txt").read_text(encoding="utf-8")

    assert "PySpice==1.5" in text
    assert "numpy==1.26.4" in text


def test_license_rationale_is_still_recorded():
    text = (REPOSITORY / "docs" / "34_pyspice_optional_backend_v0.md").read_text(encoding="utf-8")

    assert "GPLv3" in text
    assert "MIT" in text

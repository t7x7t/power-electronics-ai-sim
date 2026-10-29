"""Structural validation of an agent-authored circuit topology document.

The descriptor contract answers "is this a graph the kernel can hold".  It does
not answer "did you mean ``inductance_h``", "is this net dangling", or "is this
document a circuit a compiler can render at all".  These tests hold the new
answer to those questions: one defect produces one issue, at one path, under one
code, and a group the checker never examined is never reported as clean.
"""

from __future__ import annotations

import copy
import json
import subprocess
import sys
from types import SimpleNamespace

import pytest

from pe_sim import topology
from pe_sim.topology import (
    boost_topology_descriptor,
    buck_topology_descriptor,
    model_control_buck_topology_descriptor,
    multiphase_buck_topology_descriptor,
    ngspice_buck_topology_descriptor,
    ngspice_sync_buck_topology_descriptor,
)
from pe_sim.topology_validation import (
    CHECK_GROUPS,
    TOPOLOGY_ISSUE_CODES,
    TOPOLOGY_VALIDATION_SCHEMA,
    WARNING_ISSUE_CODES,
    TopologyIssue,
    TopologyValidationError,
    build_descriptor,
    collect_topology_issues,
    topology_validation_document,
    validate_topology,
)


def _plant(**overrides):
    values = {
        "input_voltage_v": 12.0,
        "inductance_h": 100e-6,
        "capacitance_f": 470e-6,
        "load_resistance_ohm": 10.0,
        "switching_frequency_hz": 100e3,
        "control_period_s": 10e-6,
        "dead_time_s": 100e-9,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _switching_buck_document() -> dict:
    """The one shipped descriptor that is a compilable circuit."""

    return ngspice_buck_topology_descriptor(_plant()).to_dict()


def _component(document: dict, component_id: str) -> dict:
    return next(
        item for item in document["components"] if item["component_id"] == component_id
    )


def _codes(document) -> list[tuple[str, str, str]]:
    return [
        (issue.severity, issue.code, issue.path)
        for issue in collect_topology_issues(document)
    ]


def _mutate(mutator):
    """Apply one change to a document that carries no fingerprint.

    Dropping the fingerprint keeps each probe focused on the defect it injects:
    editing a recorded descriptor also invalidates its digest, and that second
    symptom would hide the finding under test.  The fingerprint rules have their
    own tests below.
    """

    document = copy.deepcopy(_switching_buck_document())
    document.pop("fingerprint", None)
    mutator(document)
    return document


# ---------------------------------------------------------------------------
# The shipped descriptors
# ---------------------------------------------------------------------------


def _shipped_descriptors():
    plant = _plant()
    return {
        "buck": buck_topology_descriptor(plant),
        "boost": boost_topology_descriptor(plant),
        "multiphase_buck": multiphase_buck_topology_descriptor(_plant(phases=4)),
        "ngspice_buck": ngspice_buck_topology_descriptor(plant),
        "ngspice_sync_buck": ngspice_sync_buck_topology_descriptor(plant),
        "model_control_buck": model_control_buck_topology_descriptor(plant),
    }


@pytest.mark.parametrize("name", sorted(_shipped_descriptors()))
def test_every_shipped_descriptor_is_a_valid_document(name):
    descriptor = _shipped_descriptors()[name]

    issues = collect_topology_issues(descriptor.to_dict())

    assert [issue for issue in issues if issue.severity == "error"] == []
    envelope = validate_topology(descriptor.to_dict())
    assert envelope["ok"] is True
    assert envelope["computed_fingerprint"] == descriptor.fingerprint


@pytest.mark.parametrize("name", ["buck", "boost", "multiphase_buck"])
def test_an_averaged_reference_reports_only_that_it_is_not_compilable(name):
    descriptor = _shipped_descriptors()[name]

    issues = collect_topology_issues(descriptor.to_dict())

    assert [(issue.code, issue.severity) for issue in issues] == [
        ("not_compilable", "warning")
    ]


def test_the_synchronous_reference_declares_how_its_two_gate_nets_relate():
    descriptor = _shipped_descriptors()["ngspice_sync_buck"]

    issues = collect_topology_issues(descriptor.to_dict())

    # The descriptor states that both gate nets derive from one commanded
    # duty.  Once that relation is declared, two control nets are no longer a
    # finding: the compiler can render them, and the validator must not warn
    # about a document that explains itself.
    assert issues == ()
    assert descriptor.metadata["control"]["mode"] == "single_duty_complementary"


def _sync_document_without_control_declaration() -> dict:
    document = copy.deepcopy(_shipped_descriptors()["ngspice_sync_buck"].to_dict())
    document.pop("fingerprint", None)
    document["metadata"].pop("control", None)
    return document


# ---------------------------------------------------------------------------
# The closed vocabulary
# ---------------------------------------------------------------------------


def test_issue_codes_are_a_closed_set():
    # Exact equality, not a count: a renamed or added code is a contract event,
    # and a client that branches on this set must be forced to notice.  A count
    # stays right when one code is swapped for another.
    assert TOPOLOGY_ISSUE_CODES == frozenset(
        {
            "actuator_missing",
            "control_net_multiple_drivers",
            "control_net_undriven",
            "disconnected_graph",
            "duplicate_component_id",
            "duplicate_net_id",
            "duplicate_port_id",
            "fingerprint_mismatch",
            "invalid_document",
            "invalid_parameter_value",
            "isolated_net",
            "missing_parameter",
            "missing_port",
            "missing_reference_net",
            "multiple_control_nets",
            "multiple_reference_nets",
            "no_output_load",
            "not_compilable",
            "port_net_kind_mismatch",
            "schema_violation",
            "unknown_net",
            "unknown_parameter",
            "unknown_port",
            "unsupported_device_kind",
            "unsupported_net_kind",
            "voltage_source_loop",
        }
    )
    assert all(isinstance(code, str) and code.islower() for code in TOPOLOGY_ISSUE_CODES)
    assert WARNING_ISSUE_CODES <= TOPOLOGY_ISSUE_CODES
    assert WARNING_ISSUE_CODES == frozenset(
        {"actuator_missing", "multiple_control_nets", "no_output_load", "not_compilable"}
    )


def test_the_validator_and_the_compiler_bless_the_same_control_modes():
    """A mode the compiler honours must not be warned about, and vice versa."""

    from pe_sim.circuit_compile import SUPPORTED_CONTROL_MODES
    from pe_sim.topology_validation import DECLARED_CONTROL_MODES

    assert DECLARED_CONTROL_MODES == SUPPORTED_CONTROL_MODES


def test_an_issue_refuses_a_code_outside_the_vocabulary():
    with pytest.raises(ValueError, match="unknown topology issue code"):
        TopologyIssue(code="made_up", path="/", message="x")


@pytest.mark.parametrize("severity", ["fatal", "info", ""])
def test_an_issue_refuses_an_unknown_severity(severity):
    with pytest.raises(ValueError, match="severity"):
        TopologyIssue(code="schema_violation", path="/", message="x", severity=severity)


@pytest.mark.parametrize("path", ["", "components/0"])
def test_an_issue_requires_a_json_pointer(path):
    with pytest.raises(ValueError, match="JSON Pointer"):
        TopologyIssue(code="schema_violation", path=path, message="x")


def test_every_warning_code_is_actually_emitted_as_a_warning():
    """A code declared as a warning must not be produced as an error."""

    probes = {
        "actuator_missing": _mutate(
            lambda d: d["components"].remove(_component(d, "switch"))
        ),
        "multiple_control_nets": _sync_document_without_control_declaration(),
        "no_output_load": _mutate(
            lambda d: d["components"].remove(_component(d, "load"))
        ),
        "not_compilable": _shipped_descriptors()["buck"].to_dict(),
    }

    for code, document in probes.items():
        found = [
            (issue.code, issue.severity)
            for issue in collect_topology_issues(document)
            if issue.code == code
        ]
        assert found == [(code, "warning")], code


def test_no_output_load_is_a_warning_not_an_error():
    document = _mutate(lambda d: d["components"].remove(_component(d, "load")))

    issues = collect_topology_issues(document)

    # A circuit with no sink is still a connected, well-formed graph; it just
    # has nothing to observe.  That is a warning, and ``ok`` stays true.
    assert [(issue.code, issue.severity) for issue in issues] == [
        ("no_output_load", "warning")
    ]
    assert validate_topology(document)["ok"] is True
    assert validate_topology(document)["warning_count"] == 1


# ---------------------------------------------------------------------------
# Fingerprint handling
# ---------------------------------------------------------------------------


def test_a_document_without_a_fingerprint_is_accepted_and_gets_one_computed():
    document = _switching_buck_document()
    document.pop("fingerprint")

    envelope = validate_topology(document)

    assert envelope["declared_fingerprint"] is None
    assert envelope["computed_fingerprint"] == build_descriptor(document).fingerprint
    assert "fingerprint" not in envelope["checked"]


def test_a_wrong_fingerprint_is_reported_alone():
    document = _switching_buck_document()
    document["fingerprint"] = "0" * 64

    issues = collect_topology_issues(document)

    assert [(issue.code, issue.path) for issue in issues] == [
        ("fingerprint_mismatch", "/fingerprint")
    ]
    assert issues[0].expected == build_descriptor(document).fingerprint


def test_a_fingerprint_mismatch_is_suppressed_while_other_errors_exist():
    document = _switching_buck_document()
    _component(document, "inductor")["parameters"]["inductance_h"] = -1

    codes = [issue.code for issue in collect_topology_issues(document)]

    # One edit must not be reported twice; the digest finding returns as soon
    # as the document is otherwise clean.
    assert codes == ["invalid_parameter_value"]


def test_a_malformed_fingerprint_names_the_expected_value():
    document = _switching_buck_document()
    document["fingerprint"] = "not-a-digest"

    issues = collect_topology_issues(document)

    assert [issue.code for issue in issues] == ["fingerprint_mismatch"]
    assert issues[0].expected == build_descriptor(document).fingerprint


def test_build_descriptor_agrees_with_the_strict_round_trip():
    document = _switching_buck_document()

    assert build_descriptor(document) == topology.TopologyDescriptor.from_dict(document)


def test_build_descriptor_refuses_a_document_the_contract_rejects():
    document = _switching_buck_document()
    document["components"][0]["ports"][0]["net_id"] = "nowhere"

    with pytest.raises(TopologyValidationError) as raised:
        build_descriptor(document)

    assert [issue.code for issue in raised.value.issues] == ["schema_violation"]


# ---------------------------------------------------------------------------
# One defect, one issue
# ---------------------------------------------------------------------------


def _remove_required_parameter(document):
    _component(document, "inductor")["parameters"].pop("inductance_h")


def _unknown_net(document):
    _component(document, "inductor")["ports"][0]["net_id"] = "nowhere"


def _net_kind_mismatch(document):
    _component(document, "switch")["ports"][2]["net_id"] = "vin"


def _duplicate_component_id(document):
    _component(document, "capacitor")["component_id"] = "inductor"


def _duplicate_net_id(document):
    document["nets"].append(dict(document["nets"][0]))


def _isolated_net(document):
    document["nets"].append({"net_id": "spare", "kind": "electrical"})


def _missing_reference_net(document):
    for net in document["nets"]:
        if net["net_id"] == "gnd":
            net["kind"] = "electrical"


def _undriven_control_net(document):
    document["components"].remove(_component(document, "pwm_source"))


def _voltage_source_loop(document):
    document["components"].append(
        {
            "component_id": "second_source",
            "kind": "voltage_source",
            "ports": [
                {"port_id": "positive", "net_id": "vin"},
                {"port_id": "negative", "net_id": "gnd"},
            ],
            "parameters": {"voltage_v": 5.0},
        }
    )


def _disconnected_island(document):
    document["nets"].extend(
        [
            {"net_id": "island_a", "kind": "electrical"},
            {"net_id": "island_b", "kind": "electrical"},
        ]
    )
    # Two parallel resistors so both island nets are bound twice: the finding
    # under test is the island, not a dangling terminal.
    for suffix in ("one", "two"):
        document["components"].append(
            {
                "component_id": f"island_load_{suffix}",
                "kind": "resistor_load",
                "ports": [
                    {"port_id": "positive", "net_id": "island_a"},
                    {"port_id": "negative", "net_id": "island_b"},
                ],
                "parameters": {"resistance_ohm": 10.0},
            }
        )


@pytest.mark.parametrize(
    "mutator, expected",
    [
        (
            lambda d: _component(d, "inductor").__setitem__("kind", "inductorr"),
            ("unsupported_device_kind", "/components/4/kind"),
        ),
        (
            lambda d: _component(d, "inductor")["ports"][0].__setitem__(
                "port_id", "inptu"
            ),
            ("unknown_port", "/components/4/ports/0/port_id"),
        ),
        (
            lambda d: _component(d, "inductor")["ports"].pop(),
            ("missing_port", "/components/4/ports"),
        ),
        (
            lambda d: _component(d, "inductor")["parameters"].__setitem__(
                "indutance_h", 1e-4
            ),
            ("unknown_parameter", "/components/4/parameters/indutance_h"),
        ),
        (_remove_required_parameter, ("missing_parameter", "/components/4/parameters")),
        (
            lambda d: _component(d, "inductor")["parameters"].__setitem__(
                "inductance_h", -1
            ),
            ("invalid_parameter_value", "/components/4/parameters/inductance_h"),
        ),
        (
            lambda d: _component(d, "inductor")["parameters"].__setitem__(
                "inductance_h", None
            ),
            ("invalid_parameter_value", "/components/4/parameters/inductance_h"),
        ),
        (_unknown_net, ("unknown_net", "/components/4/ports/0/net_id")),
        (
            lambda d: d["nets"].append({"net_id": "thermal_a", "kind": "thermal"}),
            ("unsupported_net_kind", "/nets/5/kind"),
        ),
        (
            _duplicate_component_id,
            ("duplicate_component_id", "/components/5/component_id"),
        ),
        (_duplicate_net_id, ("duplicate_net_id", "/nets/5/net_id")),
        (_isolated_net, ("isolated_net", "/nets/5/net_id")),
        (_missing_reference_net, ("missing_reference_net", "/nets")),
        (_disconnected_island, ("disconnected_graph", "/nets")),
        (_voltage_source_loop, ("voltage_source_loop", "/components/7")),
        (_undriven_control_net, ("control_net_undriven", "/nets/4/net_id")),
        (lambda d: d.__setitem__("schema", "other"), ("schema_violation", "/schema")),
        (
            lambda d: d.__setitem__("topology_id", "has space"),
            ("schema_violation", "/topology_id"),
        ),
        (
            lambda d: d.__setitem__("descriptor_version", "one"),
            ("schema_violation", "/descriptor_version"),
        ),
    ],
)
def test_a_single_injected_defect_produces_a_single_issue(mutator, expected):
    issues = collect_topology_issues(_mutate(mutator))

    assert [(issue.code, issue.path) for issue in issues] == [expected]


def test_a_misspelled_port_does_not_also_report_a_missing_port():
    document = _mutate(
        lambda d: _component(d, "inductor")["ports"][0].__setitem__("port_id", "inptu")
    )

    codes = [issue.code for issue in collect_topology_issues(document)]

    assert codes == ["unknown_port"]


def test_a_second_reference_net_is_reported_at_the_extra_net():
    document = _mutate(
        lambda d: d["nets"].append({"net_id": "gnd2", "kind": "reference"})
    )

    codes = [(issue.code, issue.path) for issue in collect_topology_issues(document)]

    assert ("multiple_reference_nets", "/nets/5/kind") in codes
    assert ("missing_reference_net", "/nets") not in codes


# ---------------------------------------------------------------------------
# Suggestions must be safe to follow
# ---------------------------------------------------------------------------


def test_a_real_misspelling_gets_a_suggestion():
    document = _mutate(
        lambda d: _component(d, "inductor")["parameters"].__setitem__(
            "indutance_h", 1e-4
        )
    )

    issue = collect_topology_issues(document)[0]

    assert issue.suggestion == "inductance_h"


def test_a_short_junk_parameter_gets_no_suggestion():
    document = _mutate(
        lambda d: _component(d, "inductor")["parameters"].__setitem__("zz", 1.0)
    )

    issue = collect_topology_issues(document)[0]

    assert issue.code == "unknown_parameter"
    assert issue.suggestion is None


def test_an_unknown_device_kind_suggests_the_nearest_known_kind():
    document = _mutate(lambda d: _component(d, "inductor").__setitem__("kind", "inductorr"))

    assert collect_topology_issues(document)[0].suggestion == "inductor"


def test_a_short_junk_device_kind_gets_no_suggestion():
    document = _mutate(lambda d: _component(d, "inductor").__setitem__("kind", "xyz"))

    assert collect_topology_issues(document)[0].suggestion is None


# ---------------------------------------------------------------------------
# `checked` must not claim to have examined what it skipped
# ---------------------------------------------------------------------------


def test_an_unrecognised_schema_stops_before_examining_anything():
    document = _mutate(lambda d: d.__setitem__("schema", "other"))

    envelope = topology_validation_document(document)

    assert envelope["checked"] == []
    assert envelope["computed_fingerprint"] is None


def test_checked_names_the_groups_that_ran():
    envelope = validate_topology(_switching_buck_document())

    assert envelope["checked"] == [
        "components",
        "connectivity",
        "control",
        "document_shape",
        "fingerprint",
        "nets",
        "sources",
    ]


def validate_topology_document(document):
    """Kept out of the import list: the strict entry point is what callers use."""

    return topology_validation_document(document)


# ---------------------------------------------------------------------------
# The strict entry point
# ---------------------------------------------------------------------------


def test_the_strict_entry_point_raises_with_every_issue():
    document = _mutate(
        lambda d: (
            _component(d, "inductor")["parameters"].__setitem__("inductance_h", -1),
            _component(d, "load")["parameters"].__setitem__("resistance_ohm", 0),
        )
    )

    with pytest.raises(TopologyValidationError) as raised:
        validate_topology(document)

    assert {issue.code for issue in raised.value.issues} == {"invalid_parameter_value"}
    assert len(raised.value.to_dict()["issues"]) == len(raised.value.issues)


def test_a_non_object_document_is_rejected_without_raising():
    issues = collect_topology_issues("a string")

    assert [(issue.code, issue.path) for issue in issues] == [
        ("invalid_document", "/")
    ]


def test_the_envelope_carries_the_documented_schema_tag():
    assert TOPOLOGY_VALIDATION_SCHEMA == "pe-sim.topology-validation.v1"
    assert validate_topology(_switching_buck_document())["schema"] == TOPOLOGY_VALIDATION_SCHEMA


# ---------------------------------------------------------------------------
# Contracts the module shares with its neighbours
# ---------------------------------------------------------------------------


def test_the_identifier_rule_matches_the_descriptor_module():
    from pe_sim.topology_validation import _SAFE_ID_RE

    # Duplicated rather than imported because the descriptor's pattern is
    # private; this assertion is what keeps the two from drifting apart.
    assert _SAFE_ID_RE.pattern == topology._ID_RE.pattern


def test_the_check_group_vocabulary_is_closed():
    assert CHECK_GROUPS == (
        "document_shape",
        "nets",
        "components",
        "connectivity",
        "control",
        "sources",
        "fingerprint",
    )


def test_the_module_imports_without_the_backends_package():
    """A validator that imported an adapter could not run before a solver exists.

    ``import pe_sim.anything`` first runs the package ``__init__``, which is the
    library's own composition root, so ``sys.modules`` cannot answer this.  The
    module's *own* imports can: it must not reach for ``pe_sim.backends``.
    """

    import ast
    from pathlib import Path

    import pe_sim.topology_validation as module

    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    targets: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            targets.append(node.module or "")
        elif isinstance(node, ast.Import):
            targets.extend(alias.name for alias in node.names)

    assert not any(
        target == "backends" or target.startswith("backends.") or target.startswith("pe_sim.backends")
        for target in targets
    ), targets

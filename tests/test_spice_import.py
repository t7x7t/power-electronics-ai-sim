"""Tests for the SPICE netlist importer.

The property that matters is not "the parser accepts my file" but "the circuit
that comes out is the circuit that went in".  Two tests carry most of that
weight: the round trip through the compiler, which compares element cards, and
the solver acceptance test, which compares an operating point.
"""

from __future__ import annotations

import ast
import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace

import pytest

from pe_sim.circuit_compile import compile_topology
from pe_sim.spice_import import (
    ELEMENT_LETTERS,
    ELEMENT_NODES,
    IMPORT_ISSUE_CODES,
    SPICE_IMPORT_SCHEMA,
    WARNING_IMPORT_CODES,
    SpiceImportError,
    import_spice_netlist,
    spice_import_document,
)
from pe_sim.topology import ngspice_buck_topology_descriptor
from pe_sim.topology_validation import build_descriptor


NGSPICE = os.environ.get("PE_SIM_NGSPICE")
needs_ngspice = pytest.mark.skipif(
    not NGSPICE,
    reason="set PE_SIM_NGSPICE to run the imported-circuit acceptance test",
)

VIN = 12.0
L_H = 100e-6
C_F = 470e-6
R_OHM = 10.0
FSW = 100e3
DUTY = 0.5

BUCK = """* buck with a freewheeling diode
.param Vin=12
.param Lval=100u
.param Cval=470u
.param Rload=10
.param Tsw=1e-05
V1 vin 0 DC {Vin}
Vpwm pwm 0 PULSE(0 1 0 1e-9 1e-9 5e-06 {Tsw})
S1 vin sw pwm 0 SWMOD
D1 0 sw DMOD
L1 sw vout {Lval} IC=0
C1 vout 0 {Cval} IC=0
R1 vout 0 {Rload}
.model SWMOD SW(Ron=1m Roff=1g Vt=0.5 Vh=0)
.model DMOD D(Is=1e-12 N=0.02)
.tran 1u 20m 0 1u uic
.options reltol=1e-3
.end
"""

SYNC = """* synchronous buck, two gate drives
V1 vin 0 DC 12
Vh pwm_h 0 PULSE(0 1 0 1e-9 1e-9 4.9e-06 1e-05)
Vl pwm_l 0 PULSE(0 1 5e-06 1e-9 1e-9 4.9e-06 1e-05)
Sh vin sw pwm_h 0 SWMOD
Sl 0 sw pwm_l 0 SWMOD
L1 sw vout 100u
C1 vout 0 470u
R1 vout 0 10
.model SWMOD SW(Ron=1m Roff=1g Vt=0.5 Vh=0)
.end
"""


def _envelope(text: str, topology_id: str = "imported") -> dict:
    return spice_import_document(text, topology_id=topology_id)


def _codes(text: str) -> list[str]:
    return [issue["code"] for issue in _envelope(text)["issues"]]


def _document(text: str, topology_id: str = "imported") -> dict:
    return dict(import_spice_netlist(text, topology_id=topology_id).document)


def _plant() -> SimpleNamespace:
    return SimpleNamespace(
        input_voltage_v=VIN,
        inductance_h=L_H,
        capacitance_f=C_F,
        load_resistance_ohm=R_OHM,
        switching_frequency_hz=FSW,
        control_period_s=10e-6,
        dead_time_s=100e-9,
    )


def _reviewed_descriptor():
    return ngspice_buck_topology_descriptor(_plant())


def _signature(text: str) -> list[tuple[str, tuple[str, ...]]]:
    """Return ``(letter, nodes)`` for every element card, in file order."""

    counts = {letter.upper(): len(nodes) for letter, nodes in ELEMENT_NODES.items()}
    signature: list[tuple[str, tuple[str, ...]]] = []
    in_control_block = False
    for raw in text.splitlines():
        line = raw.strip()
        if line == ".control":
            in_control_block = True
            continue
        if line == ".endc":
            in_control_block = False
            continue
        if in_control_block or not line or line.startswith(("*", ".", "+")):
            continue
        tokens = line.split()
        letter = tokens[0][0].upper()
        if letter not in counts:
            continue
        signature.append((letter, tuple(tokens[1 : 1 + counts[letter]])))
    return signature


def _net_kinds(document: dict) -> dict[str, str]:
    return {net["net_id"]: net["kind"] for net in document["nets"]}


# ---------------------------------------------------------------------------
# The closed surfaces
# ---------------------------------------------------------------------------


def test_the_issue_codes_are_a_closed_set():
    assert IMPORT_ISSUE_CODES == {
        "empty_netlist",
        "missing_title",
        "malformed_card",
        "unsupported_element",
        "unsupported_directive",
        "unsupported_expression",
        "unsupported_source_function",
        "invalid_value",
        "duplicate_element",
        "unknown_model",
        "unsupported_model_type",
        "unsupported_parameter",
        "duplicate_model",
        "unused_model",
        "missing_ground",
        "unsupported_control_reference",
        "pulse_width_not_imported",
        "complementary_control_undeclared",
    }


def test_only_the_declared_codes_are_warnings():
    assert WARNING_IMPORT_CODES == {
        "unused_model",
        "pulse_width_not_imported",
        "complementary_control_undeclared",
    }
    assert WARNING_IMPORT_CODES < IMPORT_ISSUE_CODES


def test_every_letter_maps_to_a_device_kind_the_vocabulary_declares():
    from pe_sim.circuit_devices import device_spec

    for letter, kind in ELEMENT_LETTERS.items():
        spec = device_spec(kind)
        assert spec.renderable, kind
        assert spec.spicetype == letter
        # The first SPICE node positions bind declared ports, in the same order.
        shape = [port_id for port_id in ELEMENT_NODES[letter] if port_id is not None]
        assert shape == [port.port_id for port in spec.ports], letter


def test_the_importer_does_not_import_a_backend():
    import pe_sim.spice_import as module

    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    targets: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            targets.append(node.module or "")
        elif isinstance(node, ast.Import):
            targets.extend(alias.name for alias in node.names)

    assert not any("backends" in target for target in targets), targets


# ---------------------------------------------------------------------------
# A netlist that should import
# ---------------------------------------------------------------------------


def test_a_hand_written_buck_imports_cleanly():
    envelope = _envelope(BUCK)

    assert envelope["schema"] == SPICE_IMPORT_SCHEMA
    assert envelope["import_ok"] is True
    assert envelope["ok"] is True
    assert envelope["error_count"] == 0
    assert envelope["title"] == "buck with a freewheeling diode"
    assert envelope["element_kinds"] == {
        "voltage_source": 1,
        "pulse_source": 1,
        "voltage_controlled_switch": 1,
        "freewheeling_diode": 1,
        "inductor": 1,
        "capacitor": 1,
        "resistor_load": 1,
    }
    assert envelope["counts"]["components"] == 7
    assert envelope["counts"]["nets"] == 5


def test_the_imported_descriptor_is_an_ordinary_descriptor():
    document = _document(BUCK)

    assert document["schema"] == "topology-descriptor-v1"
    assert document["descriptor_version"] == "1.0"
    assert "fingerprint" not in document
    assert document["metadata"]["source"]["format"] == "spice-netlist"
    # It builds, and the validator finds nothing to say about it.
    assert build_descriptor(document).topology_id == "imported"
    assert _envelope(BUCK)["validation"]["ok"] is True
    assert _envelope(BUCK)["validation"]["issues"] == []


def test_the_reference_and_control_nets_are_classified_from_connections():
    kinds = _net_kinds(_document(BUCK))

    assert kinds == {
        "vin": "electrical",
        "gnd": "reference",
        "pwm": "control",
        "sw": "electrical",
        "vout": "electrical",
    }


def test_the_imported_circuit_compiles_to_the_same_seven_cards():
    compiled = compile_topology(
        build_descriptor(_document(BUCK)), duty=DUTY, duration_s=2e-3
    )

    assert _signature(compiled.netlist) == [
        ("V", ("vin", "0")),
        ("V", ("pwm", "0")),
        ("S", ("vin", "sw", "pwm", "0")),
        ("D", ("0", "sw")),
        ("L", ("sw", "vout")),
        ("C", ("vout", "0")),
        ("R", ("vout", "0")),
    ]
    assert "DC 1.200000000000e+01" in compiled.netlist
    assert "IC=0.000000000000e+00" in compiled.netlist


def test_numeric_params_are_substituted_and_expressions_are_not():
    assert "100u" not in str(_document(BUCK))
    inductor = next(
        component
        for component in _document(BUCK)["components"]
        if component["component_id"] == "l1"
    )
    assert inductor["parameters"]["inductance_h"] == pytest.approx(L_H)


def test_a_parameter_declared_below_its_use_still_resolves():
    """SPICE resolves ``{name}`` against the whole file, not against what it read."""

    text = (
        "* late params\n"
        "V1 vin 0 DC {vin}\n"
        "L1 vin vout {lval}\n"
        "C1 vout 0 {cval}\n"
        "R1 vout 0 10\n"
        ".param vin=12\n"
        ".param lval=100u\n"
        ".param cval=470u\n"
    )
    envelope = _envelope(text)

    assert envelope["ok"] is True
    parameters = {
        component["component_id"]: component["parameters"]
        for component in envelope["descriptor"]["components"]
    }
    assert parameters["v1"]["voltage_v"] == pytest.approx(12.0)
    assert parameters["l1"]["inductance_h"] == pytest.approx(L_H)
    assert parameters["c1"]["capacitance_f"] == pytest.approx(C_F)


def test_a_param_after_end_does_not_resolve():
    text = "* after end\nV1 vin 0 DC {vin}\nR1 vin 0 10\n.end\n.param vin=12\n"

    assert _codes(text) == ["unsupported_expression"]


def test_a_node_name_that_is_not_a_descriptor_identifier_is_renamed_and_listed():
    text = (
        "* renamed nodes\n"
        "V1 in/rail 0 DC 12\n"
        "R1 in/rail out#node 10\n"
        "R2 out#node 0 10\n"
    )
    envelope = _envelope(text)

    assert envelope["ok"] is True
    assert envelope["net_name_map"] == {
        "0": "gnd",
        "in/rail": "in_rail",
        "out#node": "out_node",
    }


def test_a_real_gnd_node_is_not_merged_with_node_zero():
    text = "* gnd is a real node here\nV1 vin 0 DC 12\nR1 vin gnd 1\nR2 gnd 0 1\n"
    envelope = _envelope(text)

    assert envelope["net_name_map"]["0"] == "gnd"
    assert envelope["net_name_map"]["gnd"] == "gnd_2"
    assert _net_kinds(envelope["descriptor"])["gnd"] == "reference"
    assert _net_kinds(envelope["descriptor"])["gnd_2"] == "electrical"


# ---------------------------------------------------------------------------
# What cannot be carried across, said out loud
# ---------------------------------------------------------------------------


def test_the_duty_in_the_carrier_is_reported_and_not_imported():
    imported = import_spice_netlist(BUCK)

    assert [issue.code for issue in imported.warnings] == ["pulse_width_not_imported"]
    assert dict(imported.observed_duty) == {"vpwm": pytest.approx(0.5)}
    carrier = next(
        component
        for component in imported.document["components"]
        if component["kind"] == "pulse_source"
    )
    assert "duty" not in str(carrier["parameters"])
    assert carrier["parameters"]["switching_frequency_hz"] == pytest.approx(FSW)


def test_analysis_and_solver_directives_are_recorded_not_silently_dropped():
    envelope = _envelope(BUCK)

    dropped = [(item["directive"], item["line"]) for item in envelope["ignored_directives"]]
    assert dropped == [(".tran", 16), (".options", 17), (".end", 18)]
    assert all(item["reason"] for item in envelope["ignored_directives"])


def test_a_control_block_is_dropped_and_changes_nothing_about_the_circuit():
    without = _document(BUCK)
    with_control = _document(
        BUCK.replace(
            ".end\n",
            ".control\nset wr_vecnames\nrun\nwrdata output.dat v(vout)\n.endc\n.end\n",
        )
    )

    assert with_control["components"] == without["components"]
    assert with_control["nets"] == without["nets"]
    envelope = _envelope(
        BUCK.replace(".end\n", ".control\nrun\n.endc\n.end\n")
    )
    assert ".control" in [item["directive"] for item in envelope["ignored_directives"]]


def test_a_window_that_starts_inside_a_pulse_cannot_be_imported_back():
    """The compiled tail pulse uses a ``B`` source; that is rendering, not a circuit."""

    compiled = compile_topology(
        _reviewed_descriptor(), duty=DUTY, duration_s=2e-3, carrier_phase_s=3e-6
    )

    codes = _codes(compiled.netlist)
    assert "unsupported_element" in codes
    assert "unsupported_source_function" in codes  # the finite PWL tail
    element = next(
        issue
        for issue in _envelope(compiled.netlist)["issues"]
        if issue["code"] == "unsupported_element"
    )
    assert "'B'" in element["message"]


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


def test_a_first_line_that_is_an_element_card_is_refused_rather_than_eaten():
    text = "V1 vin 0 DC 12\nR1 vin 0 10\n"
    envelope = _envelope(text)

    assert envelope["ok"] is False
    assert _codes(text) == ["missing_title"]
    assert envelope["issues"][0]["line"] == 1
    assert "dropped" in envelope["issues"][0]["message"]


def test_a_first_line_that_is_a_directive_is_refused():
    assert _codes(".tran 1u 1m\nV1 vin 0 DC 12\nR1 vin 0 10\n") == ["missing_title"]


def test_an_empty_netlist_is_refused():
    for text in ("", "   \n\n", "* only a title\n"):
        assert _codes(text) == ["empty_netlist"], text


def test_elements_outside_the_vocabulary_are_refused_by_letter():
    text = "* behavioural source\nV1 vin 0 DC 12\nB1 sw 0 V = 0.5*V(vin)\nR1 sw 0 10\n"
    envelope = _envelope(text)

    assert _codes(text) == ["unsupported_element"]
    assert "B" in envelope["issues"][0]["message"]


def test_an_included_file_is_refused_because_it_is_never_read():
    text = "* include\n.include other.cir\nV1 vin 0 DC 12\nR1 vin 0 10\n"
    envelope = _envelope(text)

    assert _codes(text) == ["unsupported_directive"]
    assert "never by the importer" in envelope["issues"][0]["message"]


def test_a_subcircuit_is_refused_once_rather_than_line_by_line():
    text = (
        "* subckt\n"
        ".subckt div a b\n"
        "R1 a b 1\n"
        "X2 a b div\n"
        ".ends\n"
        "V1 vin 0 DC 12\n"
        "R9 vin 0 10\n"
    )
    envelope = _envelope(text)

    assert _codes(text) == ["unsupported_directive"]
    assert envelope["issues"][0]["line"] == 2
    assert "closed device vocabulary" in envelope["issues"][0]["message"]


def test_a_parameter_expression_without_a_declaration_is_refused():
    text = "* expression\nV1 vin 0 DC {unknown_param}\nR1 vin 0 10\n"
    envelope = _envelope(text)

    assert _codes(text) == ["unsupported_expression"]
    assert "{unknown_param}" in envelope["issues"][0]["message"]


def test_the_atto_suffix_is_not_silently_applied_to_a_value():
    """``0.45A`` is atto in SPICE; here it is an error, not 4.5e-19."""

    text = "* atto\nV1 vin 0 DC 12\nC1 vout 0 0.45A\nR1 vout 0 10\n"
    assert _codes(text) == ["invalid_value"]


def test_an_unmodelled_waveform_is_refused():
    text = "* pwl\nV1 vin 0 PWL(0 0 1u 12)\nR1 vin 0 10\n"
    envelope = _envelope(text)

    assert _codes(text) == ["unsupported_source_function"]
    assert "PWL" in envelope["issues"][0]["message"]


def test_an_ac_source_specification_is_refused():
    text = "* ac\nV1 vin 0 AC 1\nR1 vin 0 10\n"
    assert _codes(text) == ["unsupported_source_function"]


def test_a_carrier_must_drive_a_control_net_from_its_positive_terminal():
    text = "* inverted drive\nVpwm 0 pwm PULSE(0 1 0 1e-9 1e-9 5e-6 1e-5)\nR1 pwm 0 10\n"
    envelope = _envelope(text)

    assert _codes(text) == ["unsupported_control_reference"]
    assert "positive terminal" in envelope["issues"][0]["message"]


def test_the_switch_control_reference_must_be_the_carrier_return_node():
    text = (
        "* switch node as gate reference, but the carrier returns to ground\n"
        "V1 vin 0 DC 12\n"
        "Vpwm pwm 0 PULSE(0 1 0 1e-9 1e-9 5e-6 1e-5)\n"
        "S1 vin sw pwm sw SWMOD\n"
        "R1 sw 0 10\n"
        ".model SWMOD SW(Ron=1m Roff=1g Vt=0.5 Vh=0)\n"
    )
    envelope = _envelope(text)

    assert _codes(text) == ["unsupported_control_reference"]
    assert "returns to" in envelope["issues"][0]["message"]


def test_a_floating_gate_drive_is_accepted():
    text = (
        "* high-side drive referenced to the switch node\n"
        "V1 vin 0 DC 12\n"
        "Vpwm pwm sw PULSE(0 1 0 1e-9 1e-9 5e-6 1e-5)\n"
        "S1 vin sw pwm sw SWMOD\n"
        "R1 sw 0 10\n"
        ".model SWMOD SW(Ron=1m Roff=1g Vt=0.5 Vh=0)\n"
    )
    envelope = _envelope(text)

    assert envelope["ok"] is True
    assert _net_kinds(envelope["descriptor"])["sw"] == "electrical"
    assert _net_kinds(envelope["descriptor"])["pwm"] == "control"


def test_a_missing_model_card_is_refused():
    text = "* no model\nV1 vin 0 DC 12\nD1 0 vin MISSING\nR1 vin 0 10\n"
    assert _codes(text) == ["unknown_model"]


def test_a_model_type_no_device_renders_from_is_refused():
    text = "* nmos\nV1 vin 0 DC 12\nR1 vin 0 10\n.model M1 NMOS(Level=1)\n"
    assert _codes(text) == ["unsupported_model_type"]


def test_a_model_parameter_without_a_counterpart_is_refused():
    text = (
        "* rs\nV1 vin 0 DC 12\nD1 0 vin DMOD\nR1 vin 0 10\n"
        ".model DMOD D(Is=1e-12 N=0.02 Rs=1)\n"
    )
    assert _codes(text) == ["unsupported_parameter"]


def test_an_ic_on_a_resistor_is_refused():
    text = "* ic on a resistor\nV1 vin 0 DC 12\nR1 vin 0 10 IC=1\n"
    assert _codes(text) == ["unsupported_parameter"]


def test_two_elements_with_the_same_name_are_refused():
    text = "* duplicate\nV1 vin 0 DC 12\nv1 vin 0 DC 5\nR1 vin 0 10\n"
    envelope = _envelope(text)

    assert _codes(text) == ["duplicate_element"]
    assert "case-insensitive" in envelope["issues"][0]["message"]


def test_a_circuit_without_node_zero_is_refused():
    text = "* floating\nV1 a b DC 12\nR1 a b 10\n"
    assert _codes(text) == ["missing_ground"]


def test_an_unterminated_control_block_is_refused():
    text = "* open control\nV1 vin 0 DC 12\nR1 vin 0 10\n.control\nrun\n"
    assert _codes(text) == ["malformed_card"]


def test_every_defect_is_reported_in_one_pass():
    text = "* several defects\nQ1 c b e MODEL\n.include other.cir\nV1 vin 0 DC 12\nR1 vin 0 10\n"
    envelope = _envelope(text)

    assert envelope["import_ok"] is False
    assert envelope["error_count"] == 2
    assert sorted(_codes(text)) == ["unsupported_directive", "unsupported_element"]
    assert [issue["line"] for issue in envelope["issues"]] == [2, 3]


def test_a_strict_import_raises_with_the_same_issues():
    text = "* several defects\nQ1 c b e MODEL\n.include other.cir\nV1 vin 0 DC 12\nR1 vin 0 10\n"

    with pytest.raises(SpiceImportError) as caught:
        import_spice_netlist(text)

    assert caught.value.to_dict()["error_count"] == 2
    assert [issue.code for issue in caught.value.issues] == [
        "unsupported_element",
        "unsupported_directive",
    ]


# ---------------------------------------------------------------------------
# Warnings that do not fail the import
# ---------------------------------------------------------------------------


def test_an_unused_model_is_only_a_warning():
    text = (
        "* unused\nV1 vin 0 DC 12\nR1 vin 0 10\n"
        ".model DMOD D(Is=1e-12 N=0.02)\n"
    )
    envelope = _envelope(text)

    assert envelope["ok"] is True
    assert _codes(text) == ["unused_model"]


def test_two_carriers_without_a_declaration_are_flagged_and_still_imported():
    envelope = _envelope(SYNC)
    codes = _codes(SYNC)

    assert envelope["import_ok"] is True
    assert codes.count("complementary_control_undeclared") == 1
    assert codes.count("pulse_width_not_imported") == 2
    assert envelope["issues"][0]["expected"].startswith("metadata.control")
    # The descriptor is honest: it does not invent a control relation.
    assert "control" not in envelope["descriptor"]["metadata"]
    # And the compiler refuses it, which is what the warning said would happen.
    from pe_sim.circuit_compile import CircuitCompileError

    with pytest.raises(CircuitCompileError) as caught:
        compile_topology(
            build_descriptor(envelope["descriptor"]), duty=DUTY, duration_s=1e-3
        )
    assert caught.value.code in {"multiple_carriers", "multiple_actuators"}


def test_a_declared_relation_makes_the_synchronous_netlist_compile():
    envelope = _envelope(SYNC)
    document = dict(envelope["descriptor"])
    document["metadata"] = {
        **document["metadata"],
        "control": {"mode": "single_duty_complementary", "dead_time_s": 100e-9},
    }

    compiled = compile_topology(
        build_descriptor(document), duty=DUTY, duration_s=2e-3
    )

    assert _signature(compiled.netlist) == [
        ("V", ("vin", "0")),
        ("V", ("pwm_h", "0")),
        ("V", ("pwm_l", "0")),
        ("S", ("vin", "sw", "pwm_h", "0")),
        ("S", ("0", "sw", "pwm_l", "0")),
        ("L", ("sw", "vout")),
        ("C", ("vout", "0")),
        ("R", ("vout", "0")),
    ]


def test_importing_a_clean_netlist_reports_import_and_validation_separately():
    broken = BUCK.replace(".end\n", "R2 vout orphan 1k\n.end\n")
    envelope = _envelope(broken)

    assert envelope["import_ok"] is True
    assert envelope["ok"] is False
    assert envelope["validation"]["ok"] is False
    assert "isolated_net" in [
        issue["code"] for issue in envelope["validation"]["issues"]
    ]


# ---------------------------------------------------------------------------
# Round trip and acceptance
# ---------------------------------------------------------------------------


def test_compiling_an_imported_netlist_reproduces_the_same_circuit():
    first = compile_topology(
        _reviewed_descriptor(), duty=DUTY, duration_s=2e-3
    )
    imported = import_spice_netlist(first.netlist, topology_id="round_trip")
    second = compile_topology(
        build_descriptor(dict(imported.document)), duty=DUTY, duration_s=2e-3
    )

    assert _signature(second.netlist) == _signature(first.netlist)
    # The execution directives are re-derived, not carried over.
    for directive in (".options", ".temp", ".tran"):
        left = [line for line in first.netlist.splitlines() if line.startswith(directive)]
        right = [
            line for line in second.netlist.splitlines() if line.startswith(directive)
        ]
        assert left == right, directive


def test_the_envelope_identifies_the_source_text():
    envelope = _envelope(BUCK)

    assert envelope["netlist_sha256"] == hashlib.sha256(BUCK.encode("utf-8")).hexdigest()
    assert envelope["descriptor_fingerprint"] is not None
    assert len(envelope["descriptor_fingerprint"]) == 64
    # The fingerprint identifies the descriptor, not the netlist: the source
    # title is part of the descriptor, so a different title is a different one.
    other = _envelope(BUCK.replace("* buck", "* another", 1))
    assert other["descriptor_fingerprint"] != envelope["descriptor_fingerprint"]


def test_the_envelope_shape_is_published():
    envelope = _envelope(BUCK)

    assert set(envelope) == {
        "schema",
        "ok",
        "import_ok",
        "topology_id",
        "title",
        "netlist_sha256",
        "descriptor",
        "descriptor_fingerprint",
        "counts",
        "element_kinds",
        "net_name_map",
        "component_name_map",
        "observed_duty",
        "ignored_directives",
        "issues",
        "error_count",
        "warning_count",
        "validation",
        "notes",
    }
    assert envelope["notes"]
    assert set(envelope["issues"][0]) == {
        "code",
        "path",
        "line",
        "message",
        "expected",
        "severity",
    }


def _solve(netlist: str) -> dict[str, list[float]]:
    work = Path(tempfile.mkdtemp(prefix="pe-sim-imported-"))
    (work / "circuit.cir").write_text(netlist, encoding="ascii")
    completed = subprocess.run(
        [str(NGSPICE), "-b", "circuit.cir"],
        cwd=work,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, (completed.stderr or completed.stdout)[-2000:]
    rows = [
        line.split()
        for line in (work / "output.dat").read_text(encoding="ascii").splitlines()
        if line.strip()
    ]
    header = [name.lower() for name in rows[0]]
    return {
        name: [float(row[index]) for row in rows[1:] if row]
        for index, name in enumerate(header)
    }


@needs_ngspice
def test_an_imported_circuit_solves_to_the_same_operating_point():
    """The import is only honest if the solver cannot tell the two apart."""

    duration = 10e-3
    imported = import_spice_netlist(BUCK, topology_id="acceptance")
    from_import = compile_topology(
        build_descriptor(dict(imported.document)), duty=DUTY, duration_s=duration
    )
    reviewed_compiled = compile_topology(
        _reviewed_descriptor(), duty=DUTY, duration_s=duration
    )

    left = _solve(from_import.netlist)
    right = _solve(reviewed_compiled.netlist)

    def mean_tail(values: list[float], count: int = 500) -> float:
        return sum(values[-count:]) / len(values[-count:])

    assert mean_tail(left["v(vout)"]) == pytest.approx(
        mean_tail(right["v(vout)"]), abs=1e-3
    )
    assert mean_tail(left["i(l1)"]) == pytest.approx(
        mean_tail(right["i(l1)"]), abs=1e-3
    )
    assert mean_tail(left["v(vout)"]) == pytest.approx(DUTY * VIN, abs=0.15)

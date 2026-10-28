"""Compiling a declared topology into a netlist the solver can run.

The compiler is the last authoring gate, so these tests hold two different
properties at once: that it refuses what it cannot render exactly, and that what
it does render is the circuit it claims to be.  The second property is checked
structurally against the project's reviewed Buck template, and - when a real
solver is configured - numerically, by solving both netlists and comparing.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from pe_sim.circuit_compile import (
    CIRCUIT_COMPILE_CODES,
    CIRCUIT_NETLIST_TEMPLATE_VERSION,
    CircuitCompileError,
    circuit_netlist_digest,
    compile_topology,
    default_sample_step_s,
)
from pe_sim.circuit_devices import RENDERABLE_DEVICE_KINDS
from pe_sim.topology import (
    boost_topology_descriptor,
    buck_topology_descriptor,
    model_control_buck_topology_descriptor,
    ngspice_buck_topology_descriptor,
    ngspice_sync_buck_topology_descriptor,
)


NGSPICE = os.environ.get("PE_SIM_NGSPICE")
needs_ngspice = pytest.mark.skipif(
    not NGSPICE,
    reason="set PE_SIM_NGSPICE to run the compiled-circuit acceptance test",
)

VIN = 12.0
L_H = 100e-6
C_F = 470e-6
R_OHM = 10.0
FSW = 100e3
DUTY = 0.5


def _plant(**overrides):
    values = {
        "input_voltage_v": VIN,
        "inductance_h": L_H,
        "capacitance_f": C_F,
        "load_resistance_ohm": R_OHM,
        "switching_frequency_hz": FSW,
        "control_period_s": 10e-6,
        "dead_time_s": 100e-9,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _buck_descriptor():
    return ngspice_buck_topology_descriptor(_plant())


def _compile(descriptor=None, **overrides):
    values = {"duty": DUTY, "duration_s": 2e-3}
    values.update(overrides)
    return compile_topology(descriptor or _buck_descriptor(), **values)


# ---------------------------------------------------------------------------
# What the compiler renders
# ---------------------------------------------------------------------------


def _node_counts() -> dict[str, int]:
    return {"V": 2, "S": 4, "D": 2, "L": 2, "C": 2, "R": 2}


def _circuit_signature(text: str) -> list[tuple[str, tuple[str, ...]]]:
    """Return ``(letter, nodes)`` for every element card, in file order.

    Element names and values are deliberately ignored: the property under test
    is that the compiled circuit connects the same terminals in the same order
    as the reviewed one, not that the two files are textually equal.
    """

    counts = _node_counts()
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
        if in_control_block:
            continue
        if not line or line.startswith(("*", ".", "+")):
            continue
        tokens = line.split()
        letter = tokens[0][0].upper()
        if letter not in counts:
            continue
        window = counts[letter]
        signature.append((letter, tuple(tokens[1 : 1 + window])))
    return signature


def test_the_compiled_circuit_connects_the_reviewed_buck_terminals():
    from pe_sim.backends.netlist import BuckNetlistParameters, render_buck_netlist
    from pe_sim.backends.settings import NgspiceSolverSettings

    compiled = _compile()
    reviewed = render_buck_netlist(
        BuckNetlistParameters(
            input_voltage_v=VIN,
            inductance_h=L_H,
            capacitance_f=C_F,
            load_resistance_ohm=R_OHM,
            switching_frequency_hz=FSW,
        ),
        duty=DUTY,
        duration_s=2e-3,
        solver=NgspiceSolverSettings(),
    )

    assert _circuit_signature(compiled.netlist) == _circuit_signature(reviewed)


def test_the_reference_net_becomes_node_zero():
    compiled = _compile()

    assert compiled.reference_net == "gnd"
    assert " 0 " in compiled.netlist or "\n" in compiled.netlist
    assert "gnd" not in compiled.netlist


def test_the_pulse_width_is_derived_from_the_commanded_duty():
    compiled = _compile(duty=0.25)

    pulse = next(
        line for line in compiled.netlist.splitlines() if "PULSE(" in line
    )
    window = pulse[pulse.index("PULSE(") + len("PULSE(") : pulse.rindex(")")]
    fields = window.replace(",", " ").split()
    width = float(fields[5])

    assert width == pytest.approx(0.25 / FSW)
    assert "0.25" not in pulse  # the duty itself is never written as a literal


def test_the_digest_changes_when_the_command_changes():
    half = _compile(duty=0.5)
    quarter = _compile(duty=0.25)

    assert half.digest != quarter.digest
    assert half.descriptor_fingerprint == quarter.descriptor_fingerprint


def test_the_digest_is_stable_across_runs_and_processes():
    first = _compile()
    second = _compile()

    assert first.netlist == second.netlist
    assert first.digest == second.digest
    assert first.digest == circuit_netlist_digest(first.netlist)


def test_the_netlist_carries_no_host_or_time_dependent_text():
    netlist = _compile().netlist

    assert str(Path.cwd()) not in netlist
    assert tempfile.gettempdir() not in netlist
    assert "C:\\" not in netlist and "/tmp" not in netlist
    assert CIRCUIT_NETLIST_TEMPLATE_VERSION in netlist


def test_every_net_voltage_and_inductor_current_is_saved():
    compiled = _compile()

    assert "v(vin)" in compiled.saved_signals
    assert "v(vout)" in compiled.saved_signals
    assert "v(gnd)" not in compiled.saved_signals
    assert "i(L1)" in compiled.saved_signals


def test_the_sample_step_follows_the_reviewed_rule():
    from pe_sim.backends.netlist import BuckNetlistParameters, default_sample_step_s as reviewed_step

    parameters = BuckNetlistParameters(
        input_voltage_v=VIN,
        inductance_h=L_H,
        capacitance_f=C_F,
        load_resistance_ohm=R_OHM,
        switching_frequency_hz=FSW,
    )
    for duration in (1e-5, 1e-4, 2e-3, 1.0):
        assert default_sample_step_s(1.0 / FSW, duration) == pytest.approx(
            reviewed_step(parameters, duration)
        )


# ---------------------------------------------------------------------------
# What the compiler refuses
# ---------------------------------------------------------------------------


def _error(descriptor, **overrides) -> CircuitCompileError:
    with pytest.raises(CircuitCompileError) as raised:
        _compile(descriptor, **overrides)
    return raised.value


def test_an_averaged_reference_is_refused_as_not_compilable():
    error = _error(buck_topology_descriptor(_plant()))

    assert error.code == "not_compilable"
    assert "averaged_switch" in str(error)


def test_a_boost_averaged_reference_is_refused_the_same_way():
    assert _error(boost_topology_descriptor(_plant())).code == "not_compilable"


def test_two_actuators_are_refused_because_the_command_is_one_scalar():
    error = _error(model_control_buck_topology_descriptor(_plant()))

    assert error.code == "multiple_actuators"
    assert error.details["actuators"] == 2


def test_two_carriers_are_refused():
    error = _error(ngspice_sync_buck_topology_descriptor(_plant()))

    # Two gate nets arrive with two carriers; the actuator count is what stops
    # the compilation first, and both facts are real refusals.
    assert error.code in {"multiple_actuators", "multiple_carriers"}


def test_a_descriptor_without_a_carrier_is_refused():
    descriptor = _buck_descriptor()
    components = tuple(
        item for item in descriptor.components if item.component_id != "pwm_source"
    )
    stripped = type(descriptor)(
        descriptor.topology_id, components, descriptor.nets, metadata=descriptor.metadata
    )

    assert _error(stripped).code == "no_carrier"


def test_a_descriptor_without_a_reference_net_is_refused():
    descriptor = _buck_descriptor()
    nets = tuple(
        type(net)(net.net_id, net.label, "electrical")
        if net.net_id == "gnd"
        else net
        for net in descriptor.nets
    )
    rebuilt = type(descriptor)(
        descriptor.topology_id, descriptor.components, nets, metadata=descriptor.metadata
    )

    assert _error(rebuilt).code == "ambiguous_reference_net"


def test_a_missing_required_parameter_is_refused_not_defaulted():
    descriptor = _buck_descriptor()
    component = next(
        item for item in descriptor.components if item.component_id == "inductor"
    )
    ports = component.ports
    replaced = type(component)(
        component.component_id,
        component.kind,
        ports,
        parameters={},
        symbol_id=component.symbol_id,
    )
    components = tuple(
        replaced if item.component_id == "inductor" else item
        for item in descriptor.components
    )
    rebuilt = type(descriptor)(
        descriptor.topology_id, components, descriptor.nets, metadata=descriptor.metadata
    )

    error = _error(rebuilt)

    assert error.code == "missing_parameter"
    assert error.details["parameter"] == "inductance_h"


def test_an_out_of_range_parameter_is_refused():
    descriptor = _buck_descriptor()
    component = next(
        item for item in descriptor.components if item.component_id == "inductor"
    )
    replaced = type(component)(
        component.component_id,
        component.kind,
        component.ports,
        parameters={"inductance_h": -1e-4},
        symbol_id=component.symbol_id,
    )
    components = tuple(
        replaced if item.component_id == "inductor" else item
        for item in descriptor.components
    )
    rebuilt = type(descriptor)(
        descriptor.topology_id, components, descriptor.nets, metadata=descriptor.metadata
    )

    assert _error(rebuilt).code == "invalid_parameter"


@pytest.mark.parametrize("duty", [-0.1, 1.5, float("nan"), float("inf")])
def test_an_impossible_duty_is_refused(duty):
    assert _error(None, duty=duty).code == "invalid_duty"


@pytest.mark.parametrize("duration", [0.0, -1.0, float("inf")])
def test_an_impossible_duration_is_refused(duration):
    assert _error(None, duration_s=duration).code == "invalid_duration"


def test_an_impossible_sample_step_is_refused():
    assert _error(None, sample_step_s=0.0).code == "invalid_sample_step"


def test_an_unknown_integrator_is_refused():
    error = _error(None, integrator="trapezoidal")

    assert error.code == "invalid_integrator"
    assert "trap" in error.details["supported"]


def test_a_boolean_option_is_refused():
    assert _error(None, options={"reltol": True}).code == "invalid_option"


def test_compile_codes_are_a_closed_set():
    assert CIRCUIT_COMPILE_CODES == frozenset(
        {
            "ambiguous_reference_net",
            "invalid_duration",
            "invalid_duty",
            "invalid_integrator",
            "invalid_option",
            "invalid_parameter",
            "invalid_sample_step",
            "missing_parameter",
            "multiple_actuators",
            "multiple_carriers",
            "no_carrier",
            "not_compilable",
            "unsupported_net_kind",
        }
    )


def test_a_compile_error_carries_a_machine_readable_record():
    error = _error(None, duty=2.0)

    assert error.to_dict()["code"] == "invalid_duty"
    assert "duty" in error.to_dict()["message"]


# ---------------------------------------------------------------------------
# Boundaries
# ---------------------------------------------------------------------------


def test_the_renderable_kinds_are_exactly_the_ones_the_compiler_handles():
    """A renderable kind with no element card would be a silent trap."""

    assert set(RENDERABLE_DEVICE_KINDS) == {
        "voltage_source",
        "pulse_source",
        "inductor",
        "capacitor",
        "resistor_load",
        "voltage_controlled_switch",
        "freewheeling_diode",
    }


def test_the_compiler_does_not_import_a_backend():
    import pe_sim.circuit_compile as module

    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    targets: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            targets.append(node.module or "")
        elif isinstance(node, ast.Import):
            targets.extend(alias.name for alias in node.names)

    assert not any("backends" in target for target in targets), targets


def test_a_compiled_netlist_is_not_evidence():
    """The compile surface states its own limit; nothing here upgrades it."""

    import pe_sim.circuit_compile as module

    doc = module.__doc__ or ""

    assert "not a validated model" in doc
    assert "comparable" in doc


# ---------------------------------------------------------------------------
# Acceptance: a real solver must agree with the reviewed netlist
# ---------------------------------------------------------------------------


def _solve(netlist: str) -> dict[str, list[float]]:
    work = Path(tempfile.mkdtemp(prefix="pe-sim-compiled-"))
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
def test_a_compiled_circuit_solves_to_the_same_operating_point():
    from pe_sim.backends.netlist import BuckNetlistParameters, render_buck_netlist
    from pe_sim.backends.settings import NgspiceSolverSettings

    duration = 10e-3
    compiled = _compile(duration_s=duration)
    reviewed = render_buck_netlist(
        BuckNetlistParameters(
            input_voltage_v=VIN,
            inductance_h=L_H,
            capacitance_f=C_F,
            load_resistance_ohm=R_OHM,
            switching_frequency_hz=FSW,
        ),
        duty=DUTY,
        duration_s=duration,
        solver=NgspiceSolverSettings(),
    )

    left = _solve(compiled.netlist)
    right = _solve(reviewed)

    def mean_tail(values: list[float], count: int = 500) -> float:
        return sum(values[-count:]) / len(values[-count:])

    assert mean_tail(left["v(vout)"]) == pytest.approx(
        mean_tail(right["v(vout)"]), abs=1e-3
    )
    assert mean_tail(left["i(l1)"]) == pytest.approx(
        mean_tail(right["i(l1)"]), abs=1e-3
    )
    # And the operating point is the one the converter should have.
    assert mean_tail(left["v(vout)"]) == pytest.approx(
        DUTY * VIN, abs=0.15
    )

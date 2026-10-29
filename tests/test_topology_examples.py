"""The shipped topology examples must stay expressible.

An example is a reference design an agent pattern-matches against, so a stale
example is worse than no example: it teaches a shape the compiler no longer
accepts.  This test is the guard that lets the directory grow without becoming a
pile of documents nobody checked.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

import pytest

from pe_sim.circuit_compile import compile_topology
from pe_sim.topology_validation import build_descriptor, topology_validation_document


NGSPICE = os.environ.get("PE_SIM_NGSPICE")
needs_ngspice = pytest.mark.skipif(
    not NGSPICE,
    reason="set PE_SIM_NGSPICE to solve the shipped examples",
)


EXAMPLES = Path(__file__).resolve().parents[1] / "examples" / "topologies"

#: The examples that must exist.  Adding a file without listing it here is fine;
#: listing one and deleting it is a failure, because a README row would then
#: point at nothing.
REQUIRED = (
    "buck.json",
    "sync_buck.json",
    "inverting_buck_boost.json",
    "interleaved_buck.json",
)


def _documents() -> list[tuple[str, dict]]:
    return [
        (path.name, json.loads(path.read_text(encoding="utf-8")))
        for path in sorted(EXAMPLES.glob("*.json"))
    ]


def test_the_documented_examples_exist_and_there_are_no_others_unlisted():
    present = {name for name, _ in _documents()}

    assert set(REQUIRED) <= present
    readme = (EXAMPLES / "README.md").read_text(encoding="utf-8")
    for name in present:
        assert name in readme, f"{name} is not mentioned in the examples README"


@pytest.mark.parametrize(
    "name,document", _documents(), ids=[name for name, _ in _documents()]
)
def test_every_example_validates_without_issues(name, document):
    envelope = topology_validation_document(document)

    assert envelope["ok"] is True, (name, envelope["issues"])
    assert envelope["issues"] == [], name


@pytest.mark.parametrize(
    "name,document", _documents(), ids=[name for name, _ in _documents()]
)
def test_every_example_compiles_at_the_running_point_it_declares(name, document):
    """An example states the command that shows its own operating point."""

    compile_args = document["metadata"]["example"]["compile"]

    compiled = compile_topology(
        build_descriptor(document),
        duty=float(compile_args["duty"]),
        duration_s=float(compile_args["duration_s"]),
    )

    assert compiled.topology_id == document["topology_id"]
    assert compiled.netlist.startswith("* pe-sim compiled circuit")
    # The duty is a command, so it must not be baked into the document.
    assert "duty" not in json.dumps(document["components"])


def test_an_example_is_an_authored_document_not_a_recorded_one():
    """A fingerprint here would claim a history the file does not have."""

    for name, document in _documents():
        assert "fingerprint" not in document, name


# ---------------------------------------------------------------------------
# The multiphase example must interleave in a real solve, not just compile
# ---------------------------------------------------------------------------


def _solve(netlist: str) -> tuple[list[float], dict[str, list[float]]]:
    work = Path(tempfile.mkdtemp(prefix="pe-sim-example-"))
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
    times = [float(row[0]) for row in rows[1:] if row]
    signals = {
        name: [float(row[index]) for row in rows[1:] if row]
        for index, name in enumerate(header)
        if index > 0
    }
    return times, signals


def _rising_edges(times, values, threshold=0.5) -> list[float]:
    return [
        times[index]
        for index in range(1, len(values))
        if values[index - 1] <= threshold < values[index]
    ]


@needs_ngspice
def test_the_interleaved_example_really_interleaves():
    """The shipped multiphase example must solve into evenly spread gates.

    Compiling proves the text is well formed.  This solves the example and reads
    the gate nets back out of the samples, so "one duty drives three phases
    spread by a third of a period" is checked against the simulator rather than
    against the compiler's own record of what it wrote.
    """

    document = json.loads(
        (EXAMPLES / "interleaved_buck.json").read_text(encoding="utf-8")
    )
    descriptor = build_descriptor(document)
    command = document["metadata"]["example"]["compile"]
    duty = float(command["duty"])
    frequency = float(
        next(
            component.parameters["switching_frequency_hz"]
            for component in descriptor.components
            if component.kind == "pulse_source"
        )
    )
    period = 1.0 / frequency
    compiled = compile_topology(
        descriptor,
        duty=duty,
        duration_s=200e-6,
        initial_conditions={
            "inductor_1": 0.4,
            "inductor_2": 0.4,
            "inductor_3": 0.4,
            "output_capacitor": duty * 12.0,
        },
    )

    times, signals = _solve(compiled.netlist)

    offsets = []
    for net_id in sorted(dict(compiled.gate_first_rise_s)):
        values = signals[f"v({net_id})"]
        edges = _rising_edges(times, values)
        assert edges, net_id
        # The phase offset, measured one full period in so a pulse that straddles
        # the window start cannot be mistaken for the first of the cycle.
        later = [edge for edge in edges if edge > period]
        assert later, net_id
        offsets.append(later[0] % period)
        high = sum(1 for value in values if value > 0.5)
        assert high / len(values) == pytest.approx(duty, abs=0.02), net_id

    offsets.sort()
    spacing = period / len(offsets)
    assert len(offsets) == 3
    assert offsets[1] - offsets[0] == pytest.approx(spacing, rel=0.05)
    assert offsets[2] - offsets[1] == pytest.approx(spacing, rel=0.05)

    mean_output = sum(signals["v(vout)"]) / len(signals["v(vout)"])
    assert mean_output == pytest.approx(duty * 12.0, rel=0.05)

"""The shipped topology examples must stay expressible.

An example is a reference design an agent pattern-matches against, so a stale
example is worse than no example: it teaches a shape the compiler no longer
accepts.  This test is the guard that lets the directory grow without becoming a
pile of documents nobody checked.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pe_sim.circuit_compile import compile_topology
from pe_sim.topology_validation import build_descriptor, topology_validation_document


EXAMPLES = Path(__file__).resolve().parents[1] / "examples" / "topologies"

#: The examples that must exist.  Adding a file without listing it here is fine;
#: listing one and deleting it is a failure, because a README row would then
#: point at nothing.
REQUIRED = ("buck.json", "sync_buck.json", "inverting_buck_boost.json")


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

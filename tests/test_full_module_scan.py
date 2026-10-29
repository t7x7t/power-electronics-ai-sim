"""A whole-package scan: every module, vocabulary, schema, command and link.

Every other test in this suite answers a question about one behaviour.  This
file answers a question about the *package*: after dozens of increments, is the
set of parts still one coherent whole?  It is the check that would have caught
the failures a reader cannot see - a module nobody classifies, a refusal code
nobody pins, a schema tag that exists only in a doc, a CLI command that exists
only in the parser.

The scan deliberately links things that live in different files:

- **modules to layers.**  Every module in ``src/pe_sim`` is listed in
  :data:`MODULES`.  Adding a module therefore fails this test until someone
  writes its name down, which is the moment to ask whether an authoring module
  just acquired a solver dependency.
- **modules to their import-time dependencies.**  No module may import a
  third-party package at import time, and only the pinned composition roots may
  import :mod:`pe_sim.backends`.  A descriptor the user authored must never
  need a simulator to be *parsed*.
- **code to the refusal vocabulary.**  Every public ``*_CODES`` set is pinned
  here, so a code cannot be invented in one module and forgotten by the clients
  that branch on it.
- **code to documentation.**  Every ``pe-sim.<name>.vN`` schema literal in the
  source is pinned, so a document and a payload cannot drift apart silently.
- **commands to documentation.**  Every subcommand the parser accepts is
  mentioned in the README or the docs, because a command an agent cannot find
  is a capability the agent does not have.

The scan is read-only and imports each module exactly once.  It asserts facts
that must be *currently true*; nothing here defines new behaviour.
"""

from __future__ import annotations

import ast
import contextlib
import importlib
import io
import re
import sys
from pathlib import Path

import pytest


REPOSITORY = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPOSITORY / "src"
PACKAGE_ROOT = SOURCE_ROOT / "pe_sim"
DOCS_ROOT = REPOSITORY / "docs"

#: Every module in the package, by dotted name.  A new file must appear here.
MODULES = (
    "pe_sim",
    "pe_sim._version",
    "pe_sim.ac_analysis",
    "pe_sim.applicability",
    "pe_sim.artifacts",
    "pe_sim.attestation",
    "pe_sim.backends",
    "pe_sim.backends.circuit_plant",
    "pe_sim.backends.discovery",
    "pe_sim.backends.model_control_buck",
    "pe_sim.backends.netlist",
    "pe_sim.backends.netlist_builder",
    "pe_sim.backends.ngspice_plants",
    "pe_sim.backends.process",
    "pe_sim.backends.registry",
    "pe_sim.backends.settings",
    "pe_sim.backends.sync_buck",
    "pe_sim.backends.whole_run_engine",
    "pe_sim.backends.wrdata",
    "pe_sim.baseline",
    "pe_sim.builtin_controllers",
    "pe_sim.builtin_plants",
    "pe_sim.cancellation",
    "pe_sim.capabilities",
    "pe_sim.capability_analysis",
    "pe_sim.catalog",
    "pe_sim.checks",
    "pe_sim.circuit_compile",
    "pe_sim.circuit_devices",
    "pe_sim.claim",
    "pe_sim.cli",
    "pe_sim.compatibility",
    "pe_sim.conformance",
    "pe_sim.contracts",
    "pe_sim.controller_registry",
    "pe_sim.controllers",
    "pe_sim.credibility",
    "pe_sim.default_contracts",
    "pe_sim.derive",
    "pe_sim.dirty",
    "pe_sim.environment",
    "pe_sim.error_codes",
    "pe_sim.evidence",
    "pe_sim.frequency_domain",
    "pe_sim.identity",
    "pe_sim.impedance_analysis",
    "pe_sim.inspection",
    "pe_sim.jsonutil",
    "pe_sim.lossy_converter",
    "pe_sim.model_capabilities",
    "pe_sim.model_claim",
    "pe_sim.multiphase_plants",
    "pe_sim.numerical_health",
    "pe_sim.outputs",
    "pe_sim.parameter_confirmation",
    "pe_sim.parameter_view",
    "pe_sim.plant_parameters",
    "pe_sim.plant_registry",
    "pe_sim.postprocess",
    "pe_sim.protection",
    "pe_sim.provenance",
    "pe_sim.public_api",
    "pe_sim.qualification",
    "pe_sim.qualification_rules",
    "pe_sim.reference_plants",
    "pe_sim.reproducibility",
    "pe_sim.review",
    "pe_sim.run_events",
    "pe_sim.run_store",
    "pe_sim.runner",
    "pe_sim.runner.audit",
    "pe_sim.runner.coordinator",
    "pe_sim.runner.lifecycle",
    "pe_sim.runner.policies",
    "pe_sim.runtime",
    "pe_sim.safety",
    "pe_sim.schema_registry",
    "pe_sim.schemas",
    "pe_sim.spec_validation",
    "pe_sim.spice_import",
    "pe_sim.study",
    "pe_sim.study_runner",
    "pe_sim.templates",
    "pe_sim.time_model",
    "pe_sim.topology",
    "pe_sim.topology_validation",
    "pe_sim.traces",
    "pe_sim.visualization_service",
    "pe_sim.waveform_metrics",
    "pe_sim.whole_run_route",
    "pe_sim.workbench_export",
)

#: The only modules allowed to import the solver layer at import time.  These
#: are the composition roots: the package facade, the two builtin-registration
#: modules, the capability probe and the CLI.  Everything else is a layer that
#: must stay answerable without a simulator.
BACKEND_IMPORTERS = frozenset(
    {
        "pe_sim",
        "pe_sim.builtin_plants",
        "pe_sim.capabilities",
        "pe_sim.cli",
        "pe_sim.model_capabilities",
    }
)

#: Every public refusal vocabulary in the package, keyed by constant name.
#: Re-exports are collapsed because the scan unions the values of every
#: definition of that name.
CODE_VOCABULARIES = {
    "CIRCUIT_COMPILE_CODES": {
        "ambiguous_complementary_control",
        "ambiguous_reference_net",
        "invalid_carrier_phase",
        "invalid_control_declaration",
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
    },
    "IMPORT_ISSUE_CODES": {
        "complementary_control_undeclared",
        "duplicate_element",
        "duplicate_model",
        "empty_netlist",
        "invalid_value",
        "malformed_card",
        "missing_ground",
        "missing_title",
        "pulse_width_not_imported",
        "unknown_model",
        "unsupported_control_reference",
        "unsupported_directive",
        "unsupported_element",
        "unsupported_expression",
        "unsupported_model_type",
        "unsupported_parameter",
        "unsupported_source_function",
        "unused_model",
    },
    "KERNEL_FAILURE_CODES": {
        "action_invalid",
        "actuator_outside_envelope",
        "actuator_undeclared",
        "backend_process_failure",
        "backend_timeout",
        "cancelled",
        "capability_configuration_mismatch",
        "capability_mismatch",
        "capability_runtime_unavailable",
        "ccm_boundary_violated",
        "check_failed",
        "checker_error",
        "control_period_too_short",
        "data_invalid",
        "envelope_declaration_invalid",
        "envelope_undeclared",
        "external_input_changed",
        "future_observation",
        "insufficient_integration_substeps",
        "insufficient_sample_resolution",
        "integration_step_too_large",
        "interrupted",
        "invalid_measurement",
        "invalid_observation_age",
        "invalid_schedule_value",
        "load_too_close_to_switch_off_resistance",
        "measurement_out_of_range",
        "missing_capability",
        "missing_measurement",
        "model_numerical_error",
        "nonfinite_observation",
        "numerical_nonconvergence",
        "out_of_envelope",
        "parameter_provenance_invalid",
        "parameter_provenance_unknown",
        "parameter_unreviewed",
        "plant_advance_failure",
        "plant_execution_failure",
        "pulse_width_below_template_resolution",
        "qualification_failure",
        "runtime_error",
        "safety_violation",
        "simulation_execution_error",
        "stale_observation",
        "timestamp_violation",
        "timing_violation",
        "trace_contract_violation",
        "unit_mismatch",
        "unsupported_capability",
    },
    "REQUEST_REJECTION_CODES": {
        "backend_mismatch",
        "formal_comparison_rejected",
        "invalid_request",
        "run_inspection_rejected",
        "unknown_backend",
        "unknown_plant",
    },
    "SPEC_ISSUE_CODES": {
        "exploratory_contract_default",
        "invalid_document",
        "invalid_parameter_value",
        "schema_violation",
        "unknown_controller",
        "unknown_controller_config",
        "unknown_parameter",
        "unknown_plant",
        "unknown_schedule_key",
    },
    "TOPOLOGY_ISSUE_CODES": {
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
        "switch_threshold_above_carrier_amplitude",
        "unknown_net",
        "unknown_parameter",
        "unknown_port",
        "unsupported_device_kind",
        "unsupported_net_kind",
        "voltage_source_loop",
    },
    "WARNING_IMPORT_CODES": {
        "complementary_control_undeclared",
        "pulse_width_not_imported",
        "unused_model",
    },
    "WARNING_ISSUE_CODES": {
        "actuator_missing",
        "multiple_control_nets",
        "no_output_load",
        "not_compilable",
        "switch_threshold_above_carrier_amplitude",
    },
}

#: Every ``pe-sim.<name>.vN`` tag the source writes.  A payload schema that
#: appears here has a producer; one that only appears in a document does not.
SCHEMAS = frozenset(
    {
        "pe-sim.ac-analysis.v1",
        "pe-sim.adapter-verification.v1",
        "pe-sim.attestation-anchor.v1",
        "pe-sim.attestation-verification.v1",
        "pe-sim.attestation-write.v1",
        "pe-sim.attestation.v1",
        "pe-sim.capabilities.v1",
        "pe-sim.capability-analysis.v1",
        "pe-sim.capability-report.v1",
        "pe-sim.circuit-devices.v1",
        "pe-sim.claim.v1",
        "pe-sim.compatibility.v1",
        "pe-sim.derived-report.v1",
        "pe-sim.impedance-analysis.v1",
        "pe-sim.model-capability.v1",
        "pe-sim.parameter-confirmation-verification.v1",
        "pe-sim.parameter-confirmation.v1",
        "pe-sim.parameter-view.v1",
        "pe-sim.plant-catalog.v1",
        "pe-sim.plant-parameters.v1",
        "pe-sim.public-api.v1",
        "pe-sim.rejection.v1",
        "pe-sim.reviewed-identities.v1",
        "pe-sim.run-catalog.v1",
        "pe-sim.run-demo.v1",
        "pe-sim.run-event.v1",
        "pe-sim.run-inspection.v1",
        "pe-sim.run-result.v1",
        "pe-sim.run-review.v1",
        "pe-sim.schema-registry.v1",
        "pe-sim.spec-validation.v1",
        "pe-sim.spice-import.v1",
        "pe-sim.study-aggregate.v1",
        "pe-sim.study-report.v1",
        "pe-sim.study.v1",
        "pe-sim.topology-samples.v1",
        "pe-sim.topology-simulation.v1",
        "pe-sim.topology-validation.v1",
        "pe-sim.trace-contract.v1",
        "pe-sim.visualization-service.v1",
        "pe-sim.waveform-metrics.v1",
    }
)

_SCHEMA_RE = re.compile(r"^pe-sim\.[a-z0-9]+(?:[-.][a-z0-9]+)*\.v[0-9]+$")
_CODE_RE = re.compile(r"^[a-z][a-z0-9_]*$")

#: Every overlap between two *independent* refusal vocabularies, with the
#: reason it is deliberate.  The map is a decision record, not a convenience: a
#: new overlap fails the scan until it is written here and justified.  Pairs
#: where one vocabulary is a declared subset of the other are excluded, because
#: that overlap is already asserted by ``..._warning_vocabulary_names_codes...``.
KNOWN_CODE_OVERLAPS: dict[tuple[str, str], set[str]] = {
    # A descriptor the compiler cannot render is warned about by the validator
    # and refused by the compiler, and both layers name the same missing field
    # with the same word.
    ("CIRCUIT_COMPILE_CODES", "TOPOLOGY_ISSUE_CODES"): {
        "missing_parameter",
        "not_compilable",
        "unsupported_net_kind",
    },
    # A topology document and an experiment document are validated against the
    # same JSON Schema vocabulary, so the shape words are shared on purpose.
    ("SPEC_ISSUE_CODES", "TOPOLOGY_ISSUE_CODES"): {
        "invalid_document",
        "invalid_parameter_value",
        "schema_violation",
        "unknown_parameter",
    },
    # ``describe`` refuses an unknown plant id as a request error, and the
    # validator reports the same mistake as an issue in the document.
    ("REQUEST_REJECTION_CODES", "SPEC_ISSUE_CODES"): {"unknown_plant"},
}

#: Vocabularies that are declared subsets of another one; their overlaps are
#: implied and asserted separately.
_SUBSET_VOCABULARIES = frozenset({"WARNING_IMPORT_CODES", "WARNING_ISSUE_CODES"})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _module_name(path: Path) -> str:
    relative = path.relative_to(SOURCE_ROOT).with_suffix("")
    parts = list(relative.parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _discovered_modules() -> list[str]:
    return sorted(_module_name(path) for path in PACKAGE_ROOT.rglob("*.py"))


def _parse(path: Path) -> ast.Module:
    # ``utf-8-sig`` because at least one historical file carries a BOM, and the
    # scan must read what the interpreter reads.
    return ast.parse(path.read_text(encoding="utf-8-sig"))


def _module_level_imports(tree: ast.Module) -> set[str]:
    found: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            prefix = "." * node.level
            found.add(f"{prefix}{node.module or ''}")
    return found


def _all_imports(tree: ast.Module) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            prefix = "." * node.level
            found.add(f"{prefix}{node.module or ''}")
    return found


def _touches_backends(name: str) -> bool:
    return name.lstrip(".").startswith("backends")


def _cli_usage() -> str:
    from pe_sim.cli import main

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        with pytest.raises(SystemExit) as raised:
            main(["--help"])
    assert raised.value.code == 0
    return buffer.getvalue()


def _cli_commands() -> list[str]:
    match = re.search(r"\{([a-z0-9,\-]+)\}", _cli_usage())
    assert match is not None, "the top-level usage line lists no commands"
    return match.group(1).split(",")


def _documentation() -> str:
    parts = [(REPOSITORY / "README.md").read_text(encoding="utf-8")]
    parts.extend(
        path.read_text(encoding="utf-8", errors="ignore")
        for path in sorted(DOCS_ROOT.glob("*.md"))
    )
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# The inventory: every module is known, and every module's layer is respected
# ---------------------------------------------------------------------------


def test_every_module_in_the_package_is_listed():
    discovered = _discovered_modules()

    missing = sorted(set(discovered) - set(MODULES))
    stale = sorted(set(MODULES) - set(discovered))
    assert not missing, (
        "new module(s) not classified by the scan: "
        + ", ".join(missing)
        + ".  Add them to MODULES and ask whether an authoring layer just "
        "acquired a solver dependency."
    )
    assert not stale, "MODULES lists module(s) that no longer exist: " + ", ".join(
        stale
    )
    assert discovered == sorted(MODULES)
    assert len(set(MODULES)) == len(MODULES), "MODULES contains a duplicate"


def test_no_module_imports_a_third_party_package_at_import_time():
    """Parsing a descriptor must never require a simulator.

    Function-local imports are allowed and already used for ``numpy`` and
    ``PySpice``: an optional acceleration or adapter binding may be imported
    when the code path that needs it runs, not when the package is read.
    """

    offenders: dict[str, list[str]] = {}
    for path in sorted(PACKAGE_ROOT.rglob("*.py")):
        third_party = sorted(
            name
            for name in _module_level_imports(_parse(path))
            if not name.startswith(".")
            and name.split(".")[0] not in sys.stdlib_module_names
            and not name.startswith("pe_sim")
        )
        if third_party:
            offenders[_module_name(path)] = third_party

    assert offenders == {}, (
        "module-level third-party imports found: "
        f"{offenders}; move them inside the function that needs them"
    )


def test_only_the_composition_roots_import_the_solver_layer_at_import_time():
    offenders = sorted(
        _module_name(path)
        for path in PACKAGE_ROOT.rglob("*.py")
        if any(_touches_backends(name) for name in _module_level_imports(_parse(path)))
        and _module_name(path) not in BACKEND_IMPORTERS
    )

    assert offenders == [], (
        "these modules import pe_sim.backends at import time but are not "
        f"declared composition roots: {offenders}"
    )


def test_the_authoring_layer_never_reaches_a_backend_even_lazily():
    """The modules an agent's document flows through must be backend-free.

    ``circuit_devices`` -> ``topology_validation`` -> ``circuit_compile`` ->
    ``spice_import`` -> ``waveform_metrics`` is the authoring path.  Each of
    those modules already has its own AST test; this one states the rule once,
    over the whole set, so a new authoring module cannot quietly opt out.
    """

    authoring = {
        "pe_sim.circuit_compile",
        "pe_sim.circuit_devices",
        "pe_sim.spice_import",
        "pe_sim.topology",
        "pe_sim.topology_validation",
        "pe_sim.waveform_metrics",
    }
    path_by_module = {_module_name(path): path for path in PACKAGE_ROOT.rglob("*.py")}
    offenders = {}
    for name in sorted(authoring):
        imports = _all_imports(_parse(path_by_module[name]))
        hits = sorted(item for item in imports if _touches_backends(item))
        if hits:
            offenders[name] = hits

    assert offenders == {}


# ---------------------------------------------------------------------------
# The refusal vocabularies
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def code_vocabularies() -> dict[str, set[str]]:
    """Every public ``*_CODES`` set found by importing each module once."""

    found: dict[str, set[str]] = {}
    for name in MODULES:
        module = importlib.import_module(name)
        for attribute in dir(module):
            if attribute.startswith("_") or not attribute.endswith("CODES"):
                continue
            value = getattr(module, attribute)
            if not isinstance(value, frozenset):
                continue
            values = {item for item in value if isinstance(item, str)}
            if not values:
                continue
            found.setdefault(attribute, set()).update(values)
    return found


def test_every_refusal_vocabulary_is_pinned(code_vocabularies):
    assert sorted(code_vocabularies) == sorted(CODE_VOCABULARIES)
    for name, values in CODE_VOCABULARIES.items():
        assert code_vocabularies[name] == values, name


def test_every_refusal_code_is_a_snake_case_token(code_vocabularies):
    bad = sorted(
        code
        for codes in code_vocabularies.values()
        for code in codes
        if not _CODE_RE.fullmatch(code)
    )

    assert bad == [], (
        f"refusal codes that are not lower_snake_case: {bad}; a code is an "
        "identifier a client branches on, not a sentence"
    )


def test_every_warning_vocabulary_names_codes_its_issue_vocabulary_declares():
    """A warning set is a *subset* of its vocabulary, never a sibling of it."""

    from pe_sim.spice_import import IMPORT_ISSUE_CODES, WARNING_IMPORT_CODES
    from pe_sim.topology_validation import (
        TOPOLOGY_ISSUE_CODES,
        WARNING_ISSUE_CODES,
    )

    assert WARNING_ISSUE_CODES <= TOPOLOGY_ISSUE_CODES
    assert WARNING_IMPORT_CODES <= IMPORT_ISSUE_CODES


def test_no_code_is_shared_by_two_unrelated_vocabularies():
    """Overlaps between vocabularies are pinned, so a new one must be a decision.

    The vocabularies describe different layers, and a client branches on the
    value it is handed.  Some codes legitimately appear twice: a document-shape
    word such as ``schema_violation`` means the same thing to the topology
    validator and to the experiment validator, and ``unknown_plant`` is both a
    rejected request from ``describe`` and a spec issue in a document.  What
    must not happen is an overlap nobody wrote down.
    """

    names = sorted(set(CODE_VOCABULARIES) - _SUBSET_VOCABULARIES)
    overlaps: dict[tuple[str, str], set[str]] = {}
    for first_index, first in enumerate(names):
        for second in names[first_index + 1 :]:
            shared = CODE_VOCABULARIES[first] & CODE_VOCABULARIES[second]
            if shared:
                overlaps[(first, second)] = shared

    assert overlaps == KNOWN_CODE_OVERLAPS


# ---------------------------------------------------------------------------
# The schema tags
# ---------------------------------------------------------------------------


def _schema_literals() -> set[str]:
    found: set[str] = set()
    for path in sorted(PACKAGE_ROOT.rglob("*.py")):
        for node in ast.walk(_parse(path)):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if _SCHEMA_RE.fullmatch(node.value):
                    found.add(node.value)
    return found


def test_every_schema_the_source_writes_is_pinned():
    written = _schema_literals()

    assert sorted(written - SCHEMAS) == [], (
        "new schema tag(s) with no entry in SCHEMAS: "
        f"{sorted(written - SCHEMAS)}"
    )
    assert sorted(SCHEMAS - written) == [], (
        "SCHEMAS lists tag(s) the source never writes: "
        f"{sorted(SCHEMAS - written)}; a payload nobody produces is a promise "
        "nobody keeps"
    )


def test_every_schema_is_documented():
    corpus = _documentation()
    missing = sorted(schema for schema in SCHEMAS if schema not in corpus)

    assert missing == [], (
        f"schema tag(s) with no documentation: {missing}; an agent reading the "
        "docs cannot know what envelope to expect"
    )


# ---------------------------------------------------------------------------
# The command line
# ---------------------------------------------------------------------------


def test_every_subcommand_is_documented():
    corpus = _documentation()

    missing = [name for name in _cli_commands() if name not in corpus]

    assert missing == [], (
        f"subcommand(s) the parser accepts but the docs never mention: {missing}"
    )


def test_every_subcommand_can_describe_itself():
    """A command an agent cannot ask about is a command it cannot use.

    ``--help`` exercises the parser construction for each command, so a broken
    argument declaration is caught here rather than at the first real call.
    """

    from pe_sim.cli import main

    failures: dict[str, object] = {}
    for name in _cli_commands():
        buffer = io.StringIO()
        try:
            with contextlib.redirect_stdout(buffer):
                with contextlib.redirect_stderr(buffer):
                    main([name, "--help"])
        except SystemExit as exc:
            if exc.code not in (0, None):
                failures[name] = exc.code
        except BaseException as exc:  # noqa: BLE001 - the scan reports anything
            failures[name] = f"{type(exc).__name__}: {exc}"
        else:
            failures[name] = "did not exit"
        if len(buffer.getvalue().strip()) == 0:
            failures.setdefault(name, "printed nothing")

    assert failures == {}


# ---------------------------------------------------------------------------
# The documentation graph
# ---------------------------------------------------------------------------

_MARKDOWN_LINK_RE = re.compile(r"\]\(([^)\s]+)\)")


def _markdown_targets(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8", errors="ignore")
    targets = []
    for target in _MARKDOWN_LINK_RE.findall(text):
        if target.startswith(("http://", "https://", "mailto:", "#")):
            continue
        target = target.split("#", 1)[0]
        if not target:
            continue
        if target.endswith(".md") or "/" in target:
            targets.append(target)
    return targets


def test_every_markdown_link_resolves():
    """A link to a file that does not exist is a dead end for the reader."""

    documents = [REPOSITORY / "README.md", *sorted(DOCS_ROOT.glob("*.md"))]
    broken: dict[str, list[str]] = {}
    for document in documents:
        for target in _markdown_targets(document):
            candidates = [
                (document.parent / target).resolve(),
                (REPOSITORY / target).resolve(),
                (DOCS_ROOT / Path(target).name).resolve(),
            ]
            if not any(candidate.exists() for candidate in candidates):
                broken.setdefault(str(document.relative_to(REPOSITORY)), []).append(
                    target
                )

    assert broken == {}, f"markdown links that resolve to nothing: {broken}"


# ---------------------------------------------------------------------------
# The public surface
# ---------------------------------------------------------------------------


def test_every_public_name_resolves_and_is_defined_in_the_package():
    import pe_sim

    undefined = sorted(name for name in pe_sim.__all__ if not hasattr(pe_sim, name))
    assert undefined == []

    duplicates = len(pe_sim.__all__) - len(set(pe_sim.__all__))
    assert duplicates == 0

    private = sorted(name for name in pe_sim.__all__ if name.startswith("_"))
    assert private == [], f"private names exported: {private}"

    # A class or function carries ``__module__``.  A constant (a schema tag, a
    # closed vocabulary) does not, so it is checked by "some module in the
    # package defines this name" instead of by attribute.
    defined_inside = {
        attribute
        for module_name in MODULES
        for attribute in dir(importlib.import_module(module_name))
    }
    outside = sorted(
        name
        for name in pe_sim.__all__
        if (
            getattr(getattr(pe_sim, name), "__module__", None) is not None
            and not getattr(pe_sim, name).__module__.startswith("pe_sim")
        )
        or name not in defined_inside
    )
    assert outside == [], (
        f"public names whose definition lives outside the package: {outside}"
    )

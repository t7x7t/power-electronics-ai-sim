"""Packaging and installation-boundary checks for end-user entry points."""

from __future__ import annotations

import json
from importlib import resources
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

from pe_sim.cli import main
from pe_sim.contracts import validate_experiment_document

REPOSITORY = Path(__file__).resolve().parents[1]


def test_runtime_validation_dependency_is_declared():
    project = tomllib.loads((REPOSITORY / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    dependencies = [str(item) for item in project.get("dependencies", [])]
    assert any(item.startswith("jsonschema") for item in dependencies), dependencies


def test_packaged_experiment_schema_matches_the_repository_contract():
    repository_copy = REPOSITORY / "schemas" / "experiment.schema.json"
    packaged_copy = REPOSITORY / "src" / "pe_sim" / "schemas" / "experiment.schema.json"
    assert packaged_copy.read_bytes() == repository_copy.read_bytes()


def test_default_validation_loads_the_packaged_schema():
    assert resources.files("pe_sim.schemas").joinpath("experiment.schema.json").is_file()
    document = json.loads((REPOSITORY / "examples" / "buck_pi_step" / "config.json").read_text(encoding="utf-8"))
    validate_experiment_document(document)


def test_reviewed_identities_are_packaged():
    assert resources.files("pe_sim").joinpath("reviewed_identities.json").is_file()


def test_demo_templates_are_packaged_and_match_the_examples():
    """A packaged user must be able to run `pe-sim demo` with no checkout."""

    for name in ("buck", "boost"):
        packaged = resources.files("pe_sim.templates").joinpath(f"{name}_pi_step.json")
        assert packaged.is_file()
        example = REPOSITORY / "examples" / f"{name}_pi_step" / "config.json"
        assert packaged.read_bytes() == example.read_bytes()


def test_environment_check_falls_back_to_the_working_checkout(tmp_path, monkeypatch):
    from pe_sim import environment

    monkeypatch.setattr(environment, "_project_root", lambda: tmp_path / "installed-package")
    monkeypatch.chdir(REPOSITORY)
    result = environment.check_recommended_environment()
    assert "pytest" in result.packages
    assert result.sources["verified"] == "requirements-tested.txt"


def test_export_workbench_is_a_first_class_cli_command(tmp_path, capsys):
    from test_workbench_export import _published_buck_run

    run_dir, _, manifest = _published_buck_run(tmp_path)
    destination = tmp_path / "derived"

    exit_code = main(["export-workbench", str(run_dir), str(destination)])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["run_id"] == "buck-demo"
    assert payload["manifest_sha256"] == manifest["manifest_sha256"]
    for name in ("topology.json", "trace.json", "outputs.json"):
        assert (destination / name).is_file()

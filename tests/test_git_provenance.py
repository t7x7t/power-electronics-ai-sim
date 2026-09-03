import json
import subprocess

from pe_sim.provenance import collect_git_provenance
from pe_sim.contracts import ExperimentSpec, Timebase
from pe_sim.runtime import FakePIController, FakePlant, Runner


def _git(path, *args):
    return subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True, text=True)


def test_git_provenance_clean_and_dirty(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "test@example.invalid")
    _git(tmp_path, "config", "user.name", "Test")
    (tmp_path / "tracked.txt").write_text("base", encoding="utf-8")
    _git(tmp_path, "add", "tracked.txt")
    _git(tmp_path, "commit", "-qm", "baseline")

    clean = collect_git_provenance(tmp_path)
    assert clean.working_tree_status == "clean"
    assert clean.status == "known"
    assert len(clean.source_commit) == 40

    (tmp_path / "tracked.txt").write_text("changed", encoding="utf-8")
    dirty = collect_git_provenance(tmp_path)
    assert dirty.working_tree_status == "dirty"
    assert dirty.status == "exploratory"
    assert dirty.limitations


def test_git_provenance_unknown_outside_repository(tmp_path):
    # A temporary directory may inherit the repository's parent boundary;
    # use a path explicitly outside the workspace to exercise unknown mode.
    result = collect_git_provenance(tmp_path / "not-a-repo")
    assert result.working_tree_status == "unknown"
    assert result.status == "unknown"
    assert result.source_commit == "unavailable"


def test_runner_records_git_provenance(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "test@example.invalid")
    _git(tmp_path, "config", "user.name", "Test")
    (tmp_path / "baseline.txt").write_text("base", encoding="utf-8")
    _git(tmp_path, "add", "baseline.txt")
    _git(tmp_path, "commit", "-qm", "baseline")
    spec = ExperimentSpec("x", "git-run", "p", "c", Timebase(duration_s=0.001), output_dir=str(tmp_path / "runs"))
    result = Runner().run(spec, FakePlant(), FakePIController())
    manifest = json.loads((result.run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["working_tree_status"] == "clean"
    assert manifest["provenance"]["status"] == "known"
    assert manifest["source_commit"] == manifest["environment"]["git"]["source_commit"]

import json
import subprocess

import pytest

from pe_sim.dirty import FormalComparisonError, analyze_git_worktree, require_formal_comparison
from pe_sim.contracts import ExperimentSpec, Timebase
from pe_sim.runtime import FakePIController, FakePlant, Runner


def _git(path, *args):
    return subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True, text=True)


def _repo(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "test@example.invalid")
    _git(tmp_path, "config", "user.name", "Test")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "model.py").write_text("x=1", encoding="utf-8")
    (tmp_path / "baseline.json").write_text("{}", encoding="utf-8")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "baseline")


def test_dirty_analysis_categorizes_paths_and_suggests_cleanup(tmp_path):
    _repo(tmp_path)
    (tmp_path / "src" / "model.py").write_text("x=2", encoding="utf-8")
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text("", encoding="utf-8")
    (tmp_path / "runs").mkdir()
    (tmp_path / "runs" / "result.json").write_text("{}", encoding="utf-8")
    analysis = analyze_git_worktree(tmp_path)
    assert analysis.status == "dirty"
    assert "src/model.py" in analysis.paths_by_category["source"]
    assert "config.json" in analysis.paths_by_category["config"]
    assert "tests/test_x.py" in analysis.paths_by_category["tests"]
    assert "runs/result.json" in analysis.paths_by_category["generated"]
    assert analysis.suggestions
    with pytest.raises(FormalComparisonError):
        require_formal_comparison(analysis)


def test_formal_runner_rejects_dirty_before_creating_run(tmp_path, monkeypatch):
    _repo(tmp_path)
    (tmp_path / "src" / "model.py").write_text("x=2", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    spec = ExperimentSpec("x", "formal-rejected", "p", "c", Timebase(duration_s=0.001), output_dir="runs")
    with pytest.raises(FormalComparisonError):
        Runner().run(spec, FakePlant(), FakePIController(), mode="formal_comparison")
    assert not (tmp_path / "runs" / "formal-rejected").exists()


def test_exploratory_runner_records_worktree_analysis(tmp_path, monkeypatch):
    _repo(tmp_path)
    (tmp_path / "notes.md").write_text("trial", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    spec = ExperimentSpec("x", "explore", "p", "c", Timebase(duration_s=0.001), output_dir="runs")
    result = Runner().run(spec, FakePlant(), FakePIController(), mode="exploratory")
    manifest = json.loads((result.run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["run_mode"] == "exploratory"
    assert manifest["worktree_analysis"]["status"] == "dirty"

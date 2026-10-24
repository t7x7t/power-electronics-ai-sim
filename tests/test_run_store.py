"""Discovery-root resolution for run packages (Phase C, decision D3)."""

from __future__ import annotations

from pathlib import Path

from pe_sim.run_store import DEFAULT_RUNS_DIRNAME, RUNS_ROOT_ENV, discovery_root


def test_the_contract_constants_are_stable():
    assert RUNS_ROOT_ENV == "PE_SIM_RUNS_ROOT"
    assert DEFAULT_RUNS_DIRNAME == "runs"


def test_an_explicit_path_wins_over_the_environment(monkeypatch, tmp_path):
    monkeypatch.setenv(RUNS_ROOT_ENV, str(tmp_path / "from-env"))
    explicit = tmp_path / "explicit"  # deliberately does not exist

    assert discovery_root(explicit) == explicit
    assert discovery_root(str(explicit)) == explicit


def test_the_environment_is_used_when_no_explicit_path_is_given(monkeypatch, tmp_path):
    configured = tmp_path / "store"
    monkeypatch.setenv(RUNS_ROOT_ENV, str(configured))

    assert discovery_root() == configured


def test_surrounding_whitespace_in_the_environment_is_stripped(monkeypatch, tmp_path):
    configured = tmp_path / "store"
    monkeypatch.setenv(RUNS_ROOT_ENV, f"  {configured}  ")

    assert discovery_root() == configured


def test_a_blank_explicit_value_is_treated_as_not_provided(monkeypatch):
    monkeypatch.setenv(RUNS_ROOT_ENV, "")

    assert discovery_root("") == Path(DEFAULT_RUNS_DIRNAME)
    assert discovery_root("   ") == Path(DEFAULT_RUNS_DIRNAME)


def test_a_blank_environment_value_falls_back_to_the_default(monkeypatch):
    monkeypatch.setenv(RUNS_ROOT_ENV, "   ")

    assert discovery_root() == Path(DEFAULT_RUNS_DIRNAME)


def test_the_default_is_the_relative_runs_directory(monkeypatch):
    monkeypatch.delenv(RUNS_ROOT_ENV, raising=False)

    resolved = discovery_root()

    assert resolved == Path("runs")
    assert not resolved.is_absolute()


def test_resolution_never_creates_a_directory(monkeypatch, tmp_path):
    target = tmp_path / "store"
    monkeypatch.setenv(RUNS_ROOT_ENV, str(target))
    before = sorted(tmp_path.iterdir())

    discovery_root()
    discovery_root(tmp_path / "explicit")

    assert sorted(tmp_path.iterdir()) == before
    assert not target.exists()

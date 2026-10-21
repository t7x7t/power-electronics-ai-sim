"""Stage 10 restricted post-processing admission for the ngspice family.

The test publishes a real Runner package with an injected deterministic solver
and a process-local clean-provenance override.  It does not require ngspice.
"""

from __future__ import annotations

import json
import re

from pe_sim.backends.ngspice_plants import NgspiceBuckPlant
from pe_sim.backends.process import NgspiceRunResult
from pe_sim.backends.wrdata import TraceTable
from pe_sim.contracts import ExperimentSpec, Timebase
from pe_sim.dirty import DirtyAnalysis
from pe_sim.evidence import classify_stage10
from pe_sim.postprocess import summarize_run
from pe_sim.provenance import GitProvenance
from pe_sim.runtime import FakePIController, Runner
import pe_sim.runtime as runtime_module


_TRAN_RE = re.compile(r"^\.tran\s+(\S+)\s+(\S+)", re.MULTILINE)


class FakeSolver:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, netlist: str) -> NgspiceRunResult:
        self.calls += 1
        match = _TRAN_RE.search(netlist)
        assert match is not None
        duration = float(match.group(2))
        points = 21
        times = tuple(duration * index / (points - 1) for index in range(points))
        vout = tuple(3.0 + 2.0 * index / (points - 1) for index in range(points))
        current = tuple(1.0 + index / (points - 1) for index in range(points))
        switch = tuple(12.0 if index % 4 < 2 else 0.0 for index in range(points))
        return NgspiceRunResult(
            returncode=0,
            stdout="",
            stderr="",
            log="",
            table=TraceTable(
                times,
                {"v(vout)": vout, "i(l1)": current, "v(sw)": switch},
            ),
            netlist_sha256="a" * 64,
            output_sha256="b" * 64,
            log_sha256="c" * 64,
            duration_s=0.0,
            command=("fake-ngspice",),
        )


def _clean_provenance(monkeypatch):
    observed_git = runtime_module.collect_git_provenance()
    observed_worktree = runtime_module.analyze_git_worktree()
    clean_git = GitProvenance(
        source_commit="a" * 40,
        branch=observed_git.branch if observed_git.branch != "unavailable" else "test",
        working_tree_status="clean",
        status="known",
        limitations=(),
    )
    clean_worktree = DirtyAnalysis(
        status="clean",
        source_commit=clean_git.source_commit,
        branch=clean_git.branch,
        repo_root=observed_worktree.repo_root,
        paths=(),
        paths_by_category={
            category: ()
            for category in ("source", "config", "tests", "docs", "generated", "other")
        },
        reasons=(),
        suggestions=(),
    )
    monkeypatch.setattr(runtime_module, "collect_git_provenance", lambda: clean_git)
    monkeypatch.setattr(runtime_module, "analyze_git_worktree", lambda: clean_worktree)


def test_published_ngspice_family_package_is_admitted_to_stage10(tmp_path, monkeypatch):
    _clean_provenance(monkeypatch)
    plant = NgspiceBuckPlant(solver_callable=FakeSolver(), control_period_s=10e-6)
    spec = ExperimentSpec(
        "stage10-ngspice",
        "ngspice-postprocess",
        "ngspice-buck",
        "pi",
        Timebase(duration_s=30e-6, control_period_s=10e-6),
        output_dir=str(tmp_path),
        seed=17,
    )

    result = Runner().run(spec, plant, FakePIController(kp=0.2))

    assert result.status == "QUALIFIED"
    manifest = json.loads((result.run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["plant"]["identity"]["implementation"]["class"] == "NgspiceBuckPlant"
    summary = summarize_run(result.run_dir)
    classification = classify_stage10(summary)
    assert classification["evidence_level"] == "functional"
    assert classification["runs"][0]["run_id"] == "ngspice-postprocess"

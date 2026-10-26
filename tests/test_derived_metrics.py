"""Derived single-run metric tests (S5 route).

The numeric tests use synthetic records with known answers.  The integration
test publishes a real BuckPlant + FakePIController run under a process-local
clean-provenance override so the Stage 10 package gate is exercised without
depending on the developer's git state or on ngspice.
"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import shutil

import pytest

from pe_sim import ExperimentSpec, FakePIController, Timebase
from pe_sim.artifacts import manifest_digest, package_digest
from pe_sim.derive import (
    hann_window,
    power_factor,
    ripple_statistics,
    spectrum,
    thd,
    write_derived_report,
)
from pe_sim.dirty import DirtyAnalysis
from pe_sim.postprocess import PostprocessEligibilityError
from pe_sim.provenance import GitProvenance
from pe_sim.reference_plants import BuckPlant
from pe_sim.runtime import Runner
import pe_sim.runtime as runtime_module


def _clean_provenance(patch: pytest.MonkeyPatch) -> None:
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
    patch.setattr(runtime_module, "collect_git_provenance", lambda: clean_git)
    patch.setattr(runtime_module, "analyze_git_worktree", lambda: clean_worktree)


@pytest.fixture(scope="module")
def buck_run_dir(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("derived-metrics")
    with pytest.MonkeyPatch.context() as patch:
        _clean_provenance(patch)
        plant = BuckPlant(
            input_voltage_v=12.0,
            inductance_h=100e-6,
            capacitance_f=470e-6,
            load_resistance_ohm=10.0,
            integration_step_s=1e-6,
        )
        spec = ExperimentSpec(
            "derived-metrics",
            "buck-run",
            plant.topology,
            "pi",
            Timebase(duration_s=100e-6, control_period_s=10e-6),
            output_dir=str(root),
            seed=17,
        )
        result = Runner().run(spec, plant, FakePIController(kp=0.2))
    assert result.status == "QUALIFIED"
    return result.run_dir


def _snapshot(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_hann_spectrum_recovers_frequency_and_amplitude():
    sample_rate = 10_000.0
    times = [index / sample_rate for index in range(4_000)]  # 20 cycles of 50 Hz
    values = [math.sin(2.0 * math.pi * 50.0 * time) for time in times]

    result = spectrum(times, values)

    assert result["window"] == "hann"
    assert result["zero_padded_to"] == 4096
    assert result["resolution_hz"] == pytest.approx(sample_rate / 4096.0)
    magnitudes = result["magnitudes"]
    peak = max(range(1, len(magnitudes)), key=magnitudes.__getitem__)
    assert abs(result["frequencies_hz"][peak] - 50.0) <= result["resolution_hz"]
    assert magnitudes[peak] == pytest.approx(1.0, rel=0.02)


def test_thd_of_pure_and_third_harmonic_signals():
    sample_rate = 12_800.0
    times = [index / sample_rate for index in range(4_096)]  # 16 coherent cycles
    pure = [math.sin(2.0 * math.pi * 50.0 * time) for time in times]
    pure_result = thd(pure, sample_rate)
    assert pure_result["fundamental_hz"] == pytest.approx(50.0, abs=0.01)
    assert pure_result["thd"] < 1e-6

    mixed = [
        math.sin(2.0 * math.pi * 50.0 * time) + 0.2 * math.sin(2.0 * math.pi * 150.0 * time)
        for time in times
    ]
    mixed_result = thd(mixed, sample_rate)
    # harmonic_magnitudes[0] is the second harmonic; the 20% third harmonic is index 1.
    assert mixed_result["harmonic_magnitudes"][1] == pytest.approx(0.2, rel=0.02)
    assert mixed_result["thd"] == pytest.approx(0.2, rel=0.02)


def test_ripple_statistics_on_known_values():
    statistics = ripple_statistics([1.0, 2.0, 3.0])
    assert statistics["count"] == 3
    assert statistics["min"] == 1.0
    assert statistics["max"] == 3.0
    assert statistics["peak_to_peak"] == 2.0
    assert statistics["mean"] == 2.0
    assert statistics["rms"] == math.sqrt(14.0 / 3.0)
    assert statistics["ac_rms"] == math.sqrt(2.0 / 3.0)

    centered = ripple_statistics([0.5, -0.5])
    assert centered["mean"] == 0.0
    assert centered["rms"] == 0.5
    assert centered["ac_rms"] == 0.5


def test_power_factor_for_in_phase_and_quadrature_currents():
    sample_rate = 10_000.0
    times = [index / sample_rate for index in range(1_000)]
    voltage = [math.sin(2.0 * math.pi * 50.0 * time) for time in times]
    assert power_factor(voltage, list(voltage)) == pytest.approx(1.0, abs=1e-9)

    quadrature = [math.sin(2.0 * math.pi * 50.0 * time - math.pi / 2.0) for time in times]
    assert abs(power_factor(voltage, quadrature)) < 1e-6


def test_derived_report_links_a_real_buck_package(tmp_path, buck_run_dir):
    before = _snapshot(buck_run_dir)
    destination = tmp_path / "derived"

    result = write_derived_report(buck_run_dir, destination)

    report_path = Path(result["path"])
    assert report_path.parent == destination
    payload = report_path.read_bytes()
    assert result["sha256"] == hashlib.sha256(payload).hexdigest()
    report = json.loads(payload.decode("utf-8"))
    assert report == result["report"]

    manifest = json.loads((buck_run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert report["schema"] == "pe-sim.derived-report.v1"
    assert report["source"] == {
        "run_id": buck_run_dir.name,
        "package_digest": package_digest(buck_run_dir, manifest),
        "manifest_digest": manifest_digest(manifest),
    }
    assert report["source"]["package_digest"] == manifest["package_sha256"]
    assert report["method"]["window"] == "hann"
    assert report["method"]["zero_padded"] is True
    assert report["limits"]["boundary_samples_only"] is True
    assert report["limits"]["window_leakage"] is True
    assert report["limits"]["resolution_hz"] == report["method"]["resolution_hz"]

    samples = json.loads((buck_run_dir / "samples.json").read_text(encoding="utf-8"))
    assert {"action", "step_index", "vout"} <= set(report["metrics"])
    assert "time_s" not in report["metrics"]
    assert report["metrics"]["vout"]["ripple"]["count"] == len(samples)
    assert report["metrics"]["vout"]["spectrum"]["window"] == "hann"
    assert report["metrics"]["vout"]["spectrum"]["frequencies_hz"][0] == 0.0
    assert _snapshot(buck_run_dir) == before


def test_derived_report_rejects_destination_inside_the_run(buck_run_dir):
    before = _snapshot(buck_run_dir)
    with pytest.raises(ValueError, match="outside the immutable run package"):
        write_derived_report(buck_run_dir, buck_run_dir / "derived")
    assert not (buck_run_dir / "derived").exists()
    assert _snapshot(buck_run_dir) == before


def test_derived_report_rejects_oversized_output_without_writing(tmp_path, buck_run_dir):
    destination = tmp_path / "bounded"
    destination.mkdir()
    with pytest.raises(ValueError, match="max_bytes"):
        write_derived_report(buck_run_dir, destination, max_bytes=64)
    assert list(destination.iterdir()) == []


def test_derived_report_rejects_tampered_packages(tmp_path, buck_run_dir):
    altered_samples = tmp_path / "tampered-samples"
    shutil.copytree(buck_run_dir, altered_samples)
    samples_path = altered_samples / "samples.json"
    rows = json.loads(samples_path.read_text(encoding="utf-8"))
    rows[0]["vout"] = float(rows[0]["vout"]) + 0.5
    samples_path.write_text(json.dumps(rows), encoding="utf-8")
    with pytest.raises(PostprocessEligibilityError):
        write_derived_report(altered_samples, tmp_path / "out-samples")
    assert not (tmp_path / "out-samples").exists()

    altered_digest = tmp_path / "tampered-digest"
    shutil.copytree(buck_run_dir, altered_digest)
    manifest_path = altered_digest / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["package_sha256"] = "0" * 64
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(PostprocessEligibilityError):
        write_derived_report(altered_digest, tmp_path / "out-digest")
    assert not (tmp_path / "out-digest").exists()


def test_derived_report_requires_a_directory_destination(tmp_path, buck_run_dir):
    target = tmp_path / "report.json"
    target.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="directory"):
        write_derived_report(buck_run_dir, target)


def test_derived_report_adds_thd_and_power_factor_when_requested(tmp_path, buck_run_dir):
    samples = json.loads((buck_run_dir / "samples.json").read_text(encoding="utf-8"))
    sample_rate = 1.0 / (samples[1]["time_s"] - samples[0]["time_s"])
    fundamental = sample_rate / 16.0  # ten samples zero-pad to sixteen bins

    report = write_derived_report(
        buck_run_dir,
        tmp_path / "derived-extras",
        signals=["vout"],
        fundamental_hz=fundamental,
        power_pair=("vout", "action"),
    )["report"]

    assert set(report["metrics"]) == {"vout"}
    thd_entry = report["metrics"]["vout"]["thd"]
    assert thd_entry["fundamental_hz"] == pytest.approx(fundamental)
    assert thd_entry["harmonics"] == 5
    assert len(thd_entry["harmonic_magnitudes"]) == 4
    assert thd_entry["thd"] >= 0.0
    assert report["power_factor"]["voltage_field"] == "vout"
    assert report["power_factor"]["current_field"] == "action"
    assert isinstance(report["power_factor"]["value"], float)


def test_input_validation_rejects_malformed_series():
    with pytest.raises(ValueError):
        ripple_statistics([])
    with pytest.raises(ValueError):
        ripple_statistics([1.0, math.nan])
    with pytest.raises(ValueError):
        ripple_statistics([1.0, math.inf])
    with pytest.raises(ValueError):
        ripple_statistics([True, False])

    with pytest.raises(ValueError):
        spectrum([0.0, 1e-3], [1.0])
    with pytest.raises(ValueError):
        spectrum([0.0, 1e-3], [1.0, 2.0, 3.0])
    with pytest.raises(ValueError):
        spectrum([0.0, 0.0], [1.0, 2.0])
    with pytest.raises(ValueError):
        spectrum([0.0, 1e-3, 3e-3], [1.0, 2.0, 3.0])

    with pytest.raises(ValueError):
        thd([1.0, 2.0], 0.0)
    with pytest.raises(ValueError):
        thd([1.0, 2.0, 3.0], 1_000.0, harmonics=0)
    with pytest.raises(ValueError):
        thd([1.0, 2.0, 3.0], 1_000.0, fundamental_hz=600.0)

    with pytest.raises(ValueError):
        power_factor([1.0, 2.0], [1.0])
    with pytest.raises(ValueError):
        power_factor([0.0, 0.0], [1.0, 2.0])
    with pytest.raises(ValueError):
        power_factor([True], [1.0])

    assert hann_window(1) == [1.0]
    with pytest.raises(ValueError):
        hann_window(0)
    with pytest.raises(ValueError):
        hann_window(True)

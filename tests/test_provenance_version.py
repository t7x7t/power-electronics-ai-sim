"""The reported runner version must describe the code that actually ran.

An editable checkout can keep stale distribution metadata.  Reporting that
metadata as ``environment.runner.version`` would make every local evidence
package name the wrong runner, so the code version is authoritative and the
metadata is recorded beside it.
"""

from __future__ import annotations

import pe_sim
import pe_sim.provenance as provenance

_DISTRIBUTION = "power-electronics-ai-sim"


def _environment(monkeypatch, metadata_version: str | None):
    monkeypatch.setattr(
        provenance,
        "_metadata_version",
        lambda name: metadata_version if name == _DISTRIBUTION else None,
    )
    return provenance.collect_environment_provenance(())


def test_runner_version_uses_the_running_code_when_metadata_lags(monkeypatch):
    environment = _environment(monkeypatch, "0.5.4")

    assert environment["runner"]["version"] == pe_sim.__version__
    assert environment["runner"]["package_metadata_version"] == "0.5.4"
    assert any(
        "differs from the running code version" in item
        for item in environment["limitations"]
    )


def test_matching_metadata_reports_the_same_version_without_a_mismatch(monkeypatch):
    environment = _environment(monkeypatch, pe_sim.__version__)

    assert environment["runner"]["version"] == pe_sim.__version__
    assert environment["runner"]["package_metadata_version"] == pe_sim.__version__
    assert not any(
        "differs from the running code version" in item
        for item in environment["limitations"]
    )


def test_missing_metadata_still_reports_the_code_version(monkeypatch):
    environment = _environment(monkeypatch, None)

    assert environment["runner"]["version"] == pe_sim.__version__
    assert environment["runner"]["package_metadata_version"] is None

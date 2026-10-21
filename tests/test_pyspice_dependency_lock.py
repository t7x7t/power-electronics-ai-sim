"""Guard the PySpice dependency pin set used by the reference path."""

from __future__ import annotations

from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[1]


def _pins() -> dict[str, str]:
    result: dict[str, str] = {}
    for line in (REPOSITORY / "requirements-pyspice-tested.txt").read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        name, version = line.split("==", 1)
        result[name.lower()] = version.strip()
    return result


def test_pyspice_lock_keeps_the_verified_numpy_line_and_compatible_dependencies():
    pins = _pins()

    assert pins["pyspice"] == "1.5"
    assert pins["numpy"] == "1.26.4"
    # These releases still support NumPy 1.26.x.  Letting pip resolve current
    # releases instead pulls scipy/contourpy versions that require NumPy 2.x.
    assert pins["scipy"] == "1.13.1"
    assert pins["matplotlib"] == "3.9.4"
    assert pins["contourpy"] == "1.3.1"


def test_pyspice_lock_is_ascii_for_windows_pip():
    payload = (REPOSITORY / "requirements-pyspice-tested.txt").read_bytes()

    assert payload.decode("ascii")

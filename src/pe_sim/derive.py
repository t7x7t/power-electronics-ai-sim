"""Derived single-run metrics: ripple, spectrum, THD, and power factor.

This module is the S5 derived-metric route of the coverage matrix.  It
consumes one already published run package and projects descriptive metrics
from the recorded samples; it never runs a Plant, reads a netlist, or invokes
a solver.

Two boundaries are load-bearing and are restated in every report:

* A derived artifact is **not a new evidence level**.  It is a deterministic
  projection of one run's samples; the run package remains the evidence that
  reviewers qualify or reject.
* The samples are **control-loop boundary samples**.  They are the truth of
  the control loop (what the controller observed and applied at each
  boundary), but they do not contain intra-switching or intra-integration-step
  waveforms.  Switching ripple, device stress, and EMI conclusions are outside
  this route, and the report states that explicitly in ``limits``.

Only the standard library is used.  The FFT is a small radix-2 Cooley-Tukey
implementation so that spectral metrics stay reproducible on any interpreter
without optional binary dependencies.

Amplitude normalization used by :func:`spectrum`::

    A[k] = 2 * |X[k]| / sum(w)      for non-DC, non-Nyquist bins
    A[k] =     |X[k]| / sum(w)      for the DC and Nyquist bins

where ``X`` is the FFT of the zero-padded, mean-removed, Hann-windowed record
and ``sum(w)`` is the Hann coherent gain.  Frequency resolution is
``sample_rate_hz / zero_padded_to``.  Zero padding moves a tone off the FFT
grid (scalloping), so the largest non-DC bin is additionally refined by a
golden-section maximization of the direct DTFT around that bin; the refined
amplitude replaces that bin's coherent-gain estimate.
"""

from __future__ import annotations

import cmath
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .artifacts import _atomic_write, manifest_digest, package_digest
from .postprocess import RunEvidence, _assert_outside_run, load_run_evidence


REPORT_SCHEMA = "pe-sim.derived-report.v1"
MAX_SPECTRUM_BINS = 256

_REPORT_FILENAME = "derived-report.json"
_REPORT_THD_HARMONICS = 5
_GOLDEN_RATIO = (math.sqrt(5.0) - 1.0) / 2.0
_PEAK_REFINEMENT_ITERATIONS = 16
_UNIFORM_RTOL = 1e-9
_UNIFORM_ATOL = 1e-12
_AMPLITUDE_NORMALIZATION = (
    "A[k] = 2*|X[k]|/sum(w) for non-DC, non-Nyquist bins and |X[k]|/sum(w) "
    "for DC/Nyquist, where X is the FFT of the zero-padded, mean-removed, "
    "Hann-windowed record and sum(w) is the Hann coherent gain; the largest "
    "non-DC bin is replaced by the larger of its bin value and the "
    "golden-section-refined direct-DTFT amplitude"
)


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


def _real_number(value: Any, label: str) -> float:
    """Return ``value`` as a finite float or raise ``ValueError``.

    ``bool`` is deliberately rejected: ``True`` is not a measurement, and
    accepting it would silently turn a flag column into a numeric channel.
    """

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must contain only real numbers")
    try:
        number = float(value)
    except (OverflowError, ValueError) as exc:
        raise ValueError(f"{label} contains a value that is not a finite float") from exc
    if not math.isfinite(number):
        raise ValueError(f"{label} must contain only finite values")
    return number


def _finite_number(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(float(value))
    except (OverflowError, ValueError):
        return False


def _series(values: Iterable[float], label: str) -> list[float]:
    if isinstance(values, (str, bytes)):
        raise ValueError(f"{label} must be an iterable of real numbers")
    try:
        items = list(values)
    except TypeError as exc:
        raise ValueError(f"{label} must be an iterable of real numbers") from exc
    if not items:
        raise ValueError(f"{label} must not be empty")
    return [_real_number(item, label) for item in items]


def _positive_number(value: Any, label: str) -> float:
    number = _real_number(value, label)
    if number <= 0.0:
        raise ValueError(f"{label} must be finite and positive")
    return number


def _positive_integer(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} must be an integer")
    if value < 1:
        raise ValueError(f"{label} must be positive")
    return value


def _uniform_sample_interval(times: Sequence[float]) -> float:
    """Return the common spacing of a strictly increasing uniform record."""

    if len(times) < 2:
        raise ValueError("at least two time samples are required")
    interval = times[1] - times[0]
    if not math.isfinite(interval) or interval <= 0.0:
        raise ValueError("the sample interval must be finite and positive")
    tolerance = _UNIFORM_RTOL * abs(interval) + _UNIFORM_ATOL
    for previous, current in zip(times, times[1:]):
        step = current - previous
        if not math.isfinite(step) or abs(step - interval) > tolerance:
            raise ValueError("time samples must be uniformly spaced")
    return interval


# ---------------------------------------------------------------------------
# Window and FFT
# ---------------------------------------------------------------------------


def hann_window(n: int) -> list[float]:
    """Return the periodic (DFT-even) Hann window of length ``n``.

    ``w[i] = 0.5 - 0.5*cos(2*pi*i/n)``, so the window is periodic in ``n``
    and its transform is exactly zero at every integer DFT bin except 0 and
    +/-1.  A coherent integer-cycle sinusoid therefore has no leakage into
    the harmonic bins.  The length-one window is ``[1.0]``.
    """

    if isinstance(n, bool) or not isinstance(n, int):
        raise ValueError("window length must be an integer")
    if n < 1:
        raise ValueError("window length must be positive")
    if n == 1:
        return [1.0]
    return [0.5 - 0.5 * math.cos(2.0 * math.pi * index / n) for index in range(n)]


def _next_power_of_two(value: int) -> int:
    if value <= 1:
        return 1
    return 1 << (value - 1).bit_length()


def _fft(values: Sequence[complex]) -> list[complex]:
    """Iterative radix-2 Cooley-Tukey FFT; ``len(values)`` must be a power of two."""

    count = len(values)
    data = [complex(value) for value in values]
    if count <= 1:
        return data
    if count & (count - 1):
        raise ValueError("FFT input length must be a power of two")
    target = 0
    for index in range(1, count):
        bit = count >> 1
        while target & bit:
            target ^= bit
            bit >>= 1
        target |= bit
        if index < target:
            data[index], data[target] = data[target], data[index]
    length = 2
    while length <= count:
        root = cmath.exp(-2j * math.pi / length)
        half = length >> 1
        for start in range(0, count, length):
            phasor = 1 + 0j
            for index in range(start, start + half):
                left = data[index]
                right = data[index + half] * phasor
                data[index] = left + right
                data[index + half] = left - right
                phasor *= root
        length <<= 1
    return data


def _dtft_magnitude(windowed: Sequence[complex], sample_rate_hz: float, frequency_hz: float) -> float:
    phasor_step = cmath.exp(-2j * math.pi * frequency_hz / sample_rate_hz)
    phasor = 1 + 0j
    total = 0j
    for value in windowed:
        total += value * phasor
        phasor *= phasor_step
    return abs(total)


def _refine_peak(
    windowed: Sequence[complex],
    sample_rate_hz: float,
    left_hz: float,
    right_hz: float,
) -> tuple[float, float]:
    """Golden-section maximum of ``|DTFT|`` on one Hann main-lobe interval."""

    if not (0.0 <= left_hz < right_hz):
        return left_hz, _dtft_magnitude(windowed, sample_rate_hz, left_hz)
    low, high = left_hz, right_hz
    left = high - _GOLDEN_RATIO * (high - low)
    right = low + _GOLDEN_RATIO * (high - low)
    left_value = _dtft_magnitude(windowed, sample_rate_hz, left)
    right_value = _dtft_magnitude(windowed, sample_rate_hz, right)
    for _ in range(_PEAK_REFINEMENT_ITERATIONS):
        if left_value >= right_value:
            high, right, right_value = right, left, left_value
            left = high - _GOLDEN_RATIO * (high - low)
            left_value = _dtft_magnitude(windowed, sample_rate_hz, left)
        else:
            low, left, left_value = left, right, right_value
            right = low + _GOLDEN_RATIO * (high - low)
            right_value = _dtft_magnitude(windowed, sample_rate_hz, right)
    best_frequency = 0.5 * (low + high)
    best_magnitude = _dtft_magnitude(windowed, sample_rate_hz, best_frequency)
    for candidate in (low, high):
        value = _dtft_magnitude(windowed, sample_rate_hz, candidate)
        if value > best_magnitude:
            best_frequency, best_magnitude = candidate, value
    return best_frequency, best_magnitude


def _spectral_analysis(values: Sequence[float], sample_rate_hz: float, *, remove_dc: bool) -> dict[str, Any]:
    count = len(values)
    nfft = _next_power_of_two(count)
    window = hann_window(count)
    coherent_gain = math.fsum(window)
    if coherent_gain <= 0.0:
        raise ValueError("the Hann window has zero coherent gain for this record")
    offset = math.fsum(values) / count if remove_dc else 0.0
    windowed = [(value - offset) * weight for value, weight in zip(values, window)]
    transformed = _fft(windowed + [0j] * (nfft - count))
    half = nfft // 2
    resolution = sample_rate_hz / nfft
    magnitudes: list[float] = []
    for index in range(half + 1):
        single_sided = index == 0 or (nfft % 2 == 0 and index == half)
        factor = (1.0 if single_sided else 2.0) / coherent_gain
        magnitudes.append(abs(transformed[index]) * factor)
    peak_index = max(range(1, half + 1), key=magnitudes.__getitem__)
    peak_frequency, peak_magnitude = _refine_peak(
        windowed,
        sample_rate_hz,
        max(0.0, (peak_index - 1) * resolution),
        min(sample_rate_hz / 2.0, (peak_index + 1) * resolution),
    )
    refined_amplitude = 2.0 * peak_magnitude / coherent_gain
    magnitudes[peak_index] = max(magnitudes[peak_index], refined_amplitude)
    return {
        "sample_rate_hz": sample_rate_hz,
        "resolution_hz": resolution,
        "zero_padded_to": nfft,
        "frequencies_hz": [index * resolution for index in range(half + 1)],
        "magnitudes": magnitudes,
        "peak_index": peak_index,
        "peak_frequency_hz": peak_frequency,
        "peak_amplitude": magnitudes[peak_index],
        "coherent_gain": coherent_gain,
    }


# ---------------------------------------------------------------------------
# Public metrics
# ---------------------------------------------------------------------------


def ripple_statistics(values: Iterable[float]) -> dict[str, Any]:
    """Return count/min/max/peak-to-peak/mean/rms/ac-rms for one series."""

    data = _series(values, "values")
    count = len(data)
    mean = math.fsum(data) / count
    rms = math.sqrt(math.fsum(value * value for value in data) / count)
    ac_rms = math.sqrt(math.fsum((value - mean) ** 2 for value in data) / count)
    minimum = min(data)
    maximum = max(data)
    return {
        "count": count,
        "min": minimum,
        "max": maximum,
        "peak_to_peak": maximum - minimum,
        "mean": mean,
        "rms": rms,
        "ac_rms": ac_rms,
    }


def spectrum(times_s: Iterable[float], values: Iterable[float], *, remove_dc: bool = True) -> dict[str, Any]:
    """Return a one-sided Hann-windowed amplitude spectrum.

    The record is zero-padded to the next power of two (``zero_padded_to``),
    so ``resolution_hz = sample_rate_hz / zero_padded_to``.  Amplitudes are
    normalized as ``2*|X[k]|/sum(w)`` (``|X[k]|/sum(w)`` for DC and Nyquist),
    which recovers the peak amplitude of a real sinusoid.  Because zero
    padding puts most tones between bins, the largest non-DC bin is refined
    by maximizing the direct DTFT around that bin; the refined amplitude is
    written back into that bin and also returned as ``peak_amplitude`` with
    ``peak_frequency_hz``.

    Non-uniform sampling is rejected; only a uniform time base is supported.
    """

    times = _series(times_s, "times_s")
    data = _series(values, "values")
    if len(times) != len(data):
        raise ValueError("times_s and values must have the same length")
    interval = _uniform_sample_interval(times)
    sample_rate = 1.0 / interval
    analysis = _spectral_analysis(data, sample_rate, remove_dc=bool(remove_dc))
    return {
        "sample_rate_hz": sample_rate,
        "resolution_hz": analysis["resolution_hz"],
        "frequencies_hz": analysis["frequencies_hz"],
        "magnitudes": analysis["magnitudes"],
        "window": "hann",
        "zero_padded_to": analysis["zero_padded_to"],
        "peak_index": analysis["peak_index"],
        "peak_frequency_hz": analysis["peak_frequency_hz"],
        "peak_amplitude": analysis["peak_amplitude"],
    }


def _thd_from_analysis(
    analysis: Mapping[str, Any],
    fundamental_hz: Any,
    harmonics: int,
) -> dict[str, Any]:
    resolution = analysis["resolution_hz"]
    nyquist_index = analysis["zero_padded_to"] // 2
    magnitudes = analysis["magnitudes"]
    if fundamental_hz is None:
        fundamental_index = analysis["peak_index"]
        fundamental = analysis["peak_frequency_hz"]
        fundamental_magnitude = analysis["peak_amplitude"]
    else:
        requested = _positive_number(fundamental_hz, "fundamental_hz")
        if requested >= analysis["sample_rate_hz"] / 2.0:
            raise ValueError("fundamental_hz must lie below the Nyquist frequency")
        fundamental_index = int(round(requested / resolution))
        if fundamental_index < 1:
            raise ValueError("fundamental_hz is below one frequency bin")
        fundamental = requested
        fundamental_magnitude = (
            analysis["peak_amplitude"]
            if fundamental_index == analysis["peak_index"]
            else magnitudes[fundamental_index]
        )
    if fundamental_magnitude <= 0.0:
        raise ValueError("fundamental magnitude is zero; THD is undefined")
    harmonic_magnitudes: list[float] = []
    for order in range(2, harmonics + 1):
        bin_index = order * fundamental_index
        if bin_index > nyquist_index:
            raise ValueError(f"harmonic {order} of the fundamental lies above the Nyquist frequency")
        harmonic_magnitudes.append(magnitudes[bin_index])
    total = math.sqrt(math.fsum(value * value for value in harmonic_magnitudes))
    return {
        "fundamental_hz": fundamental,
        "fundamental_bin_hz": fundamental_index * resolution,
        "fundamental_magnitude": fundamental_magnitude,
        "harmonic_magnitudes": harmonic_magnitudes,
        "harmonics": harmonics,
        "thd": total / fundamental_magnitude,
    }


def thd(
    values: Iterable[float],
    sample_rate_hz: float,
    *,
    fundamental_hz: float | None = None,
    harmonics: int = 5,
) -> dict[str, Any]:
    """Return bin-snapped THD over harmonics 2..N.

    The signal is assumed uniformly sampled at ``sample_rate_hz``.  With
    ``fundamental_hz=None`` the largest non-DC bin is the fundamental;
    otherwise the requested frequency is snapped to the nearest bin.  Harmonic
    magnitudes are read at integer multiples of the fundamental bin, and
    ``THD = sqrt(sum(h2..hN)^2) / h1``.  Bin snapping is exact only for
    coherent records; otherwise scalloping remains a declared limitation.
    """

    sample_rate = _positive_number(sample_rate_hz, "sample_rate_hz")
    order = _positive_integer(harmonics, "harmonics")
    data = _series(values, "values")
    if len(data) < 2:
        raise ValueError("thd requires at least two samples")
    analysis = _spectral_analysis(data, sample_rate, remove_dc=True)
    return _thd_from_analysis(analysis, fundamental_hz, order)


def power_factor(voltage: Iterable[float], current: Iterable[float]) -> float:
    """Return ``mean(v*i) / (rms(v) * rms(i))`` for two equal-length series."""

    volts = _series(voltage, "voltage")
    amps = _series(current, "current")
    if len(volts) != len(amps):
        raise ValueError("voltage and current must have the same length")
    count = len(volts)
    rms_voltage = math.sqrt(math.fsum(value * value for value in volts) / count)
    rms_current = math.sqrt(math.fsum(value * value for value in amps) / count)
    if rms_voltage == 0.0 or rms_current == 0.0:
        raise ValueError("power factor is undefined when a voltage or current RMS is zero")
    average_power = math.fsum(value * ampere for value, ampere in zip(volts, amps)) / count
    return average_power / (rms_voltage * rms_current)


# ---------------------------------------------------------------------------
# Derived report
# ---------------------------------------------------------------------------


def _time_values(evidence: RunEvidence) -> list[float]:
    times: list[float] = []
    for index, row in enumerate(evidence.samples):
        value = row.get("time_s")
        if not _finite_number(value):
            raise ValueError(f"sample {index} has no finite numeric time_s")
        times.append(float(value))
    return times


def _selected_signals(evidence: RunEvidence, signals: Any) -> list[str]:
    if signals is None:
        names = [
            name
            for name in sorted({str(key) for row in evidence.samples for key in row})
            if name != "time_s" and all(_finite_number(row.get(name)) for row in evidence.samples)
        ]
        if not names:
            raise ValueError("the run package contains no numeric signal fields besides time_s")
        return names
    if isinstance(signals, (str, bytes)):
        raise ValueError("signals must be a sequence of field names, not a single string")
    try:
        requested = list(signals)
    except TypeError as exc:
        raise ValueError("signals must be a sequence of field names") from exc
    selected: list[str] = []
    for item in requested:
        if not isinstance(item, str) or not item:
            raise ValueError("signals must contain non-empty field-name strings")
        if item == "time_s":
            raise ValueError("time_s is the sampling axis, not a derived signal")
        if item in selected:
            raise ValueError(f"signal {item!r} is requested more than once")
        if not all(_finite_number(row.get(item)) for row in evidence.samples):
            raise ValueError(f"signal {item!r} is missing or not finite in every sample")
        selected.append(item)
    if not selected:
        raise ValueError("at least one signal must be selected")
    return selected


def _validate_power_pair(power_pair: Any, samples: Sequence[Mapping[str, Any]]) -> tuple[str, str]:
    if isinstance(power_pair, (str, bytes)) or not isinstance(power_pair, (list, tuple)):
        raise ValueError("power_pair must contain exactly two field names")
    if len(power_pair) != 2:
        raise ValueError("power_pair must contain exactly two field names")
    voltage_field, current_field = power_pair
    for name in (voltage_field, current_field):
        if not isinstance(name, str) or not name:
            raise ValueError("power_pair must contain non-empty field-name strings")
        if name == "time_s":
            raise ValueError("time_s cannot be used as a power-factor channel")
        if not all(_finite_number(row.get(name)) for row in samples):
            raise ValueError(f"power_pair field {name!r} is missing or not finite in every sample")
    if voltage_field == current_field:
        raise ValueError("power_pair must reference two different fields")
    return voltage_field, current_field


def _stored_spectrum(analysis: Mapping[str, Any]) -> dict[str, Any]:
    bin_count = len(analysis["frequencies_hz"])
    stored = min(MAX_SPECTRUM_BINS, bin_count)
    return {
        "window": "hann",
        "sample_rate_hz": analysis["sample_rate_hz"],
        "resolution_hz": analysis["resolution_hz"],
        "zero_padded_to": analysis["zero_padded_to"],
        "dc_removed": True,
        "bin_count": bin_count,
        "stored_bin_count": stored,
        "peak_index": analysis["peak_index"],
        "peak_frequency_hz": analysis["peak_frequency_hz"],
        "peak_amplitude": analysis["peak_amplitude"],
        "frequencies_hz": analysis["frequencies_hz"][:stored],
        "magnitudes": analysis["magnitudes"][:stored],
    }


def _power_factor_entry(evidence: RunEvidence, power_pair: Any) -> dict[str, Any]:
    voltage_field, current_field = _validate_power_pair(power_pair, evidence.samples)
    voltage = [float(row[voltage_field]) for row in evidence.samples]
    current = [float(row[current_field]) for row in evidence.samples]
    return {
        "voltage_field": voltage_field,
        "current_field": current_field,
        "definition": "mean(v*i) / (rms(v) * rms(i))",
        "value": power_factor(voltage, current),
    }


def write_derived_report(
    run_dir: str | Path,
    destination: str | Path,
    *,
    signals: Sequence[str] | None = None,
    fundamental_hz: float | None = None,
    power_pair: Sequence[str] | None = None,
    max_bytes: int = 8 * 1024 * 1024,
) -> dict[str, Any]:
    """Write one JSON derived-metric report for a validated run package.

    The source package is read through :func:`pe_sim.postprocess.load_run_evidence`
    and is never modified.  ``destination`` is a directory that is created if
    needed and must lie outside the source package.  The report is serialized
    completely before any filesystem write, so an over-``max_bytes`` report
    raises ``ValueError`` and leaves no output file.

    Returns ``{"path", "sha256", "report"}``.
    """

    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
        raise ValueError("max_bytes must be a positive integer")
    if fundamental_hz is not None:
        _positive_number(fundamental_hz, "fundamental_hz")
    evidence = load_run_evidence(run_dir)
    source_dir = evidence.run_dir
    target_dir = Path(destination)
    if target_dir.exists() and not target_dir.is_dir():
        raise ValueError("destination must be a directory path, not a file")
    _assert_outside_run(source_dir, target_dir)

    sample_rate = 1.0 / _uniform_sample_interval(_time_values(evidence))
    selected = _selected_signals(evidence, signals)
    metrics: dict[str, Any] = {}
    reference: dict[str, Any] | None = None
    for name in selected:
        values = [float(row[name]) for row in evidence.samples]
        analysis = _spectral_analysis(values, sample_rate, remove_dc=True)
        if reference is None:
            reference = analysis
        entry: dict[str, Any] = {
            "field": name,
            "ripple": ripple_statistics(values),
            "spectrum": _stored_spectrum(analysis),
        }
        if fundamental_hz is not None:
            entry["thd"] = _thd_from_analysis(analysis, fundamental_hz, _REPORT_THD_HARMONICS)
        metrics[name] = entry
    if reference is None:  # _selected_signals guarantees a non-empty list
        raise ValueError("at least one signal must be selected")

    resolution_hz = reference["resolution_hz"]
    method: dict[str, Any] = {
        "window": "hann",
        "window_definition": "periodic (DFT-even) Hann, w[i] = 0.5 - 0.5*cos(2*pi*i/n)",
        "zero_padded": True,
        "zero_padding": "next power of two >= recorded sample count",
        "zero_padded_to": reference["zero_padded_to"],
        "sample_rate_hz": sample_rate,
        "resolution_hz": resolution_hz,
        "dc_removed": True,
        "amplitude_normalization": _AMPLITUDE_NORMALIZATION,
        "peak_refinement": (
            "golden-section maximization of the direct DTFT over the peak bin "
            "+/- one resolution bin"
        ),
        "spectrum_bin_limit": MAX_SPECTRUM_BINS,
        "spectrum_storage": "the first min(bin_count, spectrum_bin_limit) one-sided bins are stored",
    }
    if fundamental_hz is not None:
        method["thd_harmonics"] = _REPORT_THD_HARMONICS
    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "source": {
            "run_id": evidence.manifest.get("run_id"),
            "package_digest": package_digest(source_dir, evidence.manifest),
            "manifest_digest": manifest_digest(evidence.manifest),
        },
        "method": method,
        "limits": {
            "boundary_samples_only": True,
            "window_leakage": True,
            "resolution_hz": resolution_hz,
            "sample_rate_hz": sample_rate,
            "nyquist_hz": sample_rate / 2.0,
            "recorded_samples": len(evidence.samples),
            "statements": [
                (
                    "Only control-loop boundary samples recorded in samples.json are "
                    "analysed; intra-switching and intra-integration-step waveforms are "
                    "not present in the package."
                ),
                (
                    "Hann-window leakage and bin snapping remain; harmonic amplitudes "
                    "and THD are descriptive, not calibrated measurements."
                ),
                (
                    "This report is a derived artifact and not a new evidence level; "
                    "the source run package remains the evidence to qualify."
                ),
            ],
            "evidence_level": "derived_artifact_not_a_new_evidence_level",
        },
        "metrics": metrics,
    }
    if power_pair is not None:
        report["power_factor"] = _power_factor_entry(evidence, power_pair)

    payload = (
        json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n"
    ).encode("utf-8")
    if len(payload) > max_bytes:
        raise ValueError(
            f"derived report requires {len(payload)} bytes, which exceeds max_bytes={max_bytes}"
        )
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ValueError(f"destination directory could not be created: {exc}") from exc
    report_path = target_dir / _REPORT_FILENAME
    _atomic_write(report_path, payload)
    return {
        "path": str(report_path),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "report": report,
    }


__all__ = [
    "REPORT_SCHEMA",
    "MAX_SPECTRUM_BINS",
    "hann_window",
    "ripple_statistics",
    "spectrum",
    "thd",
    "power_factor",
    "write_derived_report",
]

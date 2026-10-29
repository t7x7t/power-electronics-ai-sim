"""Performance numbers derived from one solved window's samples.

The authoring mode is "the user and an agent write the circuit; the project
supplies the substrate".  Under that split, shipping a loss model per topology
would put topologies back into the kernel.  What generalises instead is a
**measurement**: given the samples of a compiled circuit and the descriptor it
came from, report the quantities every engineer asks for - input power, output
power, the loss between them, efficiency, the observed duty, inductor current
statistics and per-signal summaries.

Three boundaries are deliberate.

**Measured, not modelled.**  Every number here comes from integrating the solved
waveforms.  Nothing is inferred from a topology name, and there is no
per-converter formula: the same code answers for a buck, an inverting
buck-boost, or a circuit this project has never seen.

**Omissions are reasons, not zeros.**  A quantity that cannot be derived is
``None`` with a ``reason`` beside it.  Reporting ``0 W`` for "no source current
was saved" would be indistinguishable from a real measurement of zero, which is
the failure mode this whole layer exists to avoid.

**The sign convention is calibrated, not assumed.**  ``i(V...)`` in ngspice is
positive when current flows *into* the source's positive terminal, so delivered
power is ``-v*i``.  ``tests/test_waveform_metrics.py`` pins that against a
solved circuit rather than trusting the comment.

The numbers are still only as good as the window they came from: a window that
has not reached periodicity measures a transient, not an operating point.  The
envelope therefore reports the window and carries ``limited`` evidence.
"""

from __future__ import annotations

import math
from typing import Any, Mapping, Sequence

from .circuit_devices import canonical_device_kind


#: Schema tag for the metrics document.
WAVEFORM_METRICS_SCHEMA = "pe-sim.waveform-metrics.v1"

#: Signals a compiled circuit saves, by kind.  The module never invents another
#: naming scheme: it reads what ``circuit_compile`` chose to save.
_VOLTAGE_PREFIX = "v("
_CURRENT_PREFIX = "i("


def _normalise(name: str) -> str:
    return name.strip().lower()


def _series(table: Any, name: str) -> Sequence[float] | None:
    signals = getattr(table, "signals", None)
    if not isinstance(signals, Mapping):
        return None
    values = signals.get(_normalise(name))
    return None if values is None else values


def _net_voltage(
    table: Any, net_id: str, reference_net: str
) -> Sequence[float] | None:
    """Voltage of ``net_id`` against the reference node.

    The reference net is not saved (it is node 0 by construction), so asking for
    it is a legitimate zero rather than a missing signal.
    """

    if net_id == reference_net:
        return None
    return _series(table, f"{_VOLTAGE_PREFIX}{net_id})")


def _bound_net(component: Any, port_id: str) -> str | None:
    for port in component.ports:
        if port.port_id == port_id:
            return port.net_id
    return None


def _parameter(component: Any, name: str) -> float | None:
    value = component.parameters.get(name)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values)


def _stats(values: Sequence[float]) -> dict[str, float]:
    total = _mean(values)
    return {
        "min": min(values),
        "max": max(values),
        "mean": total,
        "rms": math.sqrt(sum(value * value for value in values) / len(values)),
        "peak_to_peak": max(values) - min(values),
        "last": values[-1],
    }


def _omitted(reason: str) -> dict[str, Any]:
    return {"value": None, "reason": reason}


def _voltage_across(
    table: Any, positive: str | None, negative: str | None, reference_net: str
) -> Sequence[float] | None:
    """``v(positive) - v(negative)`` as a series, or ``None`` if unsavable."""

    if positive is None or negative is None:
        return None
    upper = _net_voltage(table, positive, reference_net)
    lower = _net_voltage(table, negative, reference_net)
    if upper is None and lower is None:
        # Both terminals are the reference node, so the difference is zero for
        # every sample: that is a measurement, not a missing signal.
        return [0.0] * len(getattr(table, "time_s", ()))
    if upper is None:
        return [-value for value in lower]  # type: ignore[union-attr]
    if lower is None:
        return list(upper)
    return [high - low for high, low in zip(upper, lower)]


def _source_power(
    component: Any,
    table: Any,
    element_names: Mapping[str, str],
    reference_net: str,
) -> dict[str, Any]:
    """Delivered power of one independent source, in watts.

    ``i(V...)`` is positive into the source's positive terminal, so the power
    *delivered* by the source is ``-v*i``.  A source that absorbs power reports
    a negative number instead of being silently clipped to zero.
    """

    element = element_names.get(component.component_id)
    if element is None:
        return {
            "component_id": component.component_id,
            "mean_voltage_v": None,
            "mean_current_a": None,
            "power_w": _omitted(
                "the element name for this component was not supplied, so its "
                "current signal cannot be looked up"
            ),
        }
    voltage = _voltage_across(
        table,
        _bound_net(component, "positive"),
        _bound_net(component, "negative"),
        reference_net,
    )
    current = _series(table, f"{_CURRENT_PREFIX}{element})")
    if voltage is None or current is None:
        missing = "voltage" if voltage is None else "current"
        return {
            "component_id": component.component_id,
            "mean_voltage_v": None if voltage is None else _mean(voltage),
            "mean_current_a": None if current is None else _mean(current),
            "power_w": _omitted(f"the source {missing} signal is not in this table"),
        }
    delivered = [-v * i for v, i in zip(voltage, current)]
    return {
        "component_id": component.component_id,
        "element": element,
        "mean_voltage_v": _mean(voltage),
        "mean_current_a": _mean(current),
        "power_w": {"value": _mean(delivered), "reason": None},
    }


def _load_power(
    component: Any, table: Any, reference_net: str
) -> dict[str, Any]:
    """Dissipated power of one resistive load, from the voltage across it."""

    resistance = _parameter(component, "resistance_ohm")
    if resistance is None or resistance <= 0.0:
        return {
            "component_id": component.component_id,
            "mean_voltage_v": None,
            "power_w": _omitted("the load declares no usable resistance_ohm"),
        }
    voltage = _voltage_across(
        table,
        _bound_net(component, "positive"),
        _bound_net(component, "negative"),
        reference_net,
    )
    if voltage is None:
        return {
            "component_id": component.component_id,
            "mean_voltage_v": None,
            "power_w": _omitted("the load terminal voltages are not in this table"),
        }
    dissipated = [value * value / resistance for value in voltage]
    return {
        "component_id": component.component_id,
        "mean_voltage_v": _mean(voltage),
        "power_w": {"value": _mean(dissipated), "reason": None},
    }


def _inductor(
    component: Any, table: Any, element_names: Mapping[str, str]
) -> dict[str, Any]:
    element = element_names.get(component.component_id)
    if element is None:
        return {
            "component_id": component.component_id,
            "current_a": _omitted("the element name for this component was not supplied"),
        }
    values = _series(table, f"{_CURRENT_PREFIX}{element})")
    if values is None:
        return {
            "component_id": component.component_id,
            "element": element,
            "current_a": _omitted("the inductor current is not in this table"),
        }
    return {
        "component_id": component.component_id,
        "element": element,
        "current_a": _stats(values),
    }


def _duty_of(
    component: Any, table: Any, reference_net: str
) -> dict[str, Any]:
    """Fraction of samples where the carrier's control net is above half its high level."""

    control_net = _bound_net(component, "positive")
    amplitude = _parameter(component, "amplitude_v")
    frequency = _parameter(component, "switching_frequency_hz")
    if control_net is None:
        return {
            "component_id": component.component_id,
            "observed_duty": _omitted("the carrier declares no control net"),
        }
    voltage = _net_voltage(table, control_net, reference_net)
    if voltage is None:
        return {
            "component_id": component.component_id,
            "control_net": control_net,
            "observed_duty": _omitted(
                "the control net voltage is not in this table"
            ),
        }
    threshold = 0.5 * (1.0 if amplitude is None else amplitude)
    high = sum(1 for value in voltage if value > threshold)
    return {
        "component_id": component.component_id,
        "control_net": control_net,
        "switching_frequency_hz": frequency,
        "switching_period_s": None if frequency is None else 1.0 / frequency,
        "observed_duty": {"value": high / len(voltage), "reason": None},
        "observed_duty_basis": "share of samples whose control net is above half the carrier amplitude",
    }


def _stored_energy(
    descriptor: Any,
    table: Any,
    element_names: Mapping[str, str],
    reference_net: str,
    *,
    at: str,
) -> tuple[float, list[dict[str, Any]]]:
    """Reactive energy stored at the first or last sample of the window.

    Without this term an energy balance is wrong in every window that is not
    already periodic: a capacitor charging from zero absorbs joules that would
    otherwise be reported as loss.  That is not a rounding detail - it is the
    difference between a believable number and a fabricated one.
    """

    index = 0 if at == "start" else -1
    total = 0.0
    entries: list[dict[str, Any]] = []
    for component in descriptor.components:
        canonical = canonical_device_kind(component.kind)
        if canonical == "capacitor":
            capacitance = _parameter(component, "capacitance_f")
            voltage = _voltage_across(
                table,
                _bound_net(component, "positive"),
                _bound_net(component, "negative"),
                reference_net,
            )
            if capacitance is None or voltage is None:
                entries.append(
                    {
                        "component_id": component.component_id,
                        "kind": canonical,
                        "energy_j": None,
                        "reason": "capacitance or terminal voltage is unavailable",
                    }
                )
                continue
            value = 0.5 * capacitance * voltage[index] ** 2
            total += value
            entries.append(
                {
                    "component_id": component.component_id,
                    "kind": canonical,
                    "energy_j": value,
                    "reason": None,
                }
            )
        elif canonical == "inductor":
            inductance = _parameter(component, "inductance_h")
            element = element_names.get(component.component_id)
            current = (
                None
                if element is None
                else _series(table, f"{_CURRENT_PREFIX}{element})")
            )
            if inductance is None or current is None:
                entries.append(
                    {
                        "component_id": component.component_id,
                        "kind": canonical,
                        "energy_j": None,
                        "reason": "inductance or element current is unavailable",
                    }
                )
                continue
            value = 0.5 * inductance * current[index] ** 2
            total += value
            entries.append(
                {
                    "component_id": component.component_id,
                    "kind": canonical,
                    "energy_j": value,
                    "reason": None,
                }
            )
    return total, entries


def waveform_metrics(
    descriptor: Any,
    table: Any,
    *,
    element_names: Mapping[str, str] | None = None,
    evidence_level: str = "limited",
) -> dict[str, Any]:
    """Measure one solved window; every quantity states how it was obtained.

    ``element_names`` maps ``component_id`` to the element name the compiler
    chose (``CompiledCircuit.element_names``).  Without it, per-component
    currents cannot be looked up and those entries say so instead of reporting
    zero.
    """

    names: Mapping[str, str] = {} if element_names is None else element_names
    times = tuple(float(value) for value in getattr(table, "time_s", ()))
    if len(times) < 2:
        raise ValueError("waveform metrics need at least two time points")

    presentation_nets = sorted(net.net_id for net in descriptor.nets)
    reference_nets = [net.net_id for net in descriptor.nets if net.kind == "reference"]
    reference_net = reference_nets[0] if len(reference_nets) == 1 else None

    by_kind: dict[str, list[Any]] = {}
    for component in descriptor.components:
        canonical = canonical_device_kind(component.kind)
        if canonical is None:
            continue
        by_kind.setdefault(canonical, []).append(component)

    signals: dict[str, dict[str, float]] = {}
    for name in sorted(getattr(table, "signals", {})):
        values = table.signals[name]
        if values:
            signals[name] = _stats(values)

    if reference_net is None:
        sources: list[dict[str, Any]] = []
        for component in by_kind.get("voltage_source", []):
            sources.append(
                {
                    "component_id": component.component_id,
                    "power_w": _omitted(
                        "the descriptor does not declare exactly one reference net, "
                        "so terminal voltages are undefined"
                    ),
                }
            )
        loads: list[dict[str, Any]] = []
        duties: list[dict[str, Any]] = []
        inductors: list[dict[str, Any]] = [
            _inductor(component, table, names)
            for component in by_kind.get("inductor", [])
        ]
        return {
            "schema": WAVEFORM_METRICS_SCHEMA,
            "ok": False,
            "reason": "the descriptor declares no single reference net",
            "window": _window(times),
            "signals": signals,
            "sources": sources,
            "loads": loads,
            "inductors": inductors,
            "carriers": duties,
            "power": {
                "input_w": _omitted("no single reference net"),
                "output_w": _omitted("no single reference net"),
                "storage_rate_w": _omitted("no single reference net"),
                "loss_w": _omitted("no single reference net"),
                "efficiency": _omitted("no single reference net"),
            },
            "evidence_level": evidence_level,
        }

    sources = [
        _source_power(component, table, names, reference_net)
        for component in by_kind.get("voltage_source", [])
    ]
    carriers = [
        _duty_of(component, table, reference_net)
        for component in by_kind.get("pulse_source", [])
    ]
    loads = [
        _load_power(component, table, reference_net)
        for component in by_kind.get("resistor_load", [])
    ]
    inductors = [
        _inductor(component, table, names)
        for component in by_kind.get("inductor", [])
    ]

    start_j, start_entries = _stored_energy(
        descriptor, table, names, reference_net, at="start"
    )
    end_j, end_entries = _stored_energy(
        descriptor, table, names, reference_net, at="end"
    )
    span = times[-1] - times[0]
    storage_rate = (end_j - start_j) / span
    unmatched = [
        entry["component_id"]
        for entry in start_entries
        if entry["energy_j"] is None
    ]

    input_values = [
        entry["power_w"]["value"]
        for entry in sources
        if entry["power_w"]["value"] is not None
    ]
    output_values = [
        entry["power_w"]["value"]
        for entry in loads
        if entry["power_w"]["value"] is not None
    ]

    quality: dict[str, Any] = {
        "storage_share": None,
        "window_is_operating_point": False,
        "loss_is_physical": None,
        "note": (
            "the credit for energy stored in inductors and capacitors is not "
            "counted as loss; a window that stores a large share of its input "
            "measures a transient, not an operating point.  A negative loss "
            "means the sampled model dissipated less than nothing, which is a "
            "numerical artefact and is reported rather than clipped"
        ),
    }

    if not sources:
        power: dict[str, Any] = {
            "input_w": _omitted("the descriptor declares no independent dc source"),
            "output_w": _omitted("no input power to compare against"),
            "storage_rate_w": {"value": storage_rate, "reason": None},
            "loss_w": _omitted("no input power to compare against"),
            "efficiency": _omitted("no input power to compare against"),
        }
    elif len(input_values) != len(sources):
        power = {
            "input_w": _omitted("at least one source power could not be measured"),
            "output_w": _omitted("input power is incomplete"),
            "storage_rate_w": {"value": storage_rate, "reason": None},
            "loss_w": _omitted("input power is incomplete"),
            "efficiency": _omitted("input power is incomplete"),
        }
    else:
        input_w = sum(input_values)
        power = {
            "input_w": {"value": input_w, "reason": None},
            "storage_rate_w": {"value": storage_rate, "reason": None},
        }
        if input_w > 0.0:
            quality["storage_share"] = abs(storage_rate) / input_w
            quality["window_is_operating_point"] = quality["storage_share"] <= 0.05
        if not loads:
            power["output_w"] = _omitted("the descriptor declares no resistive load")
            power["loss_w"] = _omitted("no measured output power")
            power["efficiency"] = _omitted("no measured output power")
        elif len(output_values) != len(loads):
            power["output_w"] = _omitted("at least one load power could not be measured")
            power["loss_w"] = _omitted("output power is incomplete")
            power["efficiency"] = _omitted("output power is incomplete")
        else:
            output_w = sum(output_values)
            power["output_w"] = {"value": output_w, "reason": None}
            # The balance is input = output + loss + d(stored energy)/dt.  A
            # negative loss means the model is dissipating less than nothing,
            # which is a numerical artefact; it is reported, not clipped.
            loss_w = input_w - output_w - storage_rate
            power["loss_w"] = {"value": loss_w, "reason": None}
            quality["loss_is_physical"] = loss_w >= 0.0
            if input_w <= 0.0:
                power["efficiency"] = _omitted(
                    "input power is not positive, so efficiency is undefined"
                )
            elif quality["window_is_operating_point"] and not unmatched:
                power["efficiency"] = {"value": output_w / input_w, "reason": None}
            else:
                power["efficiency"] = _omitted(
                    "the window is not an operating point: stored energy changes "
                    f"by {abs(storage_rate):.6g} W, {100.0 * (quality['storage_share'] or 0.0):.1f}% "
                    "of input power; solve a longer or periodic window"
                )

    return {
        "schema": WAVEFORM_METRICS_SCHEMA,
        "ok": True,
        "window": _window(times),
        "signals": signals,
        "sources": sources,
        "carriers": carriers,
        "loads": loads,
        "inductors": inductors,
        "storage": {
            "start_j": start_j,
            "end_j": end_j,
            "change_j": end_j - start_j,
            "rate_w": storage_rate,
            "components": start_entries,
            "unmeasured": unmatched,
        },
        "power": power,
        "quality": quality,
        "assumptions": [
            "every quantity is integrated over the samples of this one window, "
            "so a window that has not reached periodicity measures a transient",
            "loss is input power minus load power minus the rate at which "
            "inductors and capacitors store energy; it is a total, not an "
            "attribution to devices",
            "source power uses -v*i because i(V...) is positive into the "
            "source's positive terminal",
            "an efficiency is published only when the stored-energy term is at "
            "most 5% of input power, so a startup transient cannot be reported "
            "as an operating-point efficiency",
            "all nets are listed so a caller can see what was measured, but "
            "nothing here asserts that the circuit is electrically correct",
        ],
        "evidence_level": evidence_level,
        "declared_nets": presentation_nets,
    }


def _window(times: Sequence[float]) -> dict[str, Any]:
    span = times[-1] - times[0]
    return {
        "points": len(times),
        "start_time_s": times[0],
        "end_time_s": times[-1],
        "span_s": span,
        "mean_sample_step_s": None if len(times) < 2 else span / (len(times) - 1),
    }


def summarise_power(power: Mapping[str, Any]) -> str:
    """One-line human summary; omitted quantities stay visible as ``unmeasured``."""

    parts: list[str] = []

    def show(key: str, unit: str) -> str:
        entry = power.get(key) or {}
        value = entry.get("value")
        return "unmeasured" if value is None else f"{value:.6g}{unit}"

    parts.append(f"input={show('input_w', ' W')}")
    parts.append(f"output={show('output_w', ' W')}")
    parts.append(f"loss={show('loss_w', ' W')}")
    parts.append(f"efficiency={show('efficiency', '')}")
    return ", ".join(parts)


__all__ = [
    "WAVEFORM_METRICS_SCHEMA",
    "summarise_power",
    "waveform_metrics",
]

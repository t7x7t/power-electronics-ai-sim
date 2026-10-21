"""wrdata parsing tests; no ngspice installation required."""

from __future__ import annotations

import pytest

from pe_sim.backends.wrdata import TraceTable, WrdataParseError, parse_wrdata


VALID = """v(vout) i(l1) v(sw)
0.000000000000e+00 0.000000000000e+00 0.000000000000e+00 1.200000000000e+01
5.000000000000e-07 3.100000000000e+00 5.000000000000e-01 1.200000000000e+01
1.000000000000e-06 5.900000000000e+00 9.800000000000e-01 0.000000000000e+00
"""


def test_parse_header_and_values():
    table = parse_wrdata(VALID)

    assert table.point_count == 3
    assert table.start_time_s == 0.0
    assert table.end_time_s == 1e-6
    assert table.signal_names == ("v(vout)", "i(l1)", "v(sw)")
    assert table.signals["v(vout)"][1] == pytest.approx(3.1)
    assert table.signals["i(l1)"][2] == pytest.approx(0.98)


def test_parse_header_with_implicit_scale_column():
    text = """v(vout) i(l1)
0.0 0.0 0.0
1e-6 5.0 1.0
"""
    table = parse_wrdata(text)

    assert table.signal_names == ("v(vout)", "i(l1)")
    assert table.time_s == (0.0, 1e-6)


def test_parse_without_header_requires_expected_columns():
    text = "0.0 0.0 0.0\n1e-6 5.0 1.0\n"

    with pytest.raises(WrdataParseError):
        parse_wrdata(text)

    table = parse_wrdata(text, expected_columns=3)
    assert table.signal_names == ("signal_1", "signal_2")


def test_interpolation_inside_window_and_endpoint_clamp():
    table = parse_wrdata(VALID)

    midpoint = table.value_at("v(vout)", 0.5e-6)
    assert midpoint == pytest.approx(3.1)
    assert table.value_at("v(vout)", 0.75e-6) == pytest.approx((3.1 + 5.9) / 2.0)
    assert table.value_at("v(vout)", 0.0) == pytest.approx(0.0)
    assert table.value_at("v(vout)", 1e-6) == pytest.approx(5.9)


def test_interpolation_refuses_extrapolation_and_unknown_signals():
    table = parse_wrdata(VALID)

    with pytest.raises(WrdataParseError):
        table.value_at("v(vout)", 2e-6)
    with pytest.raises(WrdataParseError):
        table.value_at("v(none)", 0.5e-6)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "a b c d\n0.0 1.0 2.0\n1.0 2.0 3.0\n",
        "v(vout) i(l1)\n0.0 0.0 0.0\n1e-6 1.0\n",
        "v(vout) i(l1)\n0.0 0.0 0.0\n1e-6 nan 1.0\n",
        "v(vout) i(l1)\n1e-6 1.0 1.0\n0.0 0.0 0.0\n",
        "v(vout) i(l1)\n0.0 0.0 0.0\n",
        "v(vout) i(l1)\n0.0 0.0 0.0\n1e-6 abc 1.0\n",
    ],
)
def test_malformed_output_is_rejected(text):
    with pytest.raises(WrdataParseError):
        parse_wrdata(text)


def test_duplicate_timestamps_are_accepted_but_must_be_monotonic():
    text = """v(vout)
0.0 0.0
1e-6 1.0
1e-6 2.0
2e-6 3.0
"""
    table = parse_wrdata(text)

    assert table.point_count == 4
    assert table.value_at("v(vout)", 1e-6) == pytest.approx(2.0)


def test_trace_table_validates_lengths_and_finiteness():
    with pytest.raises(WrdataParseError):
        TraceTable((0.0,), {"v": (0.0,)})
    with pytest.raises(WrdataParseError):
        TraceTable((0.0, 1.0), {"v": (0.0,)})
    with pytest.raises(WrdataParseError):
        TraceTable((0.0, float("nan")), {"v": (0.0, 1.0)})

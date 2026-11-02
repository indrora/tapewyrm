"""cinnabar.model: span trees, byte maps, and the chunked flux store."""

from __future__ import annotations

import numpy as np

from cinnabar.model import Axis, AxisKind, ByteMap, Flux, Shape, Span, depth, level


def test_span_tree_depth_and_levels():
    leaf = Span(0, 4, "sig", style="sig", shape=Shape.HEX)
    entry = Span(0, 128, "Volume", children=(leaf, Span(4, 8, "segs")))
    table = Span(0, 256, "Volume Table", children=(entry, Span(128, 256, "end")))
    assert depth([table]) == 3
    assert [s.label for s in level([table], 1)] == ["Volume", "end"]
    assert [s.label for s in level([table], 2)] == ["sig", "segs"]


def test_span_clip_keeps_subtree_inside_window():
    inner = Span(10, 30, "in", children=(Span(12, 14, "a"), Span(28, 40, "b")))
    clipped = inner.clipped(20, 35)
    assert clipped is not None and (clipped.start, clipped.end) == (20, 30)
    assert [c.label for c in clipped.children] == ["b"]
    assert clipped.children[0].start == 28 and clipped.children[0].end == 35
    assert inner.clipped(40, 50) is None


def test_span_mapped_applies_to_children():
    s = Span(1, 3, "x", children=(Span(1, 2, "y"),))
    m = s.mapped(lambda v: v * 10)
    assert (m.start, m.end, m.children[0].end) == (10, 30, 20)


def test_bytemap_identity_and_gaps():
    ident = ByteMap.identity(1000.0, 500)
    assert ident.to_axis(0) == 1000.0 and ident.to_axis(250) == 1250.0
    # Two sector runs with a missing one between: offsets in the gap interpolate.
    bm = ByteMap([(0, 1024, 1.0, 2.0), (2048, 1024, 4.0, 5.0)])
    assert bm.to_axis(0) == 1.0
    assert bm.to_axis(1024) == 2.0
    assert abs(bm.to_axis(1536) - 3.0) < 1e-9  # half way through the gap
    assert bm.to_axis(2048) == 4.0
    assert bm.to_axis(3072) == 5.0
    assert bm.to_axis(4096) == 6.0  # past the end: extrapolated


def test_axis_formats():
    t = Axis(AxisKind.TIME)
    assert t.format(0.34) == "340ms" and t.format(2.5) == "2.500s"
    assert t.format_delta(0.0000175) == "18µs"
    assert Axis(AxisKind.BYTES).format(29696) == "29,696 B"


def test_flux_transitions_and_chunks():
    intervals = np.full(200_000, 72, dtype=np.int32)  # 1 us apart at 72 MHz
    flux = Flux(intervals, 72_000_000)
    assert abs(flux.duration - 0.2) < 1e-9
    # Interval 99,999 (0-based) ends exactly at 0.1 s, so it is the first "at or after".
    assert flux.interval_index(0.1) == 99_999
    t = flux.transitions_between(0.1, 0.1001)
    assert len(t) == 100 and abs(t[0] - 0.1) < 1e-9
    assert (
        flux.transition_count(0.0, 0.05) == 49_999
    )  # the one ending exactly at 0.05 s is not before it
    assert flux.cell_count_between(0, 1) == 0
    # A chunk: 10 traced transitions each clocking out 2 cells of 1 us.
    times = 0.1 + np.arange(1, 11) * 2e-6
    cum = np.arange(1, 11) * 2
    clock = np.full(10, 1e-6)
    bits = np.array([1, 0] * 10, dtype=np.uint8)
    flux.add_chunk(0.1, 0.2, times, cum, clock, bits)
    assert flux.cell_count_between(0.1, 0.2) == 20
    starts, values = flux.cells_between(0.1, 0.2)
    assert len(starts) == 20 and list(values[:4]) == [1, 0, 1, 0]
    # cell 0 starts two clocks before transition 0, cell 1 one clock before.
    assert abs(starts[0] - 0.1) < 1e-9 and abs(starts[1] - 0.100001) < 1e-9
    assert flux.cells_between(0.5, 0.6)[0].size == 0


def test_tick_labels_gain_decimals_as_the_step_shrinks():
    t = Axis(AxisKind.TIME)
    assert t.format_tick(2.68, 0.1) == "2.680s"
    assert t.format_tick(2.6792, 0.0002) == "2.6792s"
    assert t.format_tick(2.67921, 0.00001) == "2.67921s"
    assert Axis(AxisKind.BYTES).format_tick(59_392.0, 200) == "59,392 B"


def test_zoom_ceiling_and_stream_intervals():
    from cinnabar.model import ZOOM_MAX_BYTES

    assert Axis(AxisKind.BYTES).min_window == ZOOM_MAX_BYTES
    assert abs(Axis(AxisKind.TIME).min_window - ZOOM_MAX_BYTES * 16e-6) < 1e-12
    intervals = np.array([144, 216, 288] * 1000, dtype=np.int32)  # 2, 3, 4 us at 72 MHz
    flux = Flux(intervals, 72_000_000)
    times, gaps = flux.intervals_between(0.0, flux.duration + 1e-6)  # the window end is exclusive
    assert len(times) == 3000 and abs(gaps[0] - 2e-6) < 1e-12 and abs(gaps[2] - 4e-6) < 1e-12
    assert abs(times[0] - 2e-6) < 1e-12
    assert 4e-6 < flux.interval_ceiling() < 5e-6

"""cinnabar.ui.details: the hexdump, byte lookup under a span, and export."""

from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QColor  # noqa: E402

from cinnabar.model import ByteRun, Segment, Shape, Span  # noqa: E402
from cinnabar.ui.details import hexdump_html, write_pieces  # noqa: E402


def _segment() -> Segment:
    # Two sector runs of 32 bytes each at offsets 0 and 2048 (a hole between), on a byte axis.
    runs = [
        ByteRun(1000.0, 1032.0, bytes(range(32)), 0),
        ByteRun(3048.0, 3080.0, b"VTBL" + bytes(28), 2048),
    ]
    sig = Span(3048.0, 3052.0, "sig", style="sig", shape=Shape.HEX)
    entry = Span(3048.0, 3080.0, "Volume", style="vtbl-entry", children=(sig,))
    return Segment(0, 1000.0, 4000.0, "Segment 0", [], [entry], runs)


def test_bytes_between_follows_runs_and_holes():
    seg = _segment()
    pieces = seg.bytes_between(1008.0, 1012.0)
    assert len(pieces) == 1 and pieces[0].offset == 8 and pieces[0].data == bytes(range(8, 12))
    both = seg.bytes_between(seg.start, seg.end)
    assert [p.offset for p in both] == [0, 2048]
    assert both[1].data[:4] == b"VTBL"
    assert seg.bytes_between(1500.0, 2000.0) == []


def test_span_short_and_deepest():
    seg = _segment()
    entry = seg.fields[0]
    assert entry.short == "Volume"
    assert Span(0, 1, "Format Parameter Record").short == "Format"
    assert entry.deepest_at(3049.0).label == "sig"
    assert entry.deepest_at(3070.0) is entry


def test_hexdump_colours_bytes_by_deepest_span():
    seg = _segment()
    entry = seg.fields[0]
    red, blue = QColor("#ff0000"), QColor("#0000ff")

    def colour_at(x: float):
        return red if entry.deepest_at(x).style == "sig" else blue

    html = hexdump_html(seg.bytes_between(seg.start, seg.end), colour_at)
    assert "000000&nbsp;" in html and "000800&nbsp;" in html  # offsets 0 and 2048
    assert "2,016 bytes not read" in html  # the hole between the runs
    assert html.count("background:#ff0000") == 4  # V T B L
    assert "|VTBL" in html
    assert hexdump_html([], colour_at).count("no bytes") == 1


def test_write_pieces_keeps_offsets(tmp_path: Path):
    seg = _segment()
    out = tmp_path / "seg.bin"
    n = write_pieces(out, seg.bytes_between(seg.start, seg.end))
    data = out.read_bytes()
    assert n == len(data) == 2048 + 32
    assert data[:32] == bytes(range(32)) and data[2048:2052] == b"VTBL"
    assert data[32:2048] == bytes(2016)
    assert write_pieces(tmp_path / "empty.bin", []) == 0

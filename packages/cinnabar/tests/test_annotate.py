"""cinnabar.annotate: header, volume table, volume data and compression spans."""

from __future__ import annotations

import struct

from qiclib import volume as volume_mod
from qiclib.testing.builders import build_format_parameter_record, build_vtbl_entry
from tapewyrm_archive.twti import SegmentEntry, SegmentState

from cinnabar.annotate import header as header_mod
from cinnabar.annotate import sectors as sectors_mod
from cinnabar.annotate import volume as volume_ann
from cinnabar.model import depth, level
from tests.conftest import basic_volume_stream


def test_header_spans_cover_fpr_fields_and_bsm():
    fpr = build_format_parameter_record(tape_name="SYNTH")
    data = fpr.ljust(29_696, b"\x00")
    spans = header_mod.header_spans(data)
    assert [s.label for s in spans] == ["Format Parameter Record", "Bad Sector Map"]
    fpr_span = spans[0]
    assert fpr_span.sublabel == "SYNTH"
    labels = {c.label: c for c in fpr_span.children}
    assert labels["tape name"].start == 30 and labels["tape name"].end == 74
    assert labels["55 AA 55 AA"].sublabel == "55 aa 55 aa"
    assert dict(labels["seg/trk"].detail)["value"] == "207"
    bsm = spans[1]
    assert bsm.start == (volume_mod.BSM_OFFSET if data[4] == 4 else volume_mod.FIXED_BSM_OFFSET)


def test_vtbl_spans_show_entries_and_the_terminator():
    e1 = build_vtbl_entry(start_seg=3, end_seg=10, description="FIRST", flags=0x20)
    e2 = build_vtbl_entry(start_seg=11, end_seg=12, description="SECOND", compressed=True)
    data = (e1 + e2).ljust(1024, b"\x00")
    entries = volume_mod.parse_volume_table_data(data)
    (table,) = volume_ann.vtbl_spans(data, entries)
    assert table.sublabel == "2 entries"
    kids = table.children
    assert [k.label for k in kids] == ["Volume", "Volume", "end"]
    assert kids[0].sublabel == "FIRST" and kids[1].sublabel == "SECOND"
    assert dict(kids[0].detail)["flags"].endswith("dir-last")
    assert dict(kids[1].detail)["compressed"] == "True"
    fields = {f.label: f for f in kids[0].children}
    assert (fields["sig"].start, fields["sig"].end) == (0, 4)
    assert fields["description"].sublabel == "FIRST"
    assert depth([table]) == 3


def test_basic_volume_spans_directory_first():
    stream = basic_volume_stream()
    entry = volume_mod.parse_vtbl_base(build_vtbl_entry(start_seg=2, end_seg=2))
    spans = volume_ann.volume_spans(stream, entry)
    assert [s.label for s in spans] == ["Directory Section", "Data Section"]
    directory, data = spans
    assert [c.label for c in directory.children] == ["DOCS", "HELLO.TXT", "WORLD.TXT", "INNER.TXT"]
    assert directory.children[0].style == "dir-subdir"
    assert [c.label for c in data.children] == ["HELLO.TXT", "WORLD.TXT"]
    hello = data.children[0]
    assert [g.label for g in hello.children] == ["33CC33CC", "HELLO.TXT", "path", "data"]
    sig, _entry, _path, filedata = hello.children
    assert stream[int(sig.start) : int(sig.end)] == b"\xcc\x33\xcc\x33"
    assert stream[int(filedata.start) : int(filedata.end)] == b"hello tape\n"
    assert data.end == len(stream)
    assert list(level(spans, 2))  # three levels deep


def test_compressed_volume_has_no_stream_spans_but_extents_per_segment():
    entry = volume_mod.parse_vtbl_base(build_vtbl_entry(start_seg=2, end_seg=2, compressed=True))
    assert volume_ann.volume_spans(b"\x00" * 100, entry) == []
    # An extent: 8-byte offset, a 100-byte compressed frame, a 50-byte raw frame, null fill.
    seg = bytearray(29_696)
    struct.pack_into("<Q", seg, 0, 123_456)
    struct.pack_into("<H", seg, 8, 100)
    struct.pack_into("<H", seg, 8 + 2 + 100, 0x8000 | 50)
    (extent,) = volume_ann.extent_spans(bytes(seg), spanning=False)
    assert extent.sublabel.startswith("2 frames @ 123,456")
    kinds = [(c.label, c.style) for c in extent.children]
    assert kinds[0] == ("offset", "extent-offset")
    assert kinds[1] == ("frame", "frame") and kinds[2] == ("raw", "frame-raw")
    assert kinds[-1] == ("fill", "fill")
    # Segment-spanning: the first two bytes say where the next extent starts.
    seg2 = bytearray(29_696)
    struct.pack_into("<H", seg2, 0, 0)
    spans = volume_ann.extent_spans(bytes(seg2), spanning=True)
    assert [s.label for s in spans] == ["next", "frame (cont.)"]


def test_slot_spans_follow_the_segment_table():
    entry = SegmentEntry(SegmentState.CORRECTED, erasures=2, data_len=29_696, excluded_mask=0b100)
    slots = sectors_mod.slot_spans(entry, base=29_696)
    assert len(slots) == 29  # the image keeps no parity sectors
    assert slots[0].start == 29_696 and slots[1].start == 29_696 + 1024
    assert slots[2].style == "slot-excluded" and slots[3].style == "slot-corrected"
    missing = sectors_mod.slot_spans(SegmentEntry(SegmentState.MISSING))
    assert {s.style for s in missing} == {"slot-missing"}

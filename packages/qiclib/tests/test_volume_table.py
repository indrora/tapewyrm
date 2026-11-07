"""Volume-table parsing tests: VTBL records and volume_streams (DESIGN.md §7.3, §7.5)."""

from __future__ import annotations

from tapewyrm_archive.qic80date import decode_short_date

from qiclib.testing.builders import (
    build_volume_table_segment,
    build_vtbl_entry,
    make_segment_from_sectors,
    make_short_date,
)
from qiclib.volume.table import SIG_VTBL, parse_volume_table, volume_streams


def test_short_date_round_trip():
    packed = make_short_date(1995, 6, 15, 13, 30, 45)  # mo/dy 0-based here
    decoded = decode_short_date(packed)
    assert decoded == (1995, 6, 15, 13, 30, 45)


def test_short_date_undefined():
    assert decode_short_date(0) is None
    assert decode_short_date(0xFFFFFFFF) is None


def test_parse_volume_table_entries():
    entries_bytes = [
        build_vtbl_entry(start_seg=2, end_seg=5, description="C:"),
        build_vtbl_entry(start_seg=6, end_seg=9, description="D:"),
    ]
    seg = build_volume_table_segment(seg_abs=1, vtbl_entries=entries_bytes)
    entries = parse_volume_table(seg)
    assert len(entries) == 2
    assert entries[0].signature == SIG_VTBL
    assert (entries[0].start_seg, entries[0].end_seg) == (2, 5)
    assert entries[0].description == "C:"
    assert entries[1].description == "D:"


def test_volume_streams_concatenates_data_sectors():
    # Volume table at segment 1, file set spans segments 2..3.
    vtbl = build_vtbl_entry(start_seg=2, end_seg=3, description="C:")
    vt_seg = build_volume_table_segment(seg_abs=1, vtbl_entries=[vtbl])

    # Two data segments, each with a recognizable first byte.
    seg2 = make_segment_from_sectors(0, 2, 2, [b"\xa0" + b"\x00" * 1023])
    seg3 = make_segment_from_sectors(0, 3, 3, [b"\xb0" + b"\x00" * 1023])

    segs = {(0, 1): vt_seg, (0, 2): seg2, (0, 3): seg3}

    from qiclib.header import BadSectorMap, VolumeInfo

    vol = VolumeInfo(
        format_code=4,
        segments_per_track=207,
        tracks=28,
        max_fsd=5,
        max_ftk=254,
        max_fsc=128,
        tape_name="X",
        format_date=0,
    )
    bsm = BadSectorMap()
    streams = volume_streams(segs, vol, bsm)
    assert len(streams) == 1
    entry, data = streams[0]
    assert entry.description == "C:"
    # 2 segments * 29 data sectors * 1024 bytes.
    assert len(data) == 2 * 29 * 1024
    assert data[0] == 0xA0
    assert data[29 * 1024] == 0xB0

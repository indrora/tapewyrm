"""Header segment tests: format parameter record and bad-sector map (DESIGN.md §7.3)."""

from __future__ import annotations

from qiclib.header import parse_header
from qiclib.testing.builders import build_header_segment


def test_parse_format_parameter_record():
    seg = build_header_segment(
        segments_per_track=207,
        tracks=28,
        max_fsd=5,
        tape_name="MYBACKUP",
    )
    vol, bsm = parse_header(seg)
    assert vol.valid_signature is True
    assert vol.format_code == 0x04
    assert vol.segments_per_track == 207
    assert vol.tracks == 28
    assert vol.max_fsd == 5
    assert vol.max_ftk == 254
    assert vol.max_fsc == 128
    assert vol.tape_name == "MYBACKUP"


def test_parse_bsm_sectors_and_segments():
    seg = build_header_segment(
        bad_lsns=[100, 5000],
        bad_segments=[3, 10],
    )
    _vol, bsm = parse_header(seg)
    assert bsm.is_sector_bad(100)
    assert bsm.is_sector_bad(5000)
    assert bsm.is_segment_bad(3)
    assert bsm.is_segment_bad(10)
    assert not bsm.is_sector_bad(101)
    assert not bsm.is_segment_bad(4)

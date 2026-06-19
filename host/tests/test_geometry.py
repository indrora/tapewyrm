"""Coordinate-algebra tests (DESIGN.md §7.3 worked values)."""

from tapewyrm.tape.geometry import (
    Geometry,
    coord_to_lsn,
    coord_to_seg,
    sector_in_segment,
    seg_to_coord,
)
from tapewyrm.types import Direction, TapeFormat


def test_anchor_origin():
    # (FSD,FTK,FSC) = (0,0,1) => tape track 0, segment 0, sector 0.
    assert coord_to_lsn(0, 0, 1) == 0
    assert coord_to_seg(0, 0, 1) == 0
    assert sector_in_segment(1) == 0


def test_one_floppy_track_is_four_segments():
    # 128 sectors = 1 floppy track (FTK) = 4 segments.
    assert coord_to_seg(0, 1, 1) == 4
    assert coord_to_lsn(0, 1, 1) == 128


def test_one_floppy_side_is_1020_segments():
    assert coord_to_seg(1, 0, 1) == 1020
    assert coord_to_lsn(1, 0, 1) == 32640


def test_segment_inverse_round_trip():
    for seg in (0, 1, 3, 4, 5, 1019, 1020, 5795):
        fsd, ftk, fsc0 = seg_to_coord(seg)
        assert coord_to_seg(fsd, ftk, fsc0) == seg


def test_sector_in_segment_spans_32():
    assert sector_in_segment(1) == 0
    assert sector_in_segment(32) == 31
    assert sector_in_segment(33) == 0
    assert coord_to_seg(0, 0, 33) == 1


def test_geometry_425ft_example():
    # DESIGN.md §2.2: 207 segs/track, 28 tracks -> 5796 segments, 185472 sectors.
    g = Geometry(tracks=28, segments_per_track=207)
    assert g.total_segments() == 5796
    assert g.total_segments() * g.sectors_per_segment == 185472


def test_geometry_place_and_direction():
    g = Geometry(tracks=28, segments_per_track=207)
    seg, tpt, tps, sec = g.place(0, 0, 1)
    assert (seg, tpt, tps, sec) == (0, 0, 0, 0)
    seg, tpt, tps, _ = g.place(0, 0, 33 + 207 * 4 - 4)  # into track 1 region
    assert tpt == g.seg_to_tpt_tps(seg)[0]
    assert g.direction(0) is Direction.FORWARD
    assert g.direction(1) is Direction.REVERSE


def test_geometry_for_format_fallback_spt():
    g = Geometry.for_format(TapeFormat.QIC80, calibrated_length=None)
    assert g.segments_per_track == 207
    g2 = Geometry.for_format(TapeFormat.QIC80, calibrated_length=100)
    assert g2.segments_per_track == 100

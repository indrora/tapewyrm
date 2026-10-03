"""Coordinate-algebra tests (DESIGN.md §7.3 worked values)."""

import pytest
from tapewyrm_archive.types import Direction, TapeFormat

from qiclib import cartridge
from qiclib.geometry import (
    Geometry,
    coord_to_lsn,
    coord_to_seg,
    fallback_spt,
    sector_in_segment,
    seg_to_coord,
    track_count,
)


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


# ---------------------------------------------------------------------------
# Track counts per (format, width) and the spt fallbacks
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("fmt", "wide", "tracks"),
    [
        (TapeFormat.QIC40, False, 20),  # QIC-40-MC Rev M cover
        (TapeFormat.QIC80, False, 28),  # QIC-80-MC Rev N cover
        (TapeFormat.QIC80, True, 36),
        (TapeFormat.QIC3010, False, 40),  # QIC-3010-MC Rev H cover
        (TapeFormat.QIC3010, True, 50),
        (TapeFormat.QIC3020, False, 40),  # QIC-3020-MC Rev H cover
        (TapeFormat.QIC3020, True, 50),
        (TapeFormat.UNKNOWN, False, 28),  # assumed QIC-80 (QIC-117 Rev J Note 4)
        (TapeFormat.UNKNOWN, True, 36),
        (TapeFormat.QIC40, True, 20),  # QIC-40 has no wide tape: narrow count
    ],
)
def test_track_count_per_format_and_width(fmt, wide, tracks):
    assert track_count(fmt, wide) == tracks
    assert Geometry.for_format(fmt, segments_per_track=100, wide=wide).tracks == tracks


def test_track_count_agrees_with_every_cartridge_profile():
    """The geometry table and the cartridge catalogue are the same numbers."""
    by_standard = {
        "QIC-40": TapeFormat.QIC40,
        "QIC-80": TapeFormat.QIC80,
        "QIC-3010": TapeFormat.QIC3010,
        "QIC-3020": TapeFormat.QIC3020,
    }
    for cart in cartridge.catalogue():
        wide = cart.width_in > 0.25
        assert track_count(by_standard[cart.standard], wide) == cart.tracks, cart.profile


@pytest.mark.parametrize(
    ("calibrated", "spt"),
    [(1, 100), (153, 100), (154, 207), (228, 207), (229, 229), (300, 300)],
)
def test_fallback_spt_qic117_override_table(calibrated, spt):
    """QIC-117 Rev J cmd 36: 1-153 -> 100, 154-228 -> 207, 229+ not overridden."""
    assert fallback_spt(calibrated, TapeFormat.QIC80) == spt


def test_fallback_spt_override_is_qic80_only():
    """The 100/207 override is for QIC-80 fixed 550 Oe tapes; 3010/3020 keep theirs."""
    assert fallback_spt(160, TapeFormat.QIC3020) == 160
    assert fallback_spt(160, TapeFormat.QIC3010, wide=True) == 160


def test_fallback_spt_without_calibration_follows_the_standard():
    """No report at all: QIC-80 keeps 207; others use their longest catalogued tape."""
    assert fallback_spt(None, TapeFormat.QIC80) == 207
    assert fallback_spt(None, TapeFormat.QIC40) == 365  # 1,100 ft, fixed by QIC-40
    assert fallback_spt(None, TapeFormat.QIC3020) == cartridge.min_segments_per_track(
        1000, "QIC-3020"
    )
    assert fallback_spt(None, TapeFormat.QIC3010, wide=True) == (
        cartridge.min_segments_per_track(1000, "QIC-3010")
    )
    g = Geometry.for_format(TapeFormat.QIC3020, wide=True)
    assert g.segments_per_track == cartridge.min_segments_per_track(750, "QIC-3020")

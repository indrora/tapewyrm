"""Sector placement tests: binning + capture-order independence (DESIGN.md §2.3)."""

from __future__ import annotations

import random

from tapewyrm.codec.place import place
from tapewyrm.tape.geometry import Geometry
from tapewyrm.types import RawSector


def _sector(fsd, ftk, fsc, *, crc_ok=True, deleted=False, fill=0xAA) -> RawSector:
    return RawSector(
        fsd=fsd,
        ftk=ftk,
        fsc=fsc,
        data=bytes([fill]) * 1024,
        id_crc_ok=True,
        data_crc_ok=crc_ok,
        deleted=deleted,
    )


def test_basic_binning():
    geom = Geometry(tracks=28, segments_per_track=207)
    # 32 sectors fill segment 0 (track 0, tps 0), fsc 1..32.
    sectors = [_sector(0, 0, fsc) for fsc in range(1, 33)]
    segs = place(sectors, geom)
    assert (0, 0) in segs
    seg = segs[(0, 0)]
    assert seg.seg == 0
    assert all(seg.sectors[i] is not None for i in range(32))
    # slot index == fsc - 1
    for fsc in range(1, 33):
        assert seg.sectors[fsc - 1].fsc == fsc


def test_second_segment_placement():
    geom = Geometry(tracks=28, segments_per_track=207)
    # fsc 33 -> sector_in_segment 0 of segment 1 (still track 0).
    sectors = [_sector(0, 0, 33)]
    segs = place(sectors, geom)
    assert (0, 1) in segs
    assert segs[(0, 1)].sectors[0].fsc == 33


def test_capture_order_independence():
    geom = Geometry(tracks=28, segments_per_track=207)
    sectors = [_sector(0, ftk, fsc) for ftk in range(2) for fsc in range(1, 33)]

    ordered = place(list(sectors), geom)
    shuffled = sectors[:]
    random.Random(1234).shuffle(shuffled)
    out_shuffled = place(shuffled, geom)

    assert set(ordered.keys()) == set(out_shuffled.keys())
    for key in ordered:
        a = ordered[key]
        b = out_shuffled[key]
        a_ids = [(s.fsd, s.ftk, s.fsc) if s else None for s in a.sectors]
        b_ids = [(s.fsd, s.ftk, s.fsc) if s else None for s in b.sectors]
        assert a_ids == b_ids


def test_crc_good_wins_over_bad_regardless_of_order():
    geom = Geometry(tracks=28, segments_per_track=207)
    bad = _sector(0, 0, 1, crc_ok=False, fill=0x00)
    good = _sector(0, 0, 1, crc_ok=True, fill=0xFF)

    # bad first, then good -> good kept.
    segs = place([bad, good], geom)
    assert segs[(0, 0)].sectors[0].data_crc_ok
    assert segs[(0, 0)].sectors[0].data[0] == 0xFF

    # good first, then bad -> good kept.
    segs2 = place([good, bad], geom)
    assert segs2[(0, 0)].sectors[0].data_crc_ok
    assert segs2[(0, 0)].sectors[0].data[0] == 0xFF


def test_deleted_sector_kept():
    geom = Geometry(tracks=28, segments_per_track=207)
    sectors = [_sector(0, 0, 1, deleted=True)]
    segs = place(sectors, geom)
    assert segs[(0, 0)].sectors[0].deleted is True

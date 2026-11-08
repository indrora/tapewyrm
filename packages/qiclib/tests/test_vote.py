"""qiclib.vote: rebuilding sectors no single read got right, and checking before trusting."""

from __future__ import annotations

import random
from dataclasses import replace

from qiclib import merge, rs, vote
from qiclib import segment as seg_mod
from qiclib.geometry import Geometry
from qiclib.header import BadSectorMap
from qiclib.testing.builders import make_segment_from_sectors, with_parity
from qiclib.types import RawSector, Segment

RNG = random.Random(80)


def _codeword() -> Segment:
    rows = [bytes(RNG.randrange(256) for _ in range(1024)) for _ in range(29)]
    seg = with_parity(make_segment_from_sectors(0, 0, 0, rows))
    for slot, sec in enumerate(seg.sectors):
        seg.sectors[slot] = replace(sec, data_crc=vote.data_crc(sec.data, False))
    return seg


def _bad_read(sec: RawSector, lo: int, hi: int) -> RawSector:
    """A read of ``sec`` with bytes lo..hi garbled: CRC-bad, its stored CRC intact."""
    data = bytearray(sec.data)
    for i in range(lo, hi):
        data[i] ^= RNG.randrange(1, 256)
    return replace(sec, data=bytes(data), data_crc_ok=False)


def _ccitt_bitwise(data: bytes) -> int:
    """CRC-CCITT the long way (poly 0x1021, preset FFFF, MSB first): DESIGN.md §7.3."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def test_the_data_crc_is_ccitt_over_the_address_mark_and_data():
    data = bytes(range(256)) * 4
    assert vote.data_crc(data, False) == _ccitt_bitwise(b"\xa1\xa1\xa1\xfb" + data)
    assert vote.data_crc(data, True) == _ccitt_bitwise(b"\xa1\xa1\xa1\xf8" + data)


def test_three_reads_wrong_in_different_places_vote_to_the_truth():
    sec = _codeword().sectors[4]
    copies = [_bad_read(sec, 10 * k, 10 * k + 8) for k in range(3)]
    voted = vote.vote_sector(copies)
    assert voted is not None and voted.data == sec.data and voted.data_crc_ok


def test_a_vote_needs_a_quorum_and_a_crc_that_checks():
    sec = _codeword().sectors[5]
    assert vote.vote_sector([_bad_read(sec, 0, 8), _bad_read(sec, 20, 28)]) is None
    # Three reads wrong in the same place: the majority is wrong, the CRC says so.
    same = _bad_read(sec, 0, 8)
    assert vote.vote_sector([same, same, _bad_read(sec, 40, 48)]) is None
    # A copy with no stored CRC (not from flux) does not count toward the quorum.
    no_crc = replace(_bad_read(sec, 60, 68), data_crc=None)
    assert vote.vote_sector([_bad_read(sec, 0, 8), _bad_read(sec, 20, 28), no_crc]) is None


def test_suspects_are_the_coordinates_no_read_got_right_with_a_good_id():
    seg = _codeword()
    good, bad, bad_id = seg.sectors[0], seg.sectors[1], seg.sectors[2]
    passes = [
        [
            _bad_read(good, 0, 4),
            _bad_read(bad, 0, 4),
            replace(_bad_read(bad_id, 0, 4), id_crc_ok=False),
        ],
        [good, _bad_read(bad, 8, 12)],
    ]
    suspects = vote.suspect_copies(passes)
    assert list(suspects) == [vote.key_of(bad)]
    assert len(suspects[vote.key_of(bad)]) == 2


def _five_bad(seg: Segment, reads: int) -> tuple[Segment, dict]:
    """``seg`` as the union would place it with 5 sectors bad, and their copies.

    Each bad sector's reads are garbled in columns no other bad sector's are,
    so every column has at most one erasure once the reads are compared.
    """
    placed = Segment(tpt=0, tps=0, seg=0, sectors=list(seg.sectors))
    suspects = {}
    for k, slot in enumerate((2, 7, 11, 19, 30)):
        copies = [
            _bad_read(seg.sectors[slot], 150 * k + 50 * r, 150 * k + 50 * r + 30)
            for r in range(reads)
        ]
        suspects[vote.key_of(seg.sectors[slot])] = copies
        placed.sectors[slot] = copies[0]
    return placed, suspects


def test_rs_columns_rescue_a_segment_with_five_bad_sectors():
    truth = _codeword()
    placed, suspects = _five_bad(truth, reads=2)
    assert seg_mod.classify(placed).name == "UNCORRECTABLE"
    stats = vote.VoteStats()
    assert vote.rescue_segment(placed, suspects, stats=stats) == 5
    assert [s.data for s in placed.sectors] == [s.data for s in truth.sectors]
    assert rs.verify(placed) is rs.Parity.CONSISTENT
    assert stats.rescued == {0: 5} and len(stats.rebuilt) == 5


def test_rs_columns_refuse_bytes_the_reads_agree_on_but_got_wrong():
    """Both reads of one sector carry the same wrong byte: the column check fails,
    and the segment is left exactly as it was."""
    placed, suspects = _five_bad(_codeword(), reads=2)
    key = next(iter(suspects))
    a, b = suspects[key]
    liar = bytearray(a.data)
    liar[1000] ^= 0x55
    b_liar = bytearray(b.data)
    b_liar[1000] = liar[1000]
    suspects[key] = [replace(a, data=bytes(liar)), replace(b, data=bytes(b_liar))]
    before = list(placed.sectors)
    stats = vote.VoteStats()
    assert vote.rescue_segment(placed, suspects, stats=stats) is None
    assert placed.sectors == before
    ok, width = stats.unrescued[0]
    assert width == 1024 and ok == 1023


def test_rs_columns_give_up_on_a_sector_read_only_once():
    """Read once, a bad sector is an erasure in every column; three of them leave no check."""
    placed, suspects = _five_bad(_codeword(), reads=2)
    for key in list(suspects)[:3]:
        suspects[key] = suspects[key][:1]
    assert vote.rescue_segment(placed, suspects) is None


def test_rank_rs_at_segment_level_passes_over_a_read_with_a_wrong_good_sector():
    """Two reads of one segment, all 32 sectors CRC-good in both; read A has one
    byte wrong (a CRC collision). crc ties and keeps A; rs sees A's parity disagree."""
    truth = _codeword()
    wrong = bytearray(truth.sectors[6].data)
    wrong[3] ^= 1
    a = [replace(s, data=bytes(wrong)) if i == 6 else s for i, s in enumerate(truth.sectors)]
    b = list(truth.sectors)
    geom, bsm = Geometry(tracks=28, segments_per_track=207), BadSectorMap()
    kw = {"tracks": [0, 0], "geometry": geom, "bsm": bsm}
    assert merge.select([a, b], merge.Best.SEGMENT, **kw) == [a, []]
    chosen = merge.select([a, b], merge.Best.SEGMENT, rank=merge.Rank.RS, **kw)
    assert chosen[0] == [] and [s.data for s in chosen[1]] == [s.data for s in truth.sectors]

"""qiclib.rs.verify: the spare-parity check the erasure solve leaves unspent."""

from __future__ import annotations

import random
from dataclasses import replace

import pytest

from qiclib import rs
from qiclib.testing.builders import make_segment_from_sectors, with_parity
from qiclib.types import Segment


def _codeword(seed: int) -> Segment:
    rng = random.Random(seed)
    rows = [bytes(rng.randrange(256) for _ in range(1024)) for _ in range(29)]
    return with_parity(make_segment_from_sectors(0, 0, 0, rows))


def _erase(seg: Segment, *slots: int) -> Segment:
    for slot in slots:
        seg.sectors[slot] = replace(seg.sectors[slot], data_crc_ok=False, data=bytes(1024))
    return seg


def _flip(seg: Segment, slot: int, at: int = 100) -> Segment:
    """A CRC-good sector with one wrong byte: what a CRC-16 collision leaves."""
    data = bytearray(seg.sectors[slot].data)
    data[at] ^= 0x5A
    seg.sectors[slot] = replace(seg.sectors[slot], data=bytes(data))
    return seg


def test_a_whole_codeword_is_consistent():
    assert rs.verify(_codeword(1)) is rs.Parity.CONSISTENT


def test_a_wrong_crc_good_sector_is_inconsistent():
    assert rs.verify(_flip(_codeword(2), 4)) is rs.Parity.INCONSISTENT


@pytest.mark.parametrize("erased", [(3,), (0, 31)])
def test_one_or_two_erasures_leave_parity_to_check_with(erased):
    seg = _erase(_codeword(3), *erased)
    assert rs.verify(seg) is rs.Parity.CONSISTENT
    assert rs.verify(_flip(seg, 10)) is rs.Parity.INCONSISTENT


def test_three_erasures_spend_every_parity_row():
    assert rs.verify(_erase(_codeword(4), 1, 2, 3)) is rs.Parity.UNVERIFIED


def test_four_erasures_are_uncorrectable_and_nothing_read_is_missing():
    assert rs.verify(_erase(_codeword(5), 1, 2, 3, 4)) is rs.Parity.UNCORRECTABLE
    assert rs.verify(Segment(tpt=0, tps=0, seg=0)) is rs.Parity.MISSING


def test_verify_never_writes_into_the_segment():
    seg = _flip(_erase(_codeword(6), 7), 9)
    before = [(s.data, s.data_crc_ok) for s in seg.sectors]
    rs.verify(seg)
    assert [(s.data, s.data_crc_ok) for s in seg.sectors] == before


def test_row_syndromes_agree_with_the_column_syndromes():
    """The bytes.translate fast path is the column formula, all columns at once."""
    seg = _flip(_codeword(7), 12, at=5)
    rows = [s.data for s in seg.sectors]
    fast = [v.to_bytes(1024, "big") for v in rs.row_syndromes(rows)]
    for c in (0, 5, 1023):
        slow = rs.syndromes([row[c] for row in rows], 32)
        assert [f[c] for f in fast] == slow


def test_the_check_holds_under_a_bad_sector_map_repack():
    """Excluded slots leave the codeword: positions are counted over the rest."""
    seg = _codeword(8)
    # Rebuild the parity as if slot 10 were excluded: drop it, re-encode N = 31.
    seg.excluded = {10}
    participating = [i for i in range(32) if i != 10]
    n = len(participating)
    # correct_codeword wants the positions it solves zeroed in what it is given.
    cols = [
        [seg.sectors[slot].data[c] for slot in participating[: n - 3]] + [0, 0, 0]
        for c in range(1024)
    ]
    cols = [rs.correct_codeword(col, [n - 3, n - 2, n - 1], n) for col in cols]
    for pos in (n - 3, n - 2, n - 1):
        slot = participating[pos]
        seg.sectors[slot] = replace(seg.sectors[slot], data=bytes(c[pos] for c in cols))
    assert rs.verify(seg) is rs.Parity.CONSISTENT
    assert rs.verify(_erase(seg, 0, 31)) is rs.Parity.CONSISTENT

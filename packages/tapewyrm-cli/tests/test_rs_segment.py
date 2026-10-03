"""Segment-level Reed-Solomon regressions found on the bench tape (2026-10-01).

Two bugs made every correction of real data wrong while the column math was
right: CRC-failed sectors' corrupt bytes were fed into the solve instead of
zeros, and rebuilt bytes for never-read sectors were dropped. On the bench
tape, segment 94 decompressed 53 bytes short because of the first.
"""

import random

import pytest

from tapewyrm.codec import rs
from tapewyrm.codec import segment as seg_mod
from tapewyrm.types import RawSector, Segment, SegmentStatus


def _encoded_segment(seed: int, excluded: frozenset[int] = frozenset()) -> Segment:
    """A segment of random data with real parity in its last 3 participating rows.

    Parity is computed by erasure-decoding the parity rows themselves (for a
    systematic code, encoding == solving for 3 erased parity symbols), using
    the column decoder verified against QIC-80-MC Rev N Figure 6.3.
    """
    rnd = random.Random(seed)
    seg = Segment(tpt=0, tps=0, seg=0)
    seg.excluded = set(excluded)
    part = [i for i in range(32) if i not in excluded]
    n = len(part)
    rows = [bytes(rnd.randrange(256) for _ in range(1024)) for _ in range(n - 3)]
    rows += [bytes(1024)] * 3
    cols = [[rows[r][c] for r in range(n)] for c in range(1024)]
    cols = [rs.correct_codeword(col, [n - 3, n - 2, n - 1], n) for col in cols]
    for r, slot in enumerate(part):
        seg.sectors[slot] = RawSector(
            fsd=0, ftk=0, fsc=slot + 1, data=bytes(cols[c][r] for c in range(1024)),
            id_crc_ok=True, data_crc_ok=True, deleted=False,
        )  # fmt: skip
    for slot in excluded:  # physically present but outside the codeword
        seg.sectors[slot] = RawSector(
            fsd=0, ftk=0, fsc=slot + 1, data=bytes([0xEE]) * 1024,
            id_crc_ok=True, data_crc_ok=True, deleted=False,
        )  # fmt: skip
    return seg


def _clean_data(seg: Segment) -> bytes:
    return seg_mod.correct_segment(seg).data


@pytest.mark.parametrize("kind", ["missing", "crc_bad"])
@pytest.mark.parametrize("k", [1, 2, 3])
def test_erasures_rebuild_exact_data(kind, k):
    seg = _encoded_segment(seed=k)
    want = _clean_data(seg)
    damaged = _encoded_segment(seed=k)
    for slot in random.Random(100 + k).sample(range(32), k):
        if kind == "missing":
            damaged.sectors[slot] = None  # never read: rebuilt bytes used to be dropped
        else:
            sec = damaged.sectors[slot]
            sec.data, sec.data_crc_ok = bytes([0x5A]) * 1024, False  # corrupt bytes used to leak in
    res = seg_mod.correct_segment(damaged)
    assert res.status is SegmentStatus.CORRECTED
    assert res.data == want


def test_excluded_sector_is_outside_the_codeword():
    # Bench segment 129: sector 11 excluded by the bad-sector map.
    seg = _encoded_segment(seed=7, excluded=frozenset({11}))
    res = seg_mod.correct_segment(seg)
    assert res.status is SegmentStatus.CLEAN
    assert len(res.data) == 28 * 1024  # 31 participating rows - 3 parity
    assert b"\xee" * 1024 not in res.data

    damaged = _encoded_segment(seed=7, excluded=frozenset({11}))
    damaged.sectors[3] = None
    assert seg_mod.correct_segment(damaged).data == res.data

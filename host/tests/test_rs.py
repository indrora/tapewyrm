"""RS erasure decoder tests (DESIGN.md §13.2 ground-truth vector + repack)."""

from __future__ import annotations

from itertools import combinations

import pytest

from tapewyrm.codec import rs
from tapewyrm.codec.rs import correct_codeword, syndromes
from tapewyrm.types import RawSector, Segment, SegmentStatus

# DESIGN.md §13.2 clean test column: rows 0..28 = value row+1; parity 5D FF A3.
CLEAN_CW = [r + 1 for r in range(29)] + [0x5D, 0xFF, 0xA3]


def test_field_constants():
    # exp[105] == 0xC0 (the r^105 constant) and the three generator roots.
    assert rs.EXP[105] == 0xC0
    assert set(rs.ROOTS) == {0x01, 0x02, 0xC3}


def test_clean_codeword_has_zero_syndromes():
    s = syndromes(CLEAN_CW, len(CLEAN_CW))
    assert s == [0, 0, 0]


def test_recover_any_three_erasures():
    """Erase any <=3 of the 32 positions and recover the exact clean codeword."""
    n = len(CLEAN_CW)
    for k in range(0, 4):
        for combo in combinations(range(n), k):
            recv = CLEAN_CW[:]
            for e in combo:
                recv[e] = 0
            out = correct_codeword(recv, list(combo), n)
            assert out == CLEAN_CW, f"failed for erasures {combo}"


def test_too_many_erasures_raises():
    recv = CLEAN_CW[:]
    erasures = [0, 1, 2, 3]
    for e in erasures:
        recv[e] = 0
    with pytest.raises(ValueError):
        correct_codeword(recv, erasures, len(recv))


def _make_segment_from_columns(columns: list[list[int]], bad: set[int], excluded: set[int]):
    """Build a Segment from per-column codewords (rows 0..N-1 -> sector slots).

    ``columns`` is a list of length-`width` codewords? No — here each entry is one
    codeword across the participating rows. We construct width=len(columns) bytes
    per sector. ``bad`` marks slots whose data CRC failed (erasures).
    """
    width = len(columns)
    n_rows = len(columns[0])
    participating = [i for i in range(Segment.SECTORS) if i not in excluded]
    seg = Segment(tpt=0, tps=0, seg=0, excluded=set(excluded))
    for row_idx, slot in enumerate(participating):
        data = bytes(columns[c][row_idx] for c in range(width))
        crc_ok = slot not in bad
        seg.sectors[slot] = RawSector(
            fsd=0,
            ftk=0,
            fsc=slot + 1,
            data=data,
            id_crc_ok=True,
            data_crc_ok=crc_ok,
            deleted=False,
        )
    assert n_rows == len(participating)
    return seg


def test_segment_correct_clean():
    # 4 columns, all the clean vector; no bad sectors -> CLEAN, exact data.
    width = 4
    columns = [CLEAN_CW[:] for _ in range(width)]
    seg = _make_segment_from_columns(columns, bad=set(), excluded=set())
    result = rs.correct(seg)
    assert result.status is SegmentStatus.CLEAN
    assert result.corrected_count == 0
    # data = 29 data rows * width bytes each.
    assert len(result.data) == 29 * width


def test_segment_correct_three_erasures():
    width = 8
    columns = [CLEAN_CW[:] for _ in range(width)]
    seg = _make_segment_from_columns(columns, bad={5, 10, 30}, excluded=set())
    # Zero the bad rows' data to simulate unreadable sectors.
    for slot in (5, 10, 30):
        sec = seg.sectors[slot]
        assert sec is not None
        sec.data = bytes(width)
    result = rs.correct(seg)
    assert result.status is SegmentStatus.CORRECTED
    assert result.corrected_count == 3
    # After correction, the data rows must match the clean vector exactly.
    for row in range(29):
        sec = seg.sectors[row]
        assert sec is not None
        assert list(sec.data) == [CLEAN_CW[row]] * width


def test_segment_uncorrectable_keeps_partial():
    width = 2
    columns = [CLEAN_CW[:] for _ in range(width)]
    seg = _make_segment_from_columns(columns, bad={0, 1, 2, 3}, excluded=set())
    result = rs.correct(seg)
    assert result.status is SegmentStatus.UNCORRECTABLE
    assert result.erasure_count == 4
    # Partial data still returned (29 data rows * width).
    assert len(result.data) == 29 * width


def test_excluded_sector_repack():
    """Excluded sectors shorten the codeword to N = 32 - excluded; still recovers.

    Build a valid length-N codeword (N=31, one excluded) and verify a single
    erasure recovers exactly. We synthesize the shorter codeword by RS-encoding:
    take a length-31 systematic codeword by reusing the field — here we simply
    confirm the repack participates the right slots and the math is consistent by
    encoding a fresh clean codeword for N=31 via re-derivation from parity solve.
    """
    excluded = {15}
    n = Segment.SECTORS - len(excluded)  # 31
    # Build a length-31 codeword: pick 28 data symbols, solve 3 parity so all
    # syndromes are zero (parity occupies the last 3 participating slots).
    data_syms = [(i + 1) for i in range(n - rs.REDUNDANCY)]  # 28 symbols
    cw = _encode_clean(data_syms, n)
    s = syndromes(cw, n)
    assert s == [0, 0, 0], "encoded length-31 codeword must have zero syndromes"

    # Lay it into a segment with slot 15 excluded; erase one participating slot.
    participating = [i for i in range(Segment.SECTORS) if i not in excluded]
    seg = Segment(tpt=0, tps=0, seg=0, excluded=set(excluded))
    width = 1
    for pos, slot in enumerate(participating):
        seg.sectors[slot] = RawSector(
            fsd=0,
            ftk=0,
            fsc=slot + 1,
            data=bytes([cw[pos]]),
            id_crc_ok=True,
            data_crc_ok=True,
            deleted=False,
        )
    # Erase the participating position 7 (some slot).
    erase_slot = participating[7]
    bad = seg.sectors[erase_slot]
    assert bad is not None
    bad.data_crc_ok = False
    bad.data = bytes(width)

    result = rs.correct(seg)
    assert result.status is SegmentStatus.CORRECTED
    # Recovered symbol must match original.
    assert list(seg.sectors[erase_slot].data) == [cw[7]]


def _encode_clean(data_syms: list[int], n: int) -> list[int]:
    """Encode ``data_syms`` (length n-3) into a length-n zero-syndrome codeword.

    Solve the 3 parity symbols (last 3 positions) so all three syndromes vanish:
    sum_i cw[i]*root_j^i = 0  =>  sum over parity = sum over data, then solve the
    3x3 system for the parity positions n-3, n-2, n-1.
    """
    cw = data_syms[:] + [0, 0, 0]
    # Right-hand side: syndromes of the data-only part (parity = 0).
    s = syndromes(cw, n)
    parity_pos = [n - 3, n - 2, n - 1]
    matrix = [[rs.EXP[(rs.ROOT_LOG[j] * parity_pos[e]) % 255] for e in range(3)] for j in range(3)]
    # We need sum_parity contributions to cancel the data syndromes: solve
    # matrix * X = s (since adding X at parity positions changes syndrome by
    # matrix*X; we want total 0 -> matrix*X = s).
    from tapewyrm.codec.rs import _solve_linear

    x = _solve_linear(matrix, s)
    for k, p in enumerate(parity_pos):
        cw[p] = x[k]
    return cw

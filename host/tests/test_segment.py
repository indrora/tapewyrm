"""Segment assembly tests: erasure mask, status classification, data extraction."""

from __future__ import annotations

from tapewyrm.codec import rs
from tapewyrm.codec.segment import classify, correct_segment, erasure_mask, segment_data
from tapewyrm.types import RawSector, Segment, SegmentStatus

CLEAN_CW = [r + 1 for r in range(29)] + [0x5D, 0xFF, 0xA3]


def _clean_segment(width: int = 4) -> Segment:
    seg = Segment(tpt=0, tps=0, seg=0)
    for slot in range(32):
        data = bytes([CLEAN_CW[slot]]) * width
        seg.sectors[slot] = RawSector(
            fsd=0,
            ftk=0,
            fsc=slot + 1,
            data=data,
            id_crc_ok=True,
            data_crc_ok=True,
            deleted=False,
        )
    return seg


def test_erasure_mask_marks_bad_and_deleted_and_missing():
    seg = _clean_segment()
    seg.sectors[3].data_crc_ok = False
    seg.sectors[7].deleted = True
    seg.sectors[9] = None
    mask = erasure_mask(seg)
    assert mask[3] is True
    assert mask[7] is True
    assert mask[9] is True
    assert mask[0] is False


def test_classify_clean():
    assert classify(_clean_segment()) is SegmentStatus.CLEAN


def test_classify_corrected_and_uncorrectable():
    seg = _clean_segment()
    for slot in (1, 2):
        seg.sectors[slot].data_crc_ok = False
    assert classify(seg) is SegmentStatus.CORRECTED

    seg2 = _clean_segment()
    for slot in (1, 2, 3, 4):
        seg2.sectors[slot].data_crc_ok = False
    assert classify(seg2) is SegmentStatus.UNCORRECTABLE


def test_classify_missing():
    seg = Segment(tpt=0, tps=0, seg=0)
    assert classify(seg) is SegmentStatus.MISSING


def test_correct_segment_clean_data_length():
    width = 4
    seg = _clean_segment(width)
    result = correct_segment(seg)
    assert result.status is SegmentStatus.CLEAN
    assert len(result.data) == 29 * width  # 29 data sectors


def test_correct_segment_repairs_erasures():
    width = 3
    seg = _clean_segment(width)
    for slot in (5, 20, 31):
        seg.sectors[slot].data_crc_ok = False
        seg.sectors[slot].data = bytes(width)
    result = correct_segment(seg)
    assert result.status is SegmentStatus.CORRECTED
    assert result.corrected_count == 3
    # Repaired data rows match the clean codeword.
    for row in range(29):
        assert list(seg.sectors[row].data) == [CLEAN_CW[row]] * width


def test_segment_data_helper_matches_result():
    width = 2
    seg = _clean_segment(width)
    result = correct_segment(seg)
    assert segment_data(seg) == result.data


def test_redundancy_constant():
    assert rs.REDUNDANCY == 3

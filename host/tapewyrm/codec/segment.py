"""Segment assembly helpers (DESIGN.md §6.4 step 3-4, §2.3, §7.3).

Bridges :mod:`tapewyrm.codec.place` (binned 32-slot segments) and
:mod:`tapewyrm.codec.rs` (the GF(256) erasure decoder):

  * build the CRC -> erasure mask,
  * apply the excluded-sector repack (codeword length ``N = 31 - bad_blocks``),
  * RS-correct, and
  * extract the 29 data sectors (29 * 1024 bytes) in order.

:class:`SegmentStatus` is set to CLEAN (no erasures), CORRECTED (1..3 fixed),
UNCORRECTABLE (> 3), or MISSING (empty).
"""

from __future__ import annotations

from tapewyrm.codec import rs
from tapewyrm.types import RawSector, Segment, SegmentResult, SegmentStatus

SECTOR_SIZE = RawSector.SIZE  # 1024


def erasure_mask(seg: Segment) -> list[bool]:
    """Per-slot erasure mask over all 32 slots (True = erased / needs RS).

    A slot is erased if it is missing, its data CRC failed, or it is a
    deleted-data (format-time bad) block. Excluded slots are not part of the
    codeword and are reported False here (they are repacked out, not erased).
    """
    mask: list[bool] = []
    for i in range(Segment.SECTORS):
        if i in seg.excluded:
            mask.append(False)
            continue
        sec = seg.sectors[i]
        mask.append(sec is None or not sec.data_crc_ok or sec.deleted)
    return mask


def correct_segment(seg: Segment) -> SegmentResult:
    """RS-correct a segment and return its :class:`SegmentResult`.

    Thin wrapper over :func:`tapewyrm.codec.rs.correct`. The RS layer already
    performs the excluded-sector repack (``N = 32 - excluded``, parity in the
    last 3 non-excluded slots) and the column-wise GF(256) solve; here we add the
    final extraction of the 29 logical data sectors in order and the status
    classification.

    The returned ``data`` is the 29 data sectors concatenated (29 * 1024 bytes
    for a whole segment), suitable to feed straight into volume reassembly.
    """
    result = rs.correct(seg)

    # rs.correct already fills .data with the participating data rows in order
    # and sets the status. For a whole (non-excluded) segment that is exactly the
    # 29 data sectors; the excluded-sector repack drops physically-skipped slots,
    # which by spec are not part of the logical data area either.
    return result


def segment_data(seg: Segment) -> bytes:
    """Return the corrected 29 data sectors of ``seg`` (29 * 1024 bytes).

    Convenience for callers that already corrected the segment and just want the
    data area; recomputes the participating-row extraction without re-solving.
    """
    participating = [i for i in range(Segment.SECTORS) if i not in seg.excluded]
    data_rows = participating[: max(0, len(participating) - Segment.PARITY_ROWS)]
    out = bytearray()
    for slot in data_rows:
        sec = seg.sectors[slot]
        if sec is not None and sec.data:
            out.extend(sec.data)
        else:
            out.extend(bytes(SECTOR_SIZE))
    return bytes(out)


def classify(seg: Segment) -> SegmentStatus:
    """Status of ``seg`` from its erasure count, without mutating it."""
    if all(seg.sectors[i] is None for i in range(Segment.SECTORS)):
        return SegmentStatus.MISSING
    erased = sum(erasure_mask(seg))
    if erased == 0:
        return SegmentStatus.CLEAN
    if erased <= rs.REDUNDANCY:
        return SegmentStatus.CORRECTED
    return SegmentStatus.UNCORRECTABLE

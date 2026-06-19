"""IBM/MFM segment framing + CCITT CRC (DESIGN.md §2.2, §7.3, §6A.5).

A QIC-80 segment is a standard IBM/MFM "track" image at the **byte** level:

    SEGMENT HEADER  12x00 sync . C2 C2 C2 FC (index addr mark) . 4E gaps
    x32 sectors:
        SECTOR ID   12x00 sync . A1 A1 A1 FE (id addr mark)
                    FTK FSD FSC 03 . 2xCRC . 4E gap
        DATA BLOCK  12x00 sync . A1 A1 A1 FB (FB=normal, F8=deleted/bad)
                    1024 data . 2xCRC . 4E gaps

CRC is CCITT ``x^16 + x^12 + x^5 + 1`` (poly 0x1021), register preset all-ones
(0xFFFF), computed over the address-mark bytes + field (the three Ax/Cx sync
bytes, the mark byte, and the payload) — DESIGN.md §7.3.

QIC twist (DESIGN.md §2.2, §6A.5): the FDC's C/H/R/N = ``(FTK, FSD, FSC, 03)``.
We keep the abused fields as ``(ftk, fsd, fsc)`` on the :class:`RawSector` rather
than discarding them — they are the sector's tape coordinate.

This module's *byte-level* framing (scan + CRC + encoder helpers) is fully real
and round-trip tested. The *interval -> MFM bitstream* PLL step
(:func:`recover_sectors`) is the one bench seam and is marked ``TODO(bench)``
(DESIGN.md §13.6 item 1) — wire Greaseweazle's PLL there when available.
"""

from __future__ import annotations

from collections.abc import Iterator

from tapewyrm.types import FluxStream, RawSector

# ---------------------------------------------------------------------------
# Address marks and constants
# ---------------------------------------------------------------------------

SYNC = 0x00  # 12x sync bytes precede each mark
GAP = 0x4E  # gap filler
A1 = 0xA1  # missing-clock sync for ID / data marks
C2 = 0xC2  # missing-clock sync for the index mark
IDAM = 0xFE  # ID address mark
DAM_NORMAL = 0xFB  # data address mark (normal)
DAM_DELETED = 0xF8  # deleted-data address mark (format-time bad block)
IXAM = 0xFC  # index (segment) address mark
SIZE_CODE = 0x03  # N=3 => 1024-byte sector

SECTOR_SIZE = 1024
ID_FIELD_LEN = 4  # FTK FSD FSC 03 (mark byte excluded; CRC covers mark+field)
SYNC_COUNT = 12
A1_COUNT = 3
C2_COUNT = 3
CRC_LEN = 2


# ---------------------------------------------------------------------------
# CCITT CRC (DESIGN.md §7.3)
# ---------------------------------------------------------------------------


def crc_ccitt(data: bytes) -> int:
    """CRC-CCITT (poly 0x1021, preset 0xFFFF), MSB-first, over ``data``.

    Per DESIGN.md §7.3 the CRC is computed over the address-mark bytes + field,
    i.e. callers pass the 3 sync bytes + mark byte + payload.
    """
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return crc & 0xFFFF


# ---------------------------------------------------------------------------
# Encoder helpers (build valid streams for fixtures / round-trip tests)
# ---------------------------------------------------------------------------


def _sync_run() -> bytes:
    return bytes([SYNC] * SYNC_COUNT)


def _id_field_bytes(fsd: int, ftk: int, fsc: int) -> bytes:
    """The 4 ID payload bytes in on-tape order: FTK, FSD, FSC, 03."""
    return bytes([ftk & 0xFF, fsd & 0xFF, fsc & 0xFF, SIZE_CODE])


def build_index_mark() -> bytes:
    """Build the segment index address mark: 12x00 . C2 C2 C2 FC . a gap."""
    return _sync_run() + bytes([C2] * C2_COUNT) + bytes([IXAM]) + bytes([GAP])


def build_sector_bytes(
    fsd: int,
    ftk: int,
    fsc: int,
    data: bytes,
    deleted: bool = False,
    bad_id_crc: bool = False,
    bad_data_crc: bool = False,
) -> bytes:
    """Build one full sector (ID field + data field) as decoded MFM bytes.

    Round-trips with :func:`recover_sectors_from_bytes`. ``bad_id_crc`` /
    ``bad_data_crc`` corrupt the stored CRC so the decoder flags the field bad
    (used to exercise the erasure path in tests).
    """
    if len(data) != SECTOR_SIZE:
        if len(data) < SECTOR_SIZE:
            data = data + bytes(SECTOR_SIZE - len(data))
        else:
            data = data[:SECTOR_SIZE]

    out = bytearray()

    # --- ID field ---
    id_mark = bytes([A1] * A1_COUNT) + bytes([IDAM])
    id_field = _id_field_bytes(fsd, ftk, fsc)
    id_crc = crc_ccitt(id_mark + id_field)
    if bad_id_crc:
        id_crc ^= 0xFFFF
    out += _sync_run()
    out += id_mark
    out += id_field
    out += bytes([(id_crc >> 8) & 0xFF, id_crc & 0xFF])
    out += bytes([GAP])

    # --- data field ---
    dam = DAM_DELETED if deleted else DAM_NORMAL
    data_mark = bytes([A1] * A1_COUNT) + bytes([dam])
    data_crc = crc_ccitt(data_mark + data)
    if bad_data_crc:
        data_crc ^= 0xFFFF
    out += _sync_run()
    out += data_mark
    out += data
    out += bytes([(data_crc >> 8) & 0xFF, data_crc & 0xFF])
    out += bytes([GAP, GAP])

    return bytes(out)


def build_segment_bytes(sectors: list[tuple[int, int, int, bytes]]) -> bytes:
    """Build a whole segment: index mark + the given sectors.

    Each item is ``(fsd, ftk, fsc, data)``; for richer control (deleted / bad
    CRC) build sectors with :func:`build_sector_bytes` and concatenate yourself.
    """
    out = bytearray(build_index_mark())
    for fsd, ftk, fsc, data in sectors:
        out += build_sector_bytes(fsd, ftk, fsc, data)
    return bytes(out)


# ---------------------------------------------------------------------------
# Byte-stream decoder (fully real)
# ---------------------------------------------------------------------------


def _find_mark(decoded: bytes, start: int) -> tuple[int, int] | None:
    """Find the next A1 A1 A1 <mark> or C2 C2 C2 FC from ``start``.

    Returns ``(index_of_first_sync_byte, mark_byte)`` where the index points at
    the first of the 3 sync bytes (A1/C2), or None if none found. The mark byte
    is the one immediately after the 3 sync bytes.
    """
    n = len(decoded)
    i = start
    while i + 3 < n:
        b = decoded[i]
        if b == A1 and decoded[i + 1] == A1 and decoded[i + 2] == A1:
            return i, decoded[i + 3]
        if b == C2 and decoded[i + 1] == C2 and decoded[i + 2] == C2:
            return i, decoded[i + 3]
        i += 1
    return None


def recover_sectors_from_bytes(decoded: bytes) -> Iterator[RawSector]:
    """Scan a decoded MFM **byte** stream and yield :class:`RawSector` objects.

    Recognizes:
      * ``A1 A1 A1 FE`` ID marks   -> FTK, FSD, FSC, 03 + 2-byte CRC
      * ``A1 A1 A1 FB`` data marks -> 1024 bytes + 2-byte CRC (normal)
      * ``A1 A1 A1 F8`` data marks -> deleted-data (format-time bad block)
      * ``C2 C2 C2 FC`` segment index marks (boundary cue; resets pairing)

    An ID mark is paired with the next following data mark. ``id_crc_ok`` /
    ``data_crc_ok`` / ``deleted`` are set from the parsed marks and CRCs.
    """
    n = len(decoded)
    pos = 0
    pending: tuple[int, int, int, bool] | None = None  # (fsd, ftk, fsc, id_crc_ok)

    while pos < n:
        found = _find_mark(decoded, pos)
        if found is None:
            break
        mark_at, mark = found
        sync0 = decoded[mark_at]
        field_start = mark_at + 4  # past 3 sync + mark byte

        if sync0 == C2 and mark == IXAM:
            # Segment boundary: any unpaired ID is dropped (no data field followed).
            pending = None
            pos = field_start
            continue

        if sync0 == A1 and mark == IDAM:
            if field_start + ID_FIELD_LEN + CRC_LEN > n:
                break
            field = decoded[field_start : field_start + ID_FIELD_LEN]
            stored = (decoded[field_start + ID_FIELD_LEN] << 8) | decoded[
                field_start + ID_FIELD_LEN + 1
            ]
            mark_bytes = decoded[mark_at + 3 - A1_COUNT : mark_at + 4]  # 3xA1 + FE
            computed = crc_ccitt(mark_bytes + field)
            ftk, fsd, fsc, _size = field[0], field[1], field[2], field[3]
            pending = (fsd, ftk, fsc, stored == computed)
            pos = field_start + ID_FIELD_LEN + CRC_LEN
            continue

        if sync0 == A1 and mark in (DAM_NORMAL, DAM_DELETED):
            if field_start + SECTOR_SIZE + CRC_LEN > n:
                break
            data = decoded[field_start : field_start + SECTOR_SIZE]
            stored = (decoded[field_start + SECTOR_SIZE] << 8) | decoded[
                field_start + SECTOR_SIZE + 1
            ]
            mark_bytes = decoded[mark_at : mark_at + 4]  # 3xA1 + DAM
            computed = crc_ccitt(mark_bytes + data)
            data_crc_ok = stored == computed
            deleted = mark == DAM_DELETED

            if pending is not None:
                fsd, ftk, fsc, id_crc_ok = pending
            else:
                # Orphan data field with no preceding ID; keep it but flag the ID
                # as unknown/bad so placement can decide what to do.
                fsd = ftk = fsc = 0
                id_crc_ok = False
            yield RawSector(
                fsd=fsd,
                ftk=ftk,
                fsc=fsc,
                data=bytes(data),
                id_crc_ok=id_crc_ok,
                data_crc_ok=data_crc_ok,
                deleted=deleted,
            )
            pending = None
            pos = field_start + SECTOR_SIZE + CRC_LEN
            continue

        # Unknown mark: step past the sync run and keep scanning.
        pos = mark_at + 1


# ---------------------------------------------------------------------------
# Interval -> bitstream -> bytes adapter (PLL step is the bench seam)
# ---------------------------------------------------------------------------


def intervals_to_bytes(flux: FluxStream, rate_kbps: int) -> bytes:
    """Decode flux intervals into a decoded MFM **byte** stream.

    TODO(bench), DESIGN.md §13.6 item 1: the interval -> MFM-bitstream PLL is the
    timing-critical primitive we intend to reuse from Greaseweazle (its PLL +
    bitcell recovery + IBM/MFM framing). Wire that here once the GW flux opcode
    bytes and PLL are available on the bench. The byte-level framing below
    (:func:`recover_sectors_from_bytes`) is fully real and does not depend on
    this step.

    Best-effort placeholder: if the FluxStream's ``intervals`` are exactly the
    decoded byte values (as our offline fixtures produce — see codec.flux), pass
    them through unchanged so the whole stack is end-to-end testable without
    hardware. A real capture's intervals are inter-transition tick counts that
    must go through the PLL; that path is the TODO above.
    """
    # Offline/fixture convention: intervals already hold decoded byte values.
    if all(0 <= v <= 0xFF for v in flux.intervals):
        return bytes(flux.intervals)
    # Real-flux fallback stub: cannot decode without the PLL (see TODO above).
    raise NotImplementedError(
        "interval->bitstream PLL not wired (TODO(bench), DESIGN §13.6 item 1)"
    )


def recover_sectors(flux: FluxStream, rate_kbps: int) -> Iterator[RawSector]:
    """Adapter: flux intervals -> MFM bitstream -> bytes -> sectors.

    Thin wrapper over :func:`intervals_to_bytes` (the bench-seam PLL step) and
    :func:`recover_sectors_from_bytes` (fully real byte framing).
    """
    decoded = intervals_to_bytes(flux, rate_kbps)
    yield from recover_sectors_from_bytes(decoded)

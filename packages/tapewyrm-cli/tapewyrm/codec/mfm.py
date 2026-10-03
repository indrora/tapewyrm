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

Real flux goes through :func:`recover_sectors_from_flux`: Greaseweazle's PLL
(``codec.gwpll``), then :func:`frame_bitcells` and
:func:`recover_sectors_from_bytes`; that path is bench-proven. The older
adapter :func:`recover_sectors` only serves the synthetic-fixture pipeline
(``codec.flux``), whose "intervals" are already decoded bytes.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import dataclass

from qiclib.types import RawSector
from tapewyrm_archive.progress import NULL_PROGRESS, Progress

from tapewyrm.types import FluxStream

log = logging.getLogger(__name__)

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

# Items (bytes scanned) between progress updates in the byte-level loops.
_PROGRESS_EVERY = 1 << 20


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
            log.debug(
                "build_sector_bytes: data is %d bytes < %d; zero-padding", len(data), SECTOR_SIZE
            )
            data = data + bytes(SECTOR_SIZE - len(data))
        else:
            log.debug(
                "build_sector_bytes: data is %d bytes > %d; truncating", len(data), SECTOR_SIZE
            )
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


@dataclass
class ScanStats:
    """What one :func:`recover_sectors_from_bytes` scan found, for the report.

    ``sectors`` counts yielded sectors (``data_crc_ok`` + ``data_bad``);
    ``id_only`` counts ID fields that no data field followed (dropped at an
    index mark, replaced by the next ID, or cut off by the end of the stream).
    Filled in when the scan finishes, so read it after consuming the iterator.
    """

    sectors: int = 0
    data_crc_ok: int = 0
    data_bad: int = 0
    orphan_data: int = 0
    id_only: int = 0
    index_marks: int = 0
    unknown_marks: int = 0


def recover_sectors_from_bytes(
    decoded: bytes,
    *,
    progress: Progress = NULL_PROGRESS,
    stats: ScanStats | None = None,
) -> Iterator[RawSector]:
    """Scan a decoded MFM **byte** stream and yield :class:`RawSector` objects.

    Recognizes:
      * ``A1 A1 A1 FE`` ID marks   -> FTK, FSD, FSC, 03 + 2-byte CRC
      * ``A1 A1 A1 FB`` data marks -> 1024 bytes + 2-byte CRC (normal)
      * ``A1 A1 A1 F8`` data marks -> deleted-data (format-time bad block)
      * ``C2 C2 C2 FC`` segment index marks (boundary cue; resets pairing)

    An ID mark is paired with the next following data mark. ``id_crc_ok`` /
    ``data_crc_ok`` / ``deleted`` are set from the parsed marks and CRCs.

    ``progress`` gets one task in bytes, moved at most every
    ``_PROGRESS_EVERY`` bytes (checked once per mark, never per byte).
    ``stats``, when given, is filled in with the scan's tallies at the end.
    """
    n = len(decoded)
    pos = 0
    pending: tuple[int, int, int, bool] | None = None  # (fsd, ftk, fsc, id_crc_ok)
    # Per-call tallies, logged once after the scan. The guards below fire per
    # mark (thousands per track), so they are counted rather than logged.
    n_index = n_orphan = n_unknown = n_dropped_id = n_yielded = n_data_bad = 0
    log.debug("scanning %d decoded MFM bytes for sector marks", n)
    with progress.task("scanning for sectors", total=n, unit="bytes") as bar:
        next_update = _PROGRESS_EVERY
        while pos < n:
            found = _find_mark(decoded, pos)
            if found is None:
                log.debug("no further sync mark after byte %d of %d; ending scan", pos, n)
                break
            mark_at, mark = found
            if mark_at >= next_update:
                bar.update(mark_at)
                next_update = mark_at + _PROGRESS_EVERY
            sync0 = decoded[mark_at]
            field_start = mark_at + 4  # past 3 sync + mark byte

            if sync0 == C2 and mark == IXAM:
                # Segment boundary: any unpaired ID is dropped (no data field followed).
                n_index += 1
                if pending is not None:
                    n_dropped_id += 1
                pending = None
                pos = field_start
                continue

            if sync0 == A1 and mark == IDAM:
                if field_start + ID_FIELD_LEN + CRC_LEN > n:
                    log.debug(
                        "ID mark at byte %d truncated (stream ends at %d); ending scan", mark_at, n
                    )
                    break
                field = decoded[field_start : field_start + ID_FIELD_LEN]
                stored = (decoded[field_start + ID_FIELD_LEN] << 8) | decoded[
                    field_start + ID_FIELD_LEN + 1
                ]
                mark_bytes = decoded[mark_at + 3 - A1_COUNT : mark_at + 4]  # 3xA1 + FE
                computed = crc_ccitt(mark_bytes + field)
                ftk, fsd, fsc, _size = field[0], field[1], field[2], field[3]
                if pending is not None:
                    n_dropped_id += 1  # the previous ID never got its data field
                pending = (fsd, ftk, fsc, stored == computed)
                pos = field_start + ID_FIELD_LEN + CRC_LEN
                continue

            if sync0 == A1 and mark in (DAM_NORMAL, DAM_DELETED):
                if field_start + SECTOR_SIZE + CRC_LEN > n:
                    log.debug(
                        "data mark %#04x at byte %d truncated (stream ends at %d); ending scan",
                        mark,
                        mark_at,
                        n,
                    )
                    break
                data = decoded[field_start : field_start + SECTOR_SIZE]
                stored = (decoded[field_start + SECTOR_SIZE] << 8) | decoded[
                    field_start + SECTOR_SIZE + 1
                ]
                mark_bytes = decoded[mark_at : mark_at + 4]  # 3xA1 + DAM
                computed = crc_ccitt(mark_bytes + data)
                data_crc_ok = stored == computed
                if not data_crc_ok:
                    n_data_bad += 1
                deleted = mark == DAM_DELETED

                if pending is not None:
                    fsd, ftk, fsc, id_crc_ok = pending
                else:
                    # Orphan data field with no preceding ID; keep it but flag the ID
                    # as unknown/bad so placement can decide what to do.
                    n_orphan += 1
                    fsd = ftk = fsc = 0
                    id_crc_ok = False
                n_yielded += 1
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
            n_unknown += 1
            pos = mark_at + 1
        if pending is not None:
            n_dropped_id += 1  # the stream ended between an ID and its data
        bar.update(n)
    if stats is not None:
        stats.sectors = n_yielded
        stats.data_crc_ok = n_yielded - n_data_bad
        stats.data_bad = n_data_bad
        stats.orphan_data = n_orphan
        stats.id_only = n_dropped_id
        stats.index_marks = n_index
        stats.unknown_marks = n_unknown

    log.debug(
        "sector scan done: %d sectors (%d data-CRC bad, %d orphan data fields), "
        "%d index marks, %d IDs dropped unpaired, %d unknown marks skipped",
        n_yielded,
        n_data_bad,
        n_orphan,
        n_index,
        n_dropped_id,
        n_unknown,
    )


# ---------------------------------------------------------------------------
# Fixture adapter: byte-valued "intervals" -> bytes -> sectors (codec.pipeline)
# ---------------------------------------------------------------------------


def intervals_to_bytes(flux: FluxStream, rate_kbps: int) -> bytes:
    """Pass a fixture's byte-valued "intervals" through as decoded MFM bytes.

    The offline fixtures (see ``codec.flux``) store the decoded byte stream
    where the intervals would be, so the whole pipeline is testable without a
    PLL. Real intervals are tick counts and are refused here: they go through
    :func:`recover_sectors_from_flux` instead.
    """
    # Offline/fixture convention: intervals already hold decoded byte values.
    if all(0 <= v <= 0xFF for v in flux.intervals):
        log.debug(
            "all %d intervals are byte-valued; passing through as decoded MFM bytes",
            len(flux.intervals),
        )
        return bytes(flux.intervals)
    # Real tick intervals: not this adapter's job.
    log.debug(
        "intervals are not byte-valued (%d intervals, %d kbps); refusing",
        len(flux.intervals),
        rate_kbps,
    )
    raise NotImplementedError(
        "intervals are not byte-valued fixture data; decode real flux with "
        "mfm.recover_sectors_from_flux"
    )


def recover_sectors(flux: FluxStream, rate_kbps: int) -> Iterator[RawSector]:
    """Fixture adapter: byte-valued intervals -> bytes -> sectors.

    Thin wrapper over :func:`intervals_to_bytes` (fixture pass-through) and
    :func:`recover_sectors_from_bytes`. Real flux: :func:`recover_sectors_from_flux`.
    """
    log.debug("recovering sectors from %d intervals at %d kbps", len(flux.intervals), rate_kbps)
    decoded = intervals_to_bytes(flux, rate_kbps)
    yield from recover_sectors_from_bytes(decoded)


# ---------------------------------------------------------------------------
# Real flux: bitcells -> sync-aligned bytes -> sectors (bench-proven)
# ---------------------------------------------------------------------------

# 0xA1 with its missing clock bit, as 16 raw MFM cells (the classic 0x4489).
_SYNC_A1_CELLS = b"0100010010001001"
_SYNC3_CELLS = _SYNC_A1_CELLS * 3
# A data field is 3 sync + mark + 1024 + CRC = 1030 bytes; decode a little more.
_MAX_FIELD_BYTES = 1100


def bitcells_to_bytes(cells: bytes | bytearray) -> bytes:
    """Decode MFM bitcells into a sync-aligned byte stream (see :func:`frame_bitcells`)."""
    return frame_bitcells(cells)[0]


def frame_bitcells(
    cells: bytes | bytearray, *, progress: Progress = NULL_PROGRESS
) -> tuple[bytes, int]:
    """Decode MFM bitcells (one 0/1 byte per cell) into ``(bytes, sync runs)``.

    MFM has no byte alignment of its own: it comes from the A1 sync marks. We
    find every run of three A1 syncs, decode the bytes that follow *aligned to
    that run* (data bits are the odd cells of each 16-cell word), stop at the
    next sync run, and concatenate the pieces. Each piece starts with
    ``A1 A1 A1 <mark>``, which is exactly what :func:`recover_sectors_from_bytes`
    scans for. Bytes between fields (gaps) are not needed and are skipped.

    The sync-run count is returned for the report. ``progress`` gets one task
    counting sync runs; each run decodes up to ``_MAX_FIELD_BYTES`` bytes, so a
    per-run update is thousands of cells apart and costs nothing measurable.
    """
    text = bytes(cells).translate(bytes.maketrans(b"\x00\x01", b"01"))
    starts: list[int] = []
    p = text.find(_SYNC3_CELLS)
    while p != -1:
        starts.append(p)
        p = text.find(_SYNC3_CELLS, p + len(_SYNC3_CELLS))

    log.debug("found %d A1 sync runs in %d bitcells; decoding fields", len(starts), len(text))
    out = bytearray()
    with progress.task("framing MFM bytes", total=len(starts), unit="sync runs") as bar:
        for k, s in enumerate(starts):
            nxt = starts[k + 1] if k + 1 < len(starts) else len(text)
            for j in range(min(_MAX_FIELD_BYTES, (nxt - s) // 16)):
                word = text[s + 16 * j : s + 16 * j + 16]
                out.append(int(word[1::2], 2))
            bar.advance()
    return bytes(out), len(starts)


def recover_sectors_from_flux(
    intervals: list[int],
    sample_clock_hz: int,
    rate_kbps: int,
    *,
    progress: Progress = NULL_PROGRESS,
) -> list[RawSector]:
    """Flux tick intervals -> GW PLL -> MFM bytes -> QIC sectors.

    The MFM bitcell is half a data bit: 1 us at 500 kbit/s (QIC-80).
    """
    from tapewyrm.codec import gwpll

    log.debug(
        "running PLL over %d flux intervals (sample clock %d Hz, %d kbps)",
        len(intervals),
        sample_clock_hz,
        rate_kbps,
    )
    cells = gwpll.flux_to_bitcells(
        intervals, sample_clock_hz, bitcell_seconds(rate_kbps), progress=progress
    )
    log.debug("PLL produced %d bitcells; framing MFM bytes", len(cells))
    decoded, _runs = frame_bitcells(cells, progress=progress)
    return list(recover_sectors_from_bytes(decoded, progress=progress))


def bitcell_seconds(rate_kbps: int) -> float:
    """The MFM bitcell time: half a data bit (1 us at 500 kbit/s)."""
    return 1 / (rate_kbps * 2000)

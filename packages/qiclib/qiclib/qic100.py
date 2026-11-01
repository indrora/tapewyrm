"""QIC-100-MC: the GCR serial-recorded minicartridge format (Rev F, 23 May 89).

docs/qic-standards/qic100mc.pdf. This is the *other* floppy-interface
minicartridge family (Irwin's drives, DC2080-class cartridges: 12 or 24
tracks, 12,500 ftpi, 86 MB), and it shares nothing below the cartridge
with QIC-40/80: no MFM, no sectors, no segments. The recording is NRZ1
(section 4.2: a transition in a bitcell is a 1, none is a 0) with each
data nibble recorded as a 5-bit GCR group (section 4.3, Table 1) so that
no more than two zeros run together. One bitcell is 80 microinches of
tape (section 4.5); its *time* depends on the drive's speed, so a decoder
measures it from the preamble rather than assuming it.

A track is a train of records (section 6, Figure 6):

    preamble | start-of-data marker | record-type marker | [data field] | ID field | CRCC | postamble

- preamble: 300 to 320 flux transitions, i.e. that many 1 cells (6.3);
- start-of-data marker: GCR groups ``11111 11110`` (6.4, Table 4);
- record-type marker: one byte, high nibble 15, low nibble the type
  (6.5, Table 5): 0 data, 1 ID, 2 manufacturer's, 3 parity, 4 file-system,
  5 uselog;
- data field: 4,160 bytes, the first 64 the system sub-field and the
  remaining 4,096 user data (6.6, Figure 7); absent from ID records;
- ID field: 26 bytes (6.7, Figures 8 and 9), laid out differently for ID
  and non-ID records;
- CRCC: 5 bytes, most significant first (6.8, section 9.1);
- postamble: 10 to 40 transitions (6.9).

Two records make a frame, an ID record then a non-ID one (section 7,
Figure 13); three frames make a block, two data frames and an XOR parity
frame (section 8, 9.2); frames 0-2 of a track are the manufacturer's
block (flawed-frame list) and 3-5 the uselog block (12.1, 12.3).

This module works on **bitcells** (one 0/1 byte per cell, as
``tapewyrm.codec.gwpll.flux_to_bitcells`` produces for a clock of one
bitcell) and bytes; it never sees flux, so qiclib stays stdlib-only and
the PLL stays where it is. :func:`frame_records` finds and decodes every
record in a cell stream and says where each piece of it sits, in cells,
so a viewer can put it on the time axis. :func:`encode_record` is the
inverse, for fixtures: a synthetic tape is the only QIC-100 tape this
code has met.

TODO(bench): no real QIC-100 capture has been through this yet. The GCR
table, the marker values and the field layouts are the standard's; the
CRCC bit order is derived from the standard's own worked example (see
:func:`crcc`), but its long test vector does not reproduce from the
scanned text and should be checked against a real record.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import IntEnum

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# GCR 0,2 4/5 (section 4.3, Table 1). G4 is recorded first.
# ---------------------------------------------------------------------------

GCR_ENCODE: tuple[int, ...] = (
    0b11001,  # 0 -> 19
    0b11011,  # 1 -> 1B
    0b10010,  # 2 -> 12
    0b10011,  # 3 -> 13
    0b11101,  # 4 -> 1D
    0b10101,  # 5 -> 15
    0b10110,  # 6 -> 16
    0b10111,  # 7 -> 17
    0b11010,  # 8 -> 1A
    0b01001,  # 9 -> 09
    0b01010,  # A -> 0A
    0b01011,  # B -> 0B
    0b11110,  # C -> 1E
    0b01101,  # D -> 0D
    0b01110,  # E -> 0E
    0b01111,  # F -> 0F
)
GCR_DECODE: dict[int, int] = {g: n for n, g in enumerate(GCR_ENCODE)}

PREAMBLE_GROUP = 0b11111  # section 6.3: G4..G0 all 1
SOD_FIRST, SOD_LAST = 0b11111, 0b11110  # section 6.4, Table 4
CELLS_PER_GROUP = 5
CELLS_PER_BYTE = 10

MIN_PREAMBLE = 300  # transitions (6.3)
MAX_PREAMBLE = 320
MIN_POSTAMBLE = 10  # (6.9)
MAX_POSTAMBLE = 40
DATA_FIELD_BYTES = 4160  # (6.6)
SYSTEM_SUBFIELD_BYTES = 64
USER_SUBFIELD_BYTES = 4096
ID_FIELD_BYTES = 26  # (6.7)
CRCC_BYTES = 5  # (6.8)


class RecordType(IntEnum):
    """Record-type marker low nibble (section 6.5, Table 5)."""

    DATA = 0
    ID = 1
    MANUFACTURER = 2
    PARITY = 3
    FILE_SYSTEM = 4
    USELOG = 5


def record_type_name(value: int) -> str:
    try:
        return RecordType(value).name.lower().replace("_", "-")
    except ValueError:
        return f"reserved-{value}"


# ---------------------------------------------------------------------------
# CRCC (section 9.1)
# ---------------------------------------------------------------------------

# x^40 + x^34 + x^33 + x^27 + x^25 + x^23 + x^22 + x^9 + 1, section 9.1.1.
CRCC_TAPS = (34, 33, 27, 25, 23, 22, 9, 0)
_CRCC_MASK = (1 << 40) - 1
_CRCC_TAPMASK = sum(1 << t for t in CRCC_TAPS)


def crcc(data: bytes) -> int:
    """The 40-bit CRCC of ``data`` (the record-type marker through the ID field).

    Section 9.1.2 shows the register (Figure 17, "with pre-multiplication"):
    the data bit is XORed with the bit leaving X40 and the result is fed
    into every tap, X00 included, as the register shifts toward X40;
    preset zero. Bits go in most significant first within each byte: the
    standard's worked example says processing the single byte ``01``
    leaves exactly the tap positions set, ``06 0A C0 02 01``, which only
    happens when the 1 is the last bit in. (Its "without
    pre-multiplication" example quotes ``06 0A C0 01 01`` for the same
    computation over ``01`` plus five zero bytes; that differs in one bit
    from what the register must produce and is taken as a misprint.) The
    result is recorded most significant byte first (section 6.8), which is
    what :func:`crcc_bytes` gives.
    """
    reg = 0
    for byte in data:
        for i in range(7, -1, -1):
            bit = (byte >> i) & 1
            top = (reg >> 39) & 1
            reg = (reg << 1) & _CRCC_MASK
            if top ^ bit:
                reg ^= _CRCC_TAPMASK
    return reg


def crcc_bytes(data: bytes) -> bytes:
    return crcc(data).to_bytes(CRCC_BYTES, "big")


# ---------------------------------------------------------------------------
# ID field (section 6.7, Figures 8 and 9)
# ---------------------------------------------------------------------------


def _word(raw: bytes, off: int) -> int:
    """A 16-bit field: first byte least significant (section 6.0)."""
    return raw[off] | (raw[off + 1] << 8)


def _frame_number(raw: bytes, off: int) -> int:
    """Frame number: bits 8-15 hold the *least* significant byte (6.7.3.2.2), so swapped."""
    return (raw[off] << 8) | raw[off + 1]


@dataclass
class IdField:
    """The 26-byte ID field, decoded; ``None`` where the record type has a ZERO byte."""

    record_type: int
    track: int  # 6.7.3.1
    thread: int | None  # 6.7.3.2.1 (ZERO in ID records)
    frame: int  # 6.7.3.2.2
    error_control: int | None  # 6.7.3.3
    ecc_counter: int | None  # 6.7.3.4: bits 4-7 total frames per block, 0-3 current
    ecc_method: int | None  # 6.7.3.5: bits 4-7 redundancy, 0-3 dispersion
    data_field_length: int | None  # 6.7.3.6: SIZE = 32 * (n + 3); 127 -> 4160
    format_spec: int | None  # 6.7.3.7: bit 7 overlap, 3-6 parallel tracks, 0-2 interleave
    frames_per_track: int  # 6.7.3.8
    tracks_per_tape: int  # 6.7.3.9
    post_data_gap: int  # 6.7.3.10, bytes
    post_id_gap: int  # 6.7.3.11, bytes
    drive_spec: int  # 6.7.3.12: bits 2-3 head type, 0-1 positioner
    date: int  # 6.7.3.13: yy(7) mm(4) dd(5), years since 1980
    time: int  # 6.7.3.14: hh(5) mm(6) ss/2(5); 65535 = unknown
    track_spec: int  # 6.7.3.15, reserved 0
    lot: int  # 6.7.3.16
    manufacturer: str  # 6.7.3.17: two ASCII characters, first in the low byte
    raw: bytes = b""

    @property
    def date_tuple(self) -> tuple[int, int, int] | None:
        """(year, month, day), or None when the recorder had no date (all zero)."""
        if self.date == 0:
            return None
        return 1980 + (self.date >> 9), (self.date >> 5) & 0xF, self.date & 0x1F

    @property
    def time_tuple(self) -> tuple[int, int, int] | None:
        if self.time == 0xFFFF:
            return None
        return self.time >> 11, (self.time >> 5) & 0x3F, (self.time & 0x1F) * 2


def parse_id_field(raw: bytes, record_type: int) -> IdField:
    """Decode an ID field by the layout its record type uses (Figure 8 or 9)."""
    if len(raw) != ID_FIELD_BYTES:
        raise ValueError(f"ID field is {len(raw)} bytes, not {ID_FIELD_BYTES}")
    is_id = record_type == RecordType.ID
    man = raw[24:26].decode("ascii", "replace")
    return IdField(
        record_type=record_type,
        track=raw[0] & 0x1F,
        thread=None if is_id else raw[1] & 0x0F,
        frame=_frame_number(raw, 2),
        error_control=None if is_id else raw[4],
        ecc_counter=None if is_id else raw[5],
        ecc_method=None if is_id else raw[6],
        data_field_length=None if is_id else raw[7],
        format_spec=None if is_id else raw[8],
        frames_per_track=_word(raw, 9),
        tracks_per_tape=raw[11],
        post_data_gap=_word(raw, 12),
        post_id_gap=_word(raw, 14),
        drive_spec=raw[16],
        date=_word(raw, 17),
        time=_word(raw, 19),
        track_spec=raw[21],
        lot=_word(raw, 22),
        manufacturer=man,
        raw=bytes(raw),
    )


def build_id_field(
    record_type: int,
    *,
    track: int,
    frame: int,
    thread: int = 1,
    frames_per_track: int = 618,
    tracks_per_tape: int = 24,
    error_control: int = 2,
    ecc_counter: int = 0x31,
    ecc_method: int = 0x20,
    data_field_length: int = 127,
    format_spec: int = 0x0A,
    post_data_gap: int = 400,
    post_id_gap: int = 100,
    drive_spec: int = 0x01,
    date: int = 0,
    time: int = 0xFFFF,
    lot: int = 0,
    manufacturer: str = "  ",
) -> bytes:
    """A 26-byte ID field for a record of ``record_type`` (fixtures and tests)."""
    is_id = record_type == RecordType.ID
    out = bytearray(ID_FIELD_BYTES)
    out[0] = track
    out[1] = 0 if is_id else thread
    out[2], out[3] = (frame >> 8) & 0xFF, frame & 0xFF  # LSB in bits 8-15
    if not is_id:
        out[4] = error_control
        out[5] = ecc_counter
        out[6] = ecc_method
        out[7] = data_field_length
        out[8] = format_spec
    out[9:11] = frames_per_track.to_bytes(2, "little")
    out[11] = tracks_per_tape
    out[12:14] = post_data_gap.to_bytes(2, "little")
    out[14:16] = post_id_gap.to_bytes(2, "little")
    out[16] = drive_spec
    out[17:19] = date.to_bytes(2, "little")
    out[19:21] = time.to_bytes(2, "little")
    out[21] = 0
    out[22:24] = lot.to_bytes(2, "little")
    out[24:26] = manufacturer.encode("ascii")[:2].ljust(2)
    return bytes(out)


# ---------------------------------------------------------------------------
# Records in a bitcell stream
# ---------------------------------------------------------------------------


@dataclass
class Record:
    """One record found in a cell stream, decoded, with each piece's cell position.

    Positions are cell indexes into the stream handed to
    :func:`frame_records`: ``preamble_at`` is the first 1 of the
    preamble, ``sod_at`` the first cell of the start-of-data marker, and
    so on; ``end`` is the cell after the postamble. A field's cell length
    is 10 per byte.
    """

    record_type: int
    data_field: bytes | None  # None for an ID record
    id_raw: bytes
    id_field: IdField | None  # None when the bytes could not be decoded
    crcc_stored: int
    crcc_ok: bool
    preamble_at: int
    sod_at: int
    type_at: int
    data_at: int | None
    id_at: int
    crcc_at: int
    postamble_at: int
    end: int
    bad_groups: int = 0  # 5-cell groups that were no GCR code (decoded as 0)
    notes: list[str] = field(default_factory=list)

    @property
    def frame(self) -> int | None:
        return self.id_field.frame if self.id_field is not None else None

    @property
    def is_id(self) -> bool:
        return self.record_type == RecordType.ID


def _group_at(cells: Sequence[int], at: int) -> int:
    g = 0
    for i in range(CELLS_PER_GROUP):
        g = (g << 1) | (cells[at + i] & 1)
    return g


def _bytes_at(cells: Sequence[int], at: int, count: int) -> tuple[bytes, int]:
    """Decode ``count`` bytes (two GCR groups each, high nibble first); returns (bytes, bad groups)."""
    out = bytearray(count)
    bad = 0
    for k in range(count):
        hi_g = _group_at(cells, at + k * CELLS_PER_BYTE)
        lo_g = _group_at(cells, at + k * CELLS_PER_BYTE + CELLS_PER_GROUP)
        hi = GCR_DECODE.get(hi_g)
        lo = GCR_DECODE.get(lo_g)
        if hi is None:
            bad += 1
            hi = 0
        if lo is None:
            bad += 1
            lo = 0
        out[k] = (hi << 4) | lo
    return bytes(out), bad


def _run_of_ones(cells: Sequence[int], at: int, limit: int) -> int:
    n = 0
    while at + n < limit and cells[at + n]:
        n += 1
    return n


def frame_records(cells: Sequence[int], *, min_preamble: int = 200) -> list[Record]:
    """Find and decode every record in a stream of 0/1 cells.

    A record announces itself with its preamble: ``min_preamble`` or more
    1 cells in a row (the standard writes 300-320; a PLL settling on a
    real tape may eat some, so the default is lenient). The first 0 after
    such a run is the last cell of the start-of-data marker (``11111
    11110``), and the record-type marker, data field (unless the type is
    ID), ID field and CRCC follow at fixed GCR positions. The CRCC is
    checked over the marker through the ID field. A record that runs off
    the end of the stream is dropped; one whose markers do not decode is
    kept with a note, since a viewer wants to see where it was.
    """
    records: list[Record] = []
    n = len(cells)
    at = 0
    while at < n:
        if not cells[at]:
            at += 1
            continue
        run = _run_of_ones(cells, at, n)
        if run < min_preamble:
            at += run
            continue
        zero_at = at + run  # the 0 that ends the SOD marker's last group
        sod_at = zero_at - 9  # SOD is 10 cells: 11111 1111 0
        type_at = zero_at + 1
        preamble_at = at
        at = zero_at + 1
        if type_at + CELLS_PER_BYTE > n:
            break
        type_byte, type_bad = _bytes_at(cells, type_at, 1)
        notes: list[str] = []
        if type_bad or (type_byte[0] >> 4) != 0xF:
            notes.append(f"record-type marker {type_byte.hex()} is not F?")
        record_type = type_byte[0] & 0x0F
        data_len = 0 if record_type == RecordType.ID else DATA_FIELD_BYTES
        body_cells = (1 + data_len + ID_FIELD_BYTES + CRCC_BYTES) * CELLS_PER_BYTE
        if type_at + body_cells > n:
            log.debug("record at cell %d runs past the stream (%d cells); dropped", preamble_at, n)
            break
        data_at = type_at + CELLS_PER_BYTE if data_len else None
        id_at = type_at + (1 + data_len) * CELLS_PER_BYTE
        crcc_at = id_at + ID_FIELD_BYTES * CELLS_PER_BYTE
        postamble_at = crcc_at + CRCC_BYTES * CELLS_PER_BYTE
        body, bad = _bytes_at(cells, type_at, 1 + data_len + ID_FIELD_BYTES + CRCC_BYTES)
        data_field = body[1 : 1 + data_len] if data_len else None
        id_raw = body[1 + data_len : 1 + data_len + ID_FIELD_BYTES]
        stored = int.from_bytes(body[-CRCC_BYTES:], "big")
        ok = crcc(body[:-CRCC_BYTES]) == stored
        try:
            id_field: IdField | None = parse_id_field(id_raw, record_type)
        except ValueError as exc:
            id_field = None
            notes.append(str(exc))
        post = _run_of_ones(cells, postamble_at, n)
        end = postamble_at + post
        records.append(
            Record(
                record_type,
                data_field,
                id_raw,
                id_field,
                stored,
                ok,
                preamble_at,
                sod_at,
                type_at,
                data_at,
                id_at,
                crcc_at,
                postamble_at,
                end,
                bad,
                notes,
            )
        )
        at = end
    log.debug("qic100: %d records framed from %d cells", len(records), n)
    return records


# ---------------------------------------------------------------------------
# Encoding (fixtures)
# ---------------------------------------------------------------------------


def encode_bytes(data: bytes) -> bytes:
    """Bytes -> GCR cells (one 0/1 byte per cell), high nibble's group first."""
    out = bytearray()
    for byte in data:
        for nibble in (byte >> 4, byte & 0xF):
            g = GCR_ENCODE[nibble]
            out.extend((g >> i) & 1 for i in range(4, -1, -1))
    return bytes(out)


def encode_record(
    record_type: int,
    data_field: bytes | None,
    id_raw: bytes,
    *,
    preamble: int = 310,
    postamble: int = 20,
    corrupt_crcc: bool = False,
) -> bytes:
    """A whole record as cells: preamble, SOD, type marker, data, ID, CRCC, postamble."""
    if record_type == RecordType.ID:
        if data_field is not None:
            raise ValueError("an ID record has no data field")
        body = bytes([0xF0 | record_type]) + id_raw
    else:
        if data_field is None or len(data_field) != DATA_FIELD_BYTES:
            raise ValueError(f"a non-ID record's data field is {DATA_FIELD_BYTES} bytes")
        body = bytes([0xF0 | record_type]) + data_field + id_raw
    if len(id_raw) != ID_FIELD_BYTES:
        raise ValueError(f"ID field must be {ID_FIELD_BYTES} bytes")
    crc = crcc_bytes(body)
    if corrupt_crcc:
        crc = bytes([crc[0] ^ 0x01]) + crc[1:]
    out = bytearray(b"\x01" * preamble)
    for g in (SOD_FIRST, SOD_LAST):
        out.extend((g >> i) & 1 for i in range(4, -1, -1))
    out.extend(encode_bytes(body + crc))
    out.extend(b"\x01" * postamble)
    return bytes(out)


def cells_to_intervals(cells: Sequence[int], ticks_per_cell: int) -> list[int]:
    """NRZ1: a 1 is a transition; the interval between transitions is the cell gap (fixtures)."""
    out: list[int] = []
    gap = 0
    for cell in cells:
        gap += 1
        if cell:
            out.append(gap * ticks_per_cell)
            gap = 0
    return out


def estimate_bitcell_ticks(intervals: Sequence[int], *, sample: int = 200_000) -> int:
    """The bitcell, in ticks, from the interval histogram of the first ``sample`` intervals.

    GCR 4/5 never has more than two 0 cells in a row, so every interval is
    1, 2 or 3 cells, and the preamble of every record is 300-odd 1-cell
    intervals. The 1-cell interval is therefore the *smallest* of the
    histogram's peaks, not necessarily the tallest: a data field of
    alternating nibbles (``5A`` is ``10101 01010``) is nothing but 2-cell
    intervals, and only the preambles and markers contribute 1-cell ones.
    So the answer is the smallest bucket that is not noise (at least one
    part in two hundred of the tallest, and a hundred intervals) and that
    divides the tallest peak as 1, 2 or 3 cells would. Buckets are 2% wide
    so jitter does not split a peak.
    """
    counts: Counter[int] = Counter()
    for x in intervals[:sample]:
        if x > 0:
            counts[round(x / max(1, x // 50)) * max(1, x // 50)] += 1
    if not counts:
        raise ValueError("no intervals to estimate a bitcell from")
    tallest_ticks, tallest = counts.most_common(1)[0]
    floor = max(tallest / 200, min(100, tallest))
    candidates = [
        ticks
        for ticks, n in counts.items()
        if n >= floor and any(abs(tallest_ticks / ticks - k) < 0.15 for k in (1, 2, 3))
    ]
    cell = min(candidates) if candidates else tallest_ticks
    log.debug("qic100: bitcell estimated at %d ticks from %d intervals", cell, sum(counts.values()))
    return cell

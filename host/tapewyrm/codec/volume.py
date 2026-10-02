"""Header segment, bad-sector map, and volume table parsing (DESIGN.md §7.3).

Three things live in / near the header segment:

  * the **format parameter record** (sector 0): signature, format code,
    geometry, dates, ASCII tape name;
  * the **bad-sector map** (sectors 0..28): ascending 3-byte LSN entries;
  * the **volume table** (first segment of the logical area): 128-byte ``VTBL``
    entries describing each file set's segment range.

``volume_streams`` then concatenates, for each VTBL entry, the *data* sectors of
its segment range (the 3 ECC sectors dropped) in logical-segment order into the
file set's Volume Data Area byte stream (DESIGN.md §7.5 input).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field, replace

from tapewyrm.codec import place
from tapewyrm.codec import segment as seg_mod
from tapewyrm.tape.geometry import Geometry, coord_to_lsn
from tapewyrm.types import RawSector, Segment, SegmentStatus

# Format parameter record (DESIGN.md §7.3) ----------------------------------
FPR_SIGNATURE = b"\x55\xaa\x55\xaa"
FORMAT_CODE_VARIABLE = 0x04

# Volume table entry signatures (DESIGN.md §7.3).
SIG_VTBL = b"VTBL"
SIG_XTBL = b"XTBL"  # unicode
SIG_UTID = b"UTID"  # unicode tape name
SIG_EXVT = b"EXVT"  # overflow to another segment
VTBL_ENTRY_LEN = 128

DATA_SECTORS_PER_SEGMENT = Segment.DATA_ROWS  # 29


@dataclass
class VolumeInfo:
    """Decoded format parameter record: header segment sector 0, bytes 0-255.

    Offsets follow QIC-80-MC Rev N §7.1 (docs/qic80n.pdf). Rev N defines the
    header for format code 04; codes 2, 3 and 5 are Rev K fixed formats, which
    we have not got -- but the bench Colorado tape (code 5) matches the Rev N
    field layout byte for byte (segments/track, tracks, name, dates, counters).
    Packed dates are raw; decode them with :func:`decode_short_date`.
    """

    format_code: int  # 4
    segments_per_track: int  # 24-25
    tracks: int  # 26
    max_fsd: int  # 27
    max_ftk: int  # 28
    max_fsc: int  # 29
    tape_name: str  # 30-73
    format_date: int  # 14-17: most recent format
    valid_signature: bool = True  # 0-3 == 55 AA 55 AA
    revision: int = 0  # 5: 0x0D = Rev M, 0x0C = Rev L, 0 = before L
    header_seg: int = 0  # 6-7
    dup_header_seg: int = 0  # 8-9
    first_data_seg: int = 0  # 10-11: first logical area data segment
    last_data_seg: int = 0  # 12-13
    write_date: int = 0  # 18-21: most recent write or format
    name_date: int = 0  # 74-77: when the tape name was written
    reformat_error: bool = False  # 128 == 0xFF: fields lost to a re-format error
    segments_written: int = 0  # 130-133: written/formatted/verified, lifetime
    initial_format_date: int = 0  # 138-141
    format_count: int = 0  # 142-143


@dataclass
class BadSectorMap:
    """Bad-sector map (DESIGN.md §7.3 sectors 0..28).

    ``bad_lsns`` are 1-based LSN entries (converted to 0-based here for use).
    ``bad_segments`` lists whole 32-sector segments flagged bad (high bit of the
    entry's MSB set => that segment is entirely bad).
    """

    bad_lsns: set[int] = field(default_factory=set)  # 0-based LSNs
    bad_segments: set[int] = field(default_factory=set)  # absolute segment numbers

    def is_segment_bad(self, seg_abs: int) -> bool:
        return seg_abs in self.bad_segments

    def is_sector_bad(self, lsn: int) -> bool:
        return lsn in self.bad_lsns


@dataclass
class VtblEntry:
    """One 128-byte ``VTBL`` volume-table entry, QIC-80-MC Rev N §8.

    Bytes 0-56 are always defined. When the vendor-specific bit (byte 56 bit 0)
    is set, "only this bit and the previous bytes (0-55) are defined" and the
    volume is not QIC-80-MC compliant -- so every later field is ``None``. (The
    bench tape's CMS backup is such a volume.) The raw record is kept so a
    vendor-specific decoder can read the rest.
    """

    signature: bytes
    start_seg: int  # 4-5 (word)
    end_seg: int  # 6-7 (word)
    description: str  # 8-51
    flags: int  # 56
    os_type: int | None  # 125: 1 = DOS; anything else = extended format
    compressed: bool | None  # 124 bit 7
    dir_section_size: int | None  # 92-95
    raw: bytes = b""
    date: int = 0  # 52-55, packed (decode_short_date)
    multi_cartridge_seq: int | None = None  # 57
    data_section_size: int | None = None  # 96-103 (quadword)
    compression_code: int | None = None  # 124 bits 0-5 (QIC-123; 0x01 compliant)
    source_label: str | None = None  # 106-121

    # --- derived flag accessors (DESIGN.md §7.5) ---
    @property
    def vendor_specific(self) -> bool:
        return bool(self.flags & 0x01)  # byte 56 bit 0

    @property
    def segment_spanning(self) -> bool:
        return bool(self.flags & 0x10)  # byte 56 bit 4

    @property
    def directory_last(self) -> bool:
        return bool(self.flags & 0x20)  # byte 56 bit 5


# ---------------------------------------------------------------------------
# Short date (DESIGN.md §7.3 / §7.5)
# ---------------------------------------------------------------------------


def decode_short_date(packed: int) -> tuple[int, int, int, int, int, int] | None:
    """Decode a packed short date/time into (year, month, day, hour, min, sec).

    Encoding (QIC-80-MC Rev N §7.1): bits 31..25 = year - 1970;
    bits 24..0 = ``sc + 60*(mn + 60*(hr + 24*(dy + 31*mo)))`` with MO 0-11 and
    DY 0-30. We return a calendar month 1-12 and day 1-31 (this used to leak the
    0-based values, so the bench tape read as "11/22" instead of 23 December).
    ``0`` and all-ones are treated as undefined -> None.
    """
    if packed == 0 or packed == 0xFFFFFFFF:
        return None
    year = (packed >> 25) & 0x7F
    rest = packed & 0x01FFFFFF
    sc = rest % 60
    rest //= 60
    mn = rest % 60
    rest //= 60
    hr = rest % 24
    rest //= 24
    dy = rest % 31
    rest //= 31
    mo = rest
    return (1970 + year, mo + 1, dy + 1, hr, mn, sc)


# ---------------------------------------------------------------------------
# Format parameter record + BSM (header segment)
# ---------------------------------------------------------------------------


def parse_header(seg: Segment) -> tuple[VolumeInfo, BadSectorMap]:
    """Parse a header :class:`Segment` (see :func:`parse_header_data`)."""
    return parse_header_data(seg_mod.segment_data(seg))


def parse_header_data(data: bytes) -> tuple[VolumeInfo, BadSectorMap]:
    """Parse the header segment's format parameter record + bad-sector map.

    ``data`` is the header segment's corrected data area (29 sectors); sector 0
    holds the format parameter record, sectors 0..28 the bad-sector map.
    """
    sector0 = data[:1024] if len(data) >= 1024 else data.ljust(1024, b"\x00")

    def u16(off: int) -> int:
        return int.from_bytes(sector0[off : off + 2], "little")

    def u32(off: int) -> int:
        return int.from_bytes(sector0[off : off + 4], "little")

    name_raw = sector0[30:74]
    vol = VolumeInfo(
        format_code=sector0[4],
        segments_per_track=u16(24),
        tracks=sector0[26],
        max_fsd=sector0[27],
        max_ftk=sector0[28],
        max_fsc=sector0[29],
        tape_name=name_raw.split(b"\x00", 1)[0].decode("ascii", errors="replace").rstrip(),
        # Offset 14, not 74: 74-77 is when the *tape name* was written (Rev N).
        format_date=u32(14),
        valid_signature=sector0[0:4] == FPR_SIGNATURE,
        revision=sector0[5],
        header_seg=u16(6),
        dup_header_seg=u16(8),
        first_data_seg=u16(10),
        last_data_seg=u16(12),
        write_date=u32(18),
        name_date=u32(74),
        reformat_error=sector0[128] == 0xFF,
        segments_written=u32(130),
        initial_format_date=u32(138),
        format_count=u16(142),
    )

    bsm = _parse_bsm(data, vol.format_code)
    return vol, bsm


@dataclass
class LocatedHeader:
    """The header segment found in a set of sectors, plus everything placed by it.

    ``segs`` is the full placement re-done under the header's own geometry, with
    the bad-sector map already applied (so it is ready for RS correction).
    ``header`` is the segment the format parameter record came from -- the first
    copy, or the duplicate when the first could not be read -- and
    ``header_status`` says whether RS had to rebuild any of it.
    """

    vol: VolumeInfo
    bsm: BadSectorMap
    geometry: Geometry
    segs: dict[tuple[int, int], Segment]
    header: Segment
    header_status: SegmentStatus


def locate_header(sectors: Iterable[RawSector], fallback: Geometry) -> LocatedHeader | None:
    """Find the header segment and re-place every sector under its geometry.

    This is the "header first" step every decode starts with (DESIGN.md §7.3):

    1. Place the sectors under ``fallback`` geometry. The header lives at the
       very start of track 0, which places correctly under any geometry -- but
       nothing past it does until we know the tape's floppy tracks per side.
    2. Walk segments in ascending order and take the first one whose data area
       starts with the format parameter record signature. The header is "the
       first defect-free segment", so normally its raw sector 0 already reads.
       When sector 0 is missing or failed its CRC we try an RS correction
       before giving up on that segment; if that fails too, the walk simply
       moves on and finds the duplicate copy one segment later.
    3. Re-place under the header's geometry (floppy tracks per side =
       ``max_ftk + 1`` -- 150 on the bench tape, not Rev N's 255) and apply the
       bad-sector map, which must happen before any RS correction of the data
       segments (see :func:`apply_bsm`).

    Returns ``None`` when no segment carries the signature.
    """
    merged = list(sectors)
    segs = place.place(merged, fallback)
    found = _find_header(segs)
    if found is None:
        return None
    header, header_status, header_data = found
    vol, bsm = parse_header_data(header_data)

    geom = fallback
    if vol.segments_per_track:
        geom = Geometry(
            tracks=vol.tracks or fallback.tracks,
            segments_per_track=vol.segments_per_track,
            ftk_per_side=vol.max_ftk + 1 if vol.max_ftk else fallback.ftk_per_side,
        )
        segs = place.place(merged, geom)
    apply_bsm(segs, bsm)
    return LocatedHeader(vol, bsm, geom, segs, header, header_status)


def _find_header(
    segs: dict[tuple[int, int], Segment],
) -> tuple[Segment, SegmentStatus, bytes] | None:
    """First segment whose (possibly RS-corrected) data begins with the FPR signature."""
    for seg in sorted(segs.values(), key=lambda s: s.seg):
        data = seg_mod.segment_data(seg)
        if data[:4] == FPR_SIGNATURE:
            return seg, seg_mod.classify(seg), data
        sector0 = seg.sectors[0]
        if sector0 is not None and sector0.data_crc_ok and not sector0.deleted:
            continue  # sector 0 read fine and is not a header: not this one
        # Sector 0 is unreadable. Only RS can say whether this was the header.
        # (Deleted-data segments before the header land here too; they are all
        # erasures, so the solve gives up at once.)
        res = seg_mod.correct_segment(seg)
        if res.status is not SegmentStatus.UNCORRECTABLE and res.data[:4] == FPR_SIGNATURE:
            return seg, res.status, res.data
    return None


# Rev N §7.1: the format parameter record is bytes 0-255 of sector 0 (234-255
# unused), so the map -- "sectors 0-28" -- can only begin at offset 256. (We used
# to start at 128, which read the lifetime-segments counter and the initial
# format date as two "bad sectors" on the bench tape.)
BSM_OFFSET = 256

# QIC-113 Rev G section 6: a QIC-113 extended-OS volume on a QIC-40/80 tape sets
# the vendor-specific bit and writes 113 / <revision> at VTBL offsets 58 / 60.
QIC113_SIGNATURE = 113

# Fixed formats (codes 2, 3, 5 -- QIC-80-MC Rev K, not in our Rev N): the map is
# a 32-bit little-endian mask per segment starting at offset 2048 (sector 2),
# bit k = sector k of that segment is excluded. NOT taken from a spec: verified
# empirically on the bench tape (format code 5), where segment 129's mask 0x800
# (sector 11) is the only exclusion that makes segment 129 decode, with the
# QIC-113 uncompressed byte offsets agreeing byte-exactly with segments 128 and
# 130 on both sides. All 32 slots of segment 129 carry valid IDs, so an
# excluded sector is still physically recorded; it just isn't in the codeword.
FIXED_BSM_OFFSET = 2048


def _parse_bsm(header_data: bytes, format_code: int = 4) -> BadSectorMap:
    """Parse the ascending 3-byte LSN bad-sector-map entries (DESIGN.md §7.3).

    The BSM occupies the header segment's data area (sectors 0..28). Entries are
    3 bytes, little-endian-ish ascending 1-based LSNs; ``0`` ends the list. The
    high bit of the MSB set => the whole 32-sector segment containing that LSN is
    bad. The map physically starts after the format parameter record region; we
    scan from a conventional offset and stop at the terminator.
    """
    bsm = BadSectorMap()
    if format_code != 4:
        end = min(len(header_data), 29 * 1024)
        for seg_abs, off in enumerate(range(FIXED_BSM_OFFSET, end - 3, 4)):
            mask = int.from_bytes(header_data[off : off + 4], "little")
            if mask == 0xFFFFFFFF:
                bsm.bad_segments.add(seg_abs)
            elif mask:
                bsm.bad_lsns.update(
                    seg_abs * Segment.SECTORS + k for k in range(32) if mask >> k & 1
                )
        return bsm
    i = BSM_OFFSET
    end = len(header_data)
    while i + 3 <= end:
        b0 = header_data[i]
        b1 = header_data[i + 1]
        b2 = header_data[i + 2]
        if b0 == 0 and b1 == 0 and b2 == 0:
            break  # terminator
        seg_flag = bool(b2 & 0x80)
        lsn_1based = b0 | (b1 << 8) | ((b2 & 0x7F) << 16)
        if lsn_1based == 0:
            break
        lsn0 = lsn_1based - 1
        if seg_flag:
            bsm.bad_segments.add(lsn0 // Segment.SECTORS)
        else:
            bsm.bad_lsns.add(lsn0)
        i += 3
    return bsm


# ---------------------------------------------------------------------------
# Volume table
# ---------------------------------------------------------------------------


def apply_bsm(segs: dict[tuple[int, int], Segment], bsm: BadSectorMap) -> int:
    """Mark each segment's BSM-excluded sectors; returns how many were marked.

    Must run BEFORE Reed-Solomon correction: an excluded sector is not part of
    the codeword (QIC-80-MC Rev N 6.2.5), so correcting without this treats it
    as a damaged data sector and scrambles the segment.
    """
    by_seg: dict[int, set[int]] = {}
    for lsn in bsm.bad_lsns:
        by_seg.setdefault(lsn // Segment.SECTORS, set()).add(lsn % Segment.SECTORS)
    marked = 0
    for seg in segs.values():
        slots = by_seg.get(seg.seg)
        if slots:
            seg.excluded = set(slots)
            marked += len(slots)
    return marked


def parse_volume_table(seg: Segment) -> list[VtblEntry]:
    """Parse a volume-table :class:`Segment` (see :func:`parse_volume_table_data`)."""
    return parse_volume_table_data(seg_mod.segment_data(seg))


def parse_volume_table_data(data: bytes) -> list[VtblEntry]:
    """Parse 128-byte ``VTBL``/``XTBL``/``UTID``/``EXVT`` entries from a segment.

    The volume table is the first segment of the logical area. We scan its data
    area in 128-byte records, recognizing the four signatures; ``UTID`` (tape
    name) and ``EXVT`` (overflow) are recognized but yield no file-set range.
    """
    entries: list[VtblEntry] = []
    off = 0
    n = len(data)
    while off + VTBL_ENTRY_LEN <= n:
        rec = data[off : off + VTBL_ENTRY_LEN]
        sig = rec[0:4]
        if sig == SIG_VTBL:
            entries.append(_parse_vtbl_entry(rec))
        elif sig in (SIG_XTBL, SIG_UTID, SIG_EXVT):
            # Rev N §8.1-8.3: XTBL extends the preceding VTBL (unicode name and
            # password), UTID is a unicode tape name, EXVT chains the table into
            # another segment. None of them is a file set of its own (XTBL used to
            # be parsed as one, producing a bogus entry).
            # TODO: follow EXVT (bytes 6-7 = child segment) for long tables.
            pass
        elif sig == b"\x00\x00\x00\x00":
            break  # empty record => end of table
        # Unknown 4cc: skip this record and continue scanning.
        off += VTBL_ENTRY_LEN
    return entries


def _parse_vtbl_entry(rec: bytes) -> VtblEntry:
    """Decode one 128-byte VTBL/XTBL entry (DESIGN.md §7.3, §7.5)."""

    def text(b: bytes) -> str:
        return b.split(b"\x00", 1)[0].decode("ascii", errors="replace").rstrip()

    flags = rec[56]
    entry = VtblEntry(
        signature=rec[0:4],
        # Words, not doublewords: Rev N §8 (the old 4-byte read at 4/8 gave
        # 172425219 -> 1701603654 on the bench tape instead of 3 -> 2631).
        start_seg=int.from_bytes(rec[4:6], "little"),
        end_seg=int.from_bytes(rec[6:8], "little"),
        description=text(rec[8:52]),
        flags=flags,
        date=int.from_bytes(rec[52:56], "little"),
        os_type=None,
        compressed=None,
        dir_section_size=None,
        raw=rec,
    )
    if flags & 0x01 and int.from_bytes(rec[58:60], "little") != QIC113_SIGNATURE:
        # Vendor specific and NOT a QIC-113 volume: per QIC-80 Rev N nothing
        # past byte 56 is defined.
        return entry
    # Either a plain QIC-80 entry, or a vendor-specific one carrying the QIC-113
    # signature (58/59 = 113, 60/61 = QIC-113 revision: F = 6, G = 7), whose
    # bytes 84-127 QIC-113 Rev G section 6 defines with the same layout. The
    # bench tape is the latter (113, 6: QIC-113 Rev F, DOS extended format).
    return replace(
        entry,
        os_type=rec[125],
        compressed=bool(rec[124] & 0x80),
        dir_section_size=int.from_bytes(rec[92:96], "little"),
        multi_cartridge_seq=rec[57],
        data_section_size=int.from_bytes(rec[96:104], "little"),
        compression_code=rec[124] & 0x3F,
        source_label=text(rec[106:122]),
    )


# ---------------------------------------------------------------------------
# Per-file-set Volume Data Area reassembly
# ---------------------------------------------------------------------------


def volume_streams(
    segs: dict[tuple[int, int], Segment],
    vol: VolumeInfo,
    bsm: BadSectorMap,
) -> list[tuple[VtblEntry, bytes]]:
    """Build each file set's Volume Data Area byte stream.

    For each VTBL entry, concatenate the **data** sectors (the 3 ECC sectors
    dropped) of its segment range ``[start_seg, end_seg]`` in logical-segment
    order. BSM-flagged whole-bad segments are skipped (they hold no logical data).

    Requires the volume-table segment to be locatable: the table is the first
    segment of the logical area. We find it by scanning corrected segments for a
    ``VTBL``/``XTBL`` signature.
    """
    spt = vol.segments_per_track or 1
    by_abs = _segments_by_abs(segs)

    vtbl_seg = find_volume_table_segment(segs)
    if vtbl_seg is None:
        return []
    entries = parse_volume_table(vtbl_seg)

    streams: list[tuple[VtblEntry, bytes]] = []
    for entry in entries:
        out = bytearray()
        for seg_abs in range(entry.start_seg, entry.end_seg + 1):
            if bsm.is_segment_bad(seg_abs):
                continue
            tpt, tps = divmod(seg_abs, spt)
            seg = by_abs.get(seg_abs) or segs.get((tpt, tps))
            if seg is None:
                # Missing segment: emit zero-filled data area to preserve offsets.
                out.extend(bytes(DATA_SECTORS_PER_SEGMENT * 1024))
                continue
            out.extend(seg_mod.segment_data(seg))
        streams.append((entry, bytes(out)))
    return streams


def _segments_by_abs(segs: dict[tuple[int, int], Segment]) -> dict[int, Segment]:
    return {s.seg: s for s in segs.values()}


def find_volume_table_segment(segs: dict[tuple[int, int], Segment]) -> Segment | None:
    """Find the segment whose data area begins with a VTBL/XTBL signature."""
    # Prefer the lowest absolute segment that looks like a volume table.
    candidates = sorted(segs.values(), key=lambda s: s.seg)
    for seg in candidates:
        data = seg_mod.segment_data(seg)
        if data[:4] in (SIG_VTBL, SIG_XTBL):
            return seg
    return None


def header_segment_lsn0(vol: VolumeInfo) -> int:
    """Convenience: LSN of the origin sector (0,0,1) — always 0 (DESIGN.md §7.3)."""
    return coord_to_lsn(0, 0, 1)

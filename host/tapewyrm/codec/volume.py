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

from dataclasses import dataclass, field

from tapewyrm.codec import segment as seg_mod
from tapewyrm.tape.geometry import coord_to_lsn
from tapewyrm.types import Segment

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
    """Decoded format parameter record (DESIGN.md §7.3 sector 0)."""

    format_code: int
    segments_per_track: int
    tracks: int
    max_fsd: int
    max_ftk: int
    max_fsc: int
    tape_name: str
    format_date: int  # packed short-date (see decode_short_date)
    valid_signature: bool = True


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
    """One 128-byte volume-table entry (DESIGN.md §7.3, §7.5)."""

    signature: bytes
    start_seg: int
    end_seg: int
    description: str
    flags: int  # byte 56
    os_type: int  # byte 125
    compressed: bool  # byte 124 bit 7
    dir_section_size: int  # bytes 92..95 (Directory Section Size)
    raw: bytes = b""

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

    Encoding (DESIGN.md §7.3, §7.5): bits 31..25 = year - 1970;
    bits 24..0 = ``sc + 60*(mn + 60*(hr + 24*(dy + 31*mo)))``.
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
    return (1970 + year, mo, dy, hr, mn, sc)


# ---------------------------------------------------------------------------
# Format parameter record + BSM (header segment)
# ---------------------------------------------------------------------------


def parse_header(seg: Segment) -> tuple[VolumeInfo, BadSectorMap]:
    """Parse the header segment's format parameter record + bad-sector map.

    The header segment's *data* area (29 sectors) is taken from the corrected
    segment; sector 0 holds the format parameter record, sectors 0..28 hold the
    ascending BSM entries.
    """
    data = seg_mod.segment_data(seg)
    sector0 = data[:1024] if len(data) >= 1024 else data.ljust(1024, b"\x00")

    valid = sector0[0:4] == FPR_SIGNATURE
    format_code = sector0[4]
    segments_per_track = int.from_bytes(sector0[24:26], "little")
    tracks = sector0[26]
    max_fsd = sector0[27]
    max_ftk = sector0[28]
    max_fsc = sector0[29]
    name_raw = sector0[30:74]
    tape_name = name_raw.split(b"\x00", 1)[0].decode("ascii", errors="replace").rstrip()
    # Format date: packed short-date in the header (offset is vendor-ish; QIC-80-MC
    # stores it after the name area). We read 4 bytes at offset 74.
    format_date = int.from_bytes(sector0[74:78], "little")

    vol = VolumeInfo(
        format_code=format_code,
        segments_per_track=segments_per_track,
        tracks=tracks,
        max_fsd=max_fsd,
        max_ftk=max_ftk,
        max_fsc=max_fsc,
        tape_name=tape_name,
        format_date=format_date,
        valid_signature=valid,
    )

    bsm = _parse_bsm(data)
    return vol, bsm


def _parse_bsm(header_data: bytes) -> BadSectorMap:
    """Parse the ascending 3-byte LSN bad-sector-map entries (DESIGN.md §7.3).

    The BSM occupies the header segment's data area (sectors 0..28). Entries are
    3 bytes, little-endian-ish ascending 1-based LSNs; ``0`` ends the list. The
    high bit of the MSB set => the whole 32-sector segment containing that LSN is
    bad. The map physically starts after the format parameter record region; we
    scan from a conventional offset and stop at the terminator.
    """
    bsm = BadSectorMap()
    # The format parameter record occupies the start of sector 0; the BSM proper
    # begins after it. QIC-80-MC packs the map starting at sector 0 offset 128.
    start = 128
    i = start
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


def parse_volume_table(seg: Segment) -> list[VtblEntry]:
    """Parse 128-byte ``VTBL``/``XTBL``/``UTID``/``EXVT`` entries from a segment.

    The volume table is the first segment of the logical area. We scan its data
    area in 128-byte records, recognizing the four signatures; ``UTID`` (tape
    name) and ``EXVT`` (overflow) are recognized but yield no file-set range.
    """
    data = seg_mod.segment_data(seg)
    entries: list[VtblEntry] = []
    off = 0
    n = len(data)
    while off + VTBL_ENTRY_LEN <= n:
        rec = data[off : off + VTBL_ENTRY_LEN]
        sig = rec[0:4]
        if sig in (SIG_VTBL, SIG_XTBL):
            entries.append(_parse_vtbl_entry(rec))
        elif sig in (SIG_UTID, SIG_EXVT):
            # Recognized extension records; no file-set range to add here.
            pass
        elif sig == b"\x00\x00\x00\x00":
            break  # empty record => end of table
        # Unknown 4cc: skip this record and continue scanning.
        off += VTBL_ENTRY_LEN
    return entries


def _parse_vtbl_entry(rec: bytes) -> VtblEntry:
    """Decode one 128-byte VTBL/XTBL entry (DESIGN.md §7.3, §7.5)."""
    sig = rec[0:4]
    # start/end SEG are 4-byte fields immediately after the signature.
    start_seg = int.from_bytes(rec[4:8], "little")
    end_seg = int.from_bytes(rec[8:12], "little")
    # Description: ASCII, 44 bytes at offset 12 (mirrors the tape-name field).
    description = rec[12:56].split(b"\x00", 1)[0].decode("ascii", errors="replace").rstrip()
    flags = rec[56]
    compressed = bool(rec[124] & 0x80)
    os_type = rec[125]
    dir_section_size = int.from_bytes(rec[92:96], "little")
    return VtblEntry(
        signature=sig,
        start_seg=start_seg,
        end_seg=end_seg,
        description=description,
        flags=flags,
        os_type=os_type,
        compressed=compressed,
        dir_section_size=dir_section_size,
        raw=rec,
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

    vtbl_seg = _find_volume_table_segment(segs)
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


def _find_volume_table_segment(segs: dict[tuple[int, int], Segment]) -> Segment | None:
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

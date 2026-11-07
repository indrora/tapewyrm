"""The header segment: format parameter record and bad-sector map (DESIGN.md §7.3).

Two things live in the header segment, the first defect-free segment of
track 0 (a duplicate copy follows it):

  * the **format parameter record** (sector 0, bytes 0-255): signature,
    format code, geometry, dates, ASCII tape name;
  * the **bad-sector map** (sectors 0..28): ascending 3-byte LSN entries for
    format code 4, a 32-bit mask per segment for the fixed formats.

Everything here is about the *tape*. ``locate_header`` is the "header first"
step every decode starts with, because nothing past track 0 can be placed
until the header has said how many floppy tracks a side carries, and
``apply_bsm`` must run before any Reed-Solomon correction, because an
excluded sector is not part of the codeword. What the tape *contains* -- the
volume table and the volumes it describes -- is the :mod:`qiclib.volume`
package, which builds on the :class:`VolumeInfo` and :class:`BadSectorMap`
decoded here.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field

from qiclib import place
from qiclib import segment as seg_mod
from qiclib.geometry import Geometry, coord_to_lsn
from qiclib.types import RawSector, Segment, SegmentStatus

log = logging.getLogger(__name__)

# Format parameter record (DESIGN.md §7.3) ----------------------------------
FPR_SIGNATURE = b"\x55\xaa\x55\xaa"
FORMAT_CODE_VARIABLE = 0x04


@dataclass
class VolumeInfo:
    """Decoded format parameter record: header segment sector 0, bytes 0-255.

    Offsets follow QIC-80-MC Rev N §7.1 (docs/qic-standards/qic80n.pdf). Rev N defines the
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
    # Pre-formatted tapes only ("All zero if tape not pre-formatted"): the
    # duplicator's identification, e.g. the 3M DC2120's
    # "3M     QIC80-I IO80Fi@68IPS V1.11.22A ID5" with lot "0001".
    manufacturer: str = ""  # 146-189: original manufacturer name/code
    lot_code: str = ""  # 190-233: original manufacturer lot code


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

    def excluded_slots(self) -> dict[int, set[int]]:
        """Segment number -> the sector slots (0-31) the map excludes in it.

        The one place LSNs become per-segment exclusions, so a segment that was
        read (``apply_bsm``) and one that was not (``qiclib.build`` writing a
        MISSING entry) get the same answer: the map describes the tape, not
        what we managed to read of it.
        """
        by_seg: dict[int, set[int]] = {}
        for lsn in self.bad_lsns:
            by_seg.setdefault(lsn // Segment.SECTORS, set()).add(lsn % Segment.SECTORS)
        return by_seg


# ---------------------------------------------------------------------------
# Format parameter record + BSM (header segment)
# ---------------------------------------------------------------------------


def ascii_field(raw: bytes) -> str:
    """A space-filled ASCII field: up to the first NUL, trailing blanks dropped."""
    return raw.split(b"\x00", 1)[0].decode("ascii", errors="replace").rstrip()


def parse_header(seg: Segment) -> tuple[VolumeInfo, BadSectorMap]:
    """Parse a header :class:`Segment` (see :func:`parse_header_data`)."""
    return parse_header_data(seg_mod.segment_data(seg))


def parse_header_data(data: bytes) -> tuple[VolumeInfo, BadSectorMap]:
    """Parse the header segment's format parameter record + bad-sector map.

    ``data`` is the header segment's corrected data area (29 sectors); sector 0
    holds the format parameter record, sectors 0..28 the bad-sector map.
    """
    log.debug("header: parsing format parameter record from %d bytes", len(data))
    if len(data) < 1024:
        log.debug("header: data is %d bytes (< 1024); zero-padding sector 0", len(data))
    sector0 = data[:1024] if len(data) >= 1024 else data.ljust(1024, b"\x00")

    def u16(off: int) -> int:
        return int.from_bytes(sector0[off : off + 2], "little")

    def u32(off: int) -> int:
        return int.from_bytes(sector0[off : off + 4], "little")

    vol = VolumeInfo(
        format_code=sector0[4],
        segments_per_track=u16(24),
        tracks=sector0[26],
        max_fsd=sector0[27],
        max_ftk=sector0[28],
        max_fsc=sector0[29],
        tape_name=ascii_field(sector0[30:74]),
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
        manufacturer=ascii_field(sector0[146:190]),
        lot_code=ascii_field(sector0[190:234]),
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
    log.debug("locate_header: placing %d sectors under fallback geometry %s", len(merged), fallback)
    segs = place.place(merged, fallback)
    log.debug("locate_header: searching %d segments for the header", len(segs))
    found = _find_header(segs)
    if found is None:
        log.debug("locate_header: no segment carries the FPR signature; returning None")
        return None
    header, header_status, header_data = found
    vol, bsm = parse_header_data(header_data)

    geom = fallback
    if not vol.segments_per_track:
        log.debug("locate_header: header has segments_per_track=0; keeping fallback geometry")
    if vol.segments_per_track:
        if not vol.tracks:
            log.debug("locate_header: header tracks=0; using fallback %d", fallback.tracks)
        if not vol.max_ftk:
            log.debug(
                "locate_header: header max_ftk=0; using fallback ftk_per_side %d",
                fallback.ftk_per_side,
            )
        geom = Geometry(
            tracks=vol.tracks or fallback.tracks,
            segments_per_track=vol.segments_per_track,
            ftk_per_side=vol.max_ftk + 1 if vol.max_ftk else fallback.ftk_per_side,
        )
        log.debug(
            "locate_header: re-placing %d sectors under header geometry %s", len(merged), geom
        )
        segs = place.place(merged, geom)
    log.debug("locate_header: applying bad-sector map")
    marked = apply_bsm(segs, bsm)
    log.debug("locate_header: %d sectors excluded by the bad-sector map", marked)
    return LocatedHeader(vol, bsm, geom, segs, header, header_status)


def _find_header(
    segs: dict[tuple[int, int], Segment],
) -> tuple[Segment, SegmentStatus, bytes] | None:
    """First segment whose (possibly RS-corrected) data begins with the FPR signature."""
    # Per-segment misses are counted, not logged one by one: with no header on
    # the capture, every segment on the tape would land here.
    skipped_readable = 0
    rs_tried = 0
    for seg in sorted(segs.values(), key=lambda s: s.seg):
        data = seg_mod.segment_data(seg)
        if data[:4] == FPR_SIGNATURE:
            log.debug(
                "header: FPR signature in raw segment %d (after %d readable misses, %d RS tries)",
                seg.seg,
                skipped_readable,
                rs_tried,
            )
            return seg, seg_mod.classify(seg), data
        sector0 = seg.sectors[0]
        if sector0 is not None and sector0.data_crc_ok and not sector0.deleted:
            skipped_readable += 1
            continue  # sector 0 read fine and is not a header: not this one
        # Sector 0 is unreadable. Only RS can say whether this was the header.
        # (Deleted-data segments before the header land here too; they are all
        # erasures, so the solve gives up at once.)
        log.debug("header: segment %d sector 0 unreadable; trying RS correction", seg.seg)
        rs_tried += 1
        res = seg_mod.correct_segment(seg)
        if res.status is not SegmentStatus.UNCORRECTABLE and res.data[:4] == FPR_SIGNATURE:
            log.debug("header: FPR signature in RS-corrected segment %d (%s)", seg.seg, res.status)
            return seg, res.status, res.data
    log.debug(
        "header: not found (%d readable non-header segments, %d RS tries); returning None",
        skipped_readable,
        rs_tried,
    )
    return None


# Rev N §7.1: the format parameter record is bytes 0-255 of sector 0 (234-255
# unused), so the map -- "sectors 0-28" -- can only begin at offset 256. (We used
# to start at 128, which read the lifetime-segments counter and the initial
# format date as two "bad sectors" on the bench tape.)
BSM_OFFSET = 256

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
    log.debug("bsm: parsing bad-sector map for format code %d", format_code)
    if format_code != 4:
        log.debug(
            "bsm: format code %d is not 4; using the fixed per-segment mask layout", format_code
        )
        end = min(len(header_data), 29 * 1024)
        for seg_abs, off in enumerate(range(FIXED_BSM_OFFSET, end - 3, 4)):
            mask = int.from_bytes(header_data[off : off + 4], "little")
            if mask == 0xFFFFFFFF:
                bsm.bad_segments.add(seg_abs)
            elif mask:
                bsm.bad_lsns.update(
                    seg_abs * Segment.SECTORS + k for k in range(32) if mask >> k & 1
                )
        log.debug("bsm: %d bad segments, %d bad sectors", len(bsm.bad_segments), len(bsm.bad_lsns))
        return bsm
    i = BSM_OFFSET
    end = len(header_data)
    while i + 3 <= end:
        b0 = header_data[i]
        b1 = header_data[i + 1]
        b2 = header_data[i + 2]
        if b0 == 0 and b1 == 0 and b2 == 0:
            log.debug("bsm: terminator at offset %d", i)
            break  # terminator
        seg_flag = bool(b2 & 0x80)
        lsn_1based = b0 | (b1 << 8) | ((b2 & 0x7F) << 16)
        if lsn_1based == 0:
            log.debug("bsm: entry at offset %d has LSN 0 (segment flag only); stopping", i)
            break
        lsn0 = lsn_1based - 1
        if seg_flag:
            bsm.bad_segments.add(lsn0 // Segment.SECTORS)
        else:
            bsm.bad_lsns.add(lsn0)
        i += 3
    log.debug("bsm: %d bad segments, %d bad sectors", len(bsm.bad_segments), len(bsm.bad_lsns))
    return bsm


def apply_bsm(segs: dict[tuple[int, int], Segment], bsm: BadSectorMap) -> int:
    """Mark each segment's BSM-excluded sectors; returns how many were marked.

    Must run BEFORE Reed-Solomon correction: an excluded sector is not part of
    the codeword (QIC-80-MC Rev N 6.2.5), so correcting without this treats it
    as a damaged data sector and scrambles the segment.
    """
    by_seg = bsm.excluded_slots()
    marked = 0
    for seg in segs.values():
        slots = by_seg.get(seg.seg)
        if slots:
            seg.excluded = set(slots)
            marked += len(slots)
    return marked


def header_segment_lsn0(vol: VolumeInfo) -> int:
    """Convenience: LSN of the origin sector (0,0,1) — always 0 (DESIGN.md §7.3)."""
    return coord_to_lsn(0, 0, 1)

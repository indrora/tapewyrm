"""Tape geometry and the sector-ID coordinate algebra (DESIGN.md §7.3, §7.4).

This is the bridge between the FDC's circular-disk addressing ``(FSD, FTK, FSC)``
and the tape's linear reality ``(SEG, TPT, TPS, sector-in-segment)``. The formulas
are stated as fact from QIC-80-MC Rev N:

    LSN = 32640*FSD + 128*FTK + (FSC-1)
    SEG = 1020*FSD + 4*FTK + (FSC-1)//32
        inverse: FSD = SEG//1020 ; FTK = (SEG%1020)//4 ; FSC0 = (SEG%4)*32 + 1
    TPT = SEG // spt ; TPS = SEG % spt

Ranges: 1 <= FSC <= 128, 0 <= FTK <= 254, FSD length-dependent.
(FSD,FTK,FSC) = (0,0,1) => tape track 0, segment 0.
128 sectors = 1 floppy track = 4 segments; 1020 segments = 1 floppy side.

Those constants are Rev N's, which only covers format code 4, where the header
records "maximum floppy track = 254". The general rule is
``floppy tracks per side = header max_ftk + 1``: the bench tape (format code 5,
fixed format) records max_ftk = 149, i.e. 150 tracks = 600 segments per side.
With the 1020 constant every sector on side >= 1 landed in the wrong segment
(874 of 2629 volume segments "missing"). 5796 segments over max side 9 = 10
sides of 600 fits; with 1020 the tape would need only 6. So every function here
takes ``ftk_per_side`` (default 255, Rev N) and :class:`Geometry` carries it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from tapewyrm_archive.types import Direction, TapeFormat

from qiclib import cartridge

log = logging.getLogger(__name__)

SECTORS_PER_SEGMENT = 32
SEGMENTS_PER_FTK = 4
SEGMENTS_PER_FSD = 1020
SECTORS_PER_FTK = 128
SECTORS_PER_FSD = 32640


# ---------------------------------------------------------------------------
# Pure coordinate algebra (no geometry/spt needed)
# ---------------------------------------------------------------------------


FTK_PER_SIDE = 255  # Rev N (format code 4); else header max_ftk + 1


def coord_to_lsn(fsd: int, ftk: int, fsc: int, ftk_per_side: int = FTK_PER_SIDE) -> int:
    """(FSD, FTK, FSC) -> logical sector number."""
    return SECTORS_PER_FTK * (ftk_per_side * fsd + ftk) + (fsc - 1)


def coord_to_seg(fsd: int, ftk: int, fsc: int, ftk_per_side: int = FTK_PER_SIDE) -> int:
    """(FSD, FTK, FSC) -> absolute logical segment."""
    return SEGMENTS_PER_FTK * (ftk_per_side * fsd + ftk) + (fsc - 1) // SECTORS_PER_SEGMENT


def sector_in_segment(fsc: int) -> int:
    """Segment-relative sector index 0..31 for a given FSC (1-based)."""
    return (fsc - 1) % SECTORS_PER_SEGMENT


def seg_to_coord(seg: int, ftk_per_side: int = FTK_PER_SIDE) -> tuple[int, int, int]:
    """Absolute segment -> (FSD, FTK, FSC0) where FSC0 is the segment's first FSC."""
    per_side = SEGMENTS_PER_FTK * ftk_per_side
    fsd = seg // per_side
    ftk = (seg % per_side) // SEGMENTS_PER_FTK
    fsc0 = (seg % SEGMENTS_PER_FTK) * SECTORS_PER_SEGMENT + 1
    return fsd, ftk, fsc0


# ---------------------------------------------------------------------------
# Geometry model
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Track counts and segments-per-track fallbacks, per standard and tape width
# ---------------------------------------------------------------------------

# The recording standard each Report Tape Status format names, spelled the way
# the cartridge profiles spell it. UNKNOWN is read as QIC-80: QIC-117 Rev J
# Note 4 lets the host assume a QIC-80 tape when it can't ask, and it is what
# every bench drive so far has been.
_STANDARD_FOR_FORMAT: dict[TapeFormat, str] = {
    TapeFormat.UNKNOWN: "QIC-80",
    TapeFormat.QIC40: "QIC-40",
    TapeFormat.QIC80: "QIC-80",
    TapeFormat.QIC3010: "QIC-3010",
    TapeFormat.QIC3020: "QIC-3020",
}

# Anything wider than the 0.250 in tape is the 0.315 in (8 mm) "wide" tape,
# which is what Report Tape Status bit 7 reports.
_NARROW_WIDTH_IN = 0.250


def _profiles(fmt: TapeFormat, wide: bool) -> tuple[cartridge.Cartridge, ...]:
    """The cartridge profiles for ``fmt`` on this tape width.

    The track counts come from the cartridge profiles
    (``qiclib/profiles/cartridge/*.toml``), not a second table here; each
    profile cites the standard's cover it took its numbers from:

    * QIC-40-MC Rev M: 20 tracks, 0.250 in only;
    * QIC-80-MC Rev N: 28 tracks (0.250 in) / 36 (0.315 in);
    * QIC-3010-MC Rev H and QIC-3020-MC Rev H: both 40 (0.250 in) / 50 (0.315 in).

    A standard with no wide tape (QIC-40) falls back to its narrow profiles,
    with a warning: the drive said wide, but no standard we know agrees.
    """
    standard = _STANDARD_FOR_FORMAT[fmt]
    of_standard = [c for c in cartridge.catalogue() if c.standard == standard]
    matching = tuple(c for c in of_standard if (c.width_in > _NARROW_WIDTH_IN) == wide)
    if not matching and wide:
        log.warning("%s defines no 0.315 in tape; using its 0.250 in geometry", standard)
        matching = tuple(c for c in of_standard if c.width_in <= _NARROW_WIDTH_IN)
    if not matching:
        # Only a broken profile directory gets here: every standard ships narrow profiles.
        log.debug("no cartridge profile for %s (wide=%s); raising", standard, wide)
        raise ValueError(f"no cartridge profile describes {standard}; reinstall qiclib")
    return matching


def track_count(fmt: TapeFormat, wide: bool = False) -> int:
    """Tracks per cartridge for a recording format on narrow or wide tape.

    ``wide`` is Report Tape Status bit 7 (:class:`tapewyrm_archive.qic117.TapeStatus`).
    Callers that don't know the width pass False: 0.250 in is the common tape
    and, before the 0.315 in Travan-class cartridges, the only one.
    """
    counts = {c.tracks for c in _profiles(fmt, wide)}
    if len(counts) != 1:
        # A standard fixes its track count per width; two counts is a profile typo.
        log.debug("%s wide=%s profiles disagree on tracks %s; raising", fmt.name, wide, counts)
        raise ValueError(
            f"cartridge profiles for {fmt.name} (wide={wide}) disagree on tracks: "
            f"{sorted(counts)}; fix qiclib/profiles/cartridge"
        )
    return counts.pop()


# QIC-117 Rev J, Calibrate Tape Length (cmd 36): "Host software shall override
# the number of segments calibrated to meet QIC-80 Rev A-K specifications for
# fixed length 550 Oe tapes", by this table (calibrated -> override):
#   1-153 -> 100, 154-228 -> 207, 229 or more -> no override.
# The override is QIC-80's alone; the other standards keep what the drive
# calibrated. DESIGN.md §7.3.
_SPT_FALLBACK_SMALL = 100
_SPT_FALLBACK_LARGE = 207
_SPT_FALLBACK_THRESHOLD = 153  # last calibrated count overridden to SMALL
_SPT_OVERRIDE_LIMIT = 228  # last calibrated count overridden at all


def _longest_tape_spt(fmt: TapeFormat, wide: bool) -> int:
    """Segments per track on the longest catalogued tape of ``fmt`` and width.

    The longest, not the shortest, because the capture byte budget
    (:meth:`Geometry.byte_budget`) is sized from spt and must stay an upper
    bound: too small and a pass is cut off before the end of the track.
    """
    longest = max(_profiles(fmt, wide), key=lambda c: c.length_ft)
    if longest.segments_per_track is not None:
        return longest.segments_per_track  # fixed by the standard (QIC-40)
    return cartridge.min_segments_per_track(longest.length_ft, longest.standard)


def fallback_spt(
    calibrated_length: int | None,
    fmt: TapeFormat = TapeFormat.QIC80,
    wide: bool = False,
) -> int:
    """Segments per track when the drive can't report it (DESIGN.md §7.3).

    ``calibrated_length`` is the Calibrate Tape Length (cmd 36) segment count.
    QIC-80 (and an unknown format, read as QIC-80) applies the QIC-117 override
    table above; every other standard uses the calibrated count as-is. With no
    calibration at all, QIC-80 assumes the 425 ft class (207, the common
    tape); the others assume their longest catalogued tape.
    """
    is_qic80 = _STANDARD_FOR_FORMAT[fmt] == "QIC-80"
    if calibrated_length is None:
        if is_qic80:
            log.debug("no calibrated length; defaulting to %d segments/track", _SPT_FALLBACK_LARGE)
            return _SPT_FALLBACK_LARGE  # 425ft-class default
        spt = _longest_tape_spt(fmt, wide)
        log.debug(
            "no calibrated length for %s (wide=%s); assuming its longest tape, %d segments/track",
            fmt.name,
            wide,
            spt,
        )
        return spt
    if not is_qic80 or calibrated_length > _SPT_OVERRIDE_LIMIT:
        log.debug("%s calibrated %d segments/track; not overridden", fmt.name, calibrated_length)
        return calibrated_length
    log.debug("QIC-117 override for %d calibrated segments/track", calibrated_length)
    return (
        _SPT_FALLBACK_SMALL if calibrated_length <= _SPT_FALLBACK_THRESHOLD else _SPT_FALLBACK_LARGE
    )


@dataclass(frozen=True)
class Geometry:
    """Tape geometry: track count, segments/track, sector size.

    Track counts depend on standard and tape width; see :func:`track_count`
    (DESIGN.md §7.3).
    """

    tracks: int
    segments_per_track: int
    sectors_per_segment: int = SECTORS_PER_SEGMENT
    ftk_per_side: int = FTK_PER_SIDE  # header max_ftk + 1 (see module docstring)

    @classmethod
    def for_format(
        cls,
        fmt: TapeFormat,
        segments_per_track: int | None = None,
        wide: bool = False,
        calibrated_length: int | None = None,
    ) -> Geometry:
        """Build geometry from reported tape format + (optional) spt.

        ``wide`` is Report Tape Status bit 7; pass False when the width is
        unknown (assumed 0.250 in, see :func:`track_count`).
        """
        tracks = track_count(fmt, wide)
        if not segments_per_track:
            log.debug(
                "segments_per_track %r not reported; using fallback for calibrated length %r",
                segments_per_track,
                calibrated_length,
            )
        spt = segments_per_track or fallback_spt(calibrated_length, fmt, wide)
        log.debug("geometry for %s (wide=%s): %d tracks x %d spt", fmt.name, wide, tracks, spt)
        return cls(tracks=tracks, segments_per_track=spt)

    def total_segments(self) -> int:
        return self.tracks * self.segments_per_track

    def direction(self, track: int) -> Direction:
        """Even tracks forward, odd reverse (logical) — DESIGN.md §2.2/§7.3."""
        return Direction.for_track(track)

    def seg_to_tpt_tps(self, seg: int) -> tuple[int, int]:
        """Absolute logical segment -> (tape track, segment-relative-to-track)."""
        return divmod(seg, self.segments_per_track)

    def place(self, fsd: int, ftk: int, fsc: int) -> tuple[int, int, int, int]:
        """(FSD, FTK, FSC) -> (SEG, TPT, TPS, sector-in-segment).

        The single self-locating step that lets capture order be irrelevant
        (DESIGN.md §2.3).
        """
        seg = coord_to_seg(fsd, ftk, fsc, self.ftk_per_side)
        tpt, tps = self.seg_to_tpt_tps(seg)
        return seg, tpt, tps, sector_in_segment(fsc)

    def byte_budget(self, rate_kbps: int, safety: float = 1.5) -> int:
        """Upper-bound GW-flux byte budget for one full track pass.

        Heuristic stop-condition ceiling: the real pass ends earlier on EOT /
        END. Sized from on-tape bytes per segment * spt * safety.

        TODO(bench): calibrate the flux-bytes-per-decoded-byte factor against a
        real capture; the GW flux encoding packs inter-transition intervals, so
        the true ratio depends on the opcode/continuation scheme (§13.6 item 1).
        """
        # 32 sectors * (1024 data + ~70 framing/CRC/gap) decoded bytes per segment.
        on_tape_per_segment = self.sectors_per_segment * (1024 + 70)
        # MFM doubles bit count; GW packs ~1 flux byte per transition -> ~ x2.
        flux_per_segment = on_tape_per_segment * 2
        return int(flux_per_segment * self.segments_per_track * safety)

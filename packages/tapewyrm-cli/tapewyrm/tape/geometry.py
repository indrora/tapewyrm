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

# DESIGN.md §7.3: if the drive lacks Calibrate / Report-Format-Segments (CCS-2),
# fall back to the QIC-117 override based on calibrated tape length:
#   100 segments/track for <= 153 (calibrated units), 207 for 154..228.
_SPT_FALLBACK_SMALL = 100
_SPT_FALLBACK_LARGE = 207
_SPT_FALLBACK_THRESHOLD = 153


def fallback_spt(calibrated_length: int | None) -> int:
    """Segments-per-track fallback when geometry can't be reported (§7.3)."""
    if calibrated_length is None:
        log.debug("no calibrated length; defaulting to %d segments/track", _SPT_FALLBACK_LARGE)
        return _SPT_FALLBACK_LARGE  # 425ft-class default
    return (
        _SPT_FALLBACK_SMALL if calibrated_length <= _SPT_FALLBACK_THRESHOLD else _SPT_FALLBACK_LARGE
    )


@dataclass(frozen=True)
class Geometry:
    """Tape geometry: track count, segments/track, sector size.

    28 tracks for 0.250 in tape, 36 for 0.315 in / Travan (DESIGN.md §7.3).
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

        QIC-3010/3020 track counts differ (DESIGN.md §9 item 8) — folded in here
        as deltas; refine when those formats are the target.
        """
        if wide:
            tracks = 36  # 0.315 in / Travan
        elif fmt in (TapeFormat.QIC3010, TapeFormat.QIC3020):
            tracks = 40 if fmt is TapeFormat.QIC3010 else 50  # TODO(bench): confirm
        else:
            tracks = 28  # 0.250 in QIC-40/80
        if not segments_per_track:
            log.debug(
                "segments_per_track %r not reported; using fallback for calibrated length %r",
                segments_per_track,
                calibrated_length,
            )
        spt = segments_per_track if segments_per_track else fallback_spt(calibrated_length)
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

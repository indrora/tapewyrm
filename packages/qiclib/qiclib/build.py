"""Sectors -> TWTI tape image: the layout half of ``tw convert``.

The physical layer (tapewyrm-cli's MFM decoder) turns each capture into a
list of :class:`~qiclib.types.RawSector`; this module does everything after
that: merge the passes, find the header segment, place every sector by its
own address under the header's geometry, apply the bad-sector map,
Reed-Solomon correct each segment and write the image with
``tapewyrm_archive.twti``.
"""

from __future__ import annotations

import logging
import time
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from tapewyrm_archive.progress import NULL_PROGRESS, Progress
from tapewyrm_archive.twti import SEGMENT_STRIDE, VERSION, SegmentEntry, SegmentState, TapeImage

from qiclib import cartridge as cartridge_mod
from qiclib import merge
from qiclib import segment as seg_mod
from qiclib import volume as volume_mod
from qiclib.geometry import Geometry
from qiclib.types import RawSector, SegmentStatus

log = logging.getLogger(__name__)

_FROM_RS = {
    SegmentStatus.CLEAN: SegmentState.CLEAN,
    SegmentStatus.CORRECTED: SegmentState.CORRECTED,
    SegmentStatus.UNCORRECTABLE: SegmentState.UNCORRECTABLE,
    SegmentStatus.MISSING: SegmentState.MISSING,
}


# ---------------------------------------------------------------------------
# Drive identity: which source capture speaks for the drive
# ---------------------------------------------------------------------------

# The QIC-117 reports a TWRF header carries (TWS-1). A TWRF v2 header always
# has every one of these keys, with null for a report the drive did not
# answer, so *presence* of a key says nothing; only a non-null value means
# "the drive reported". rate_kbps, device_serial and firmware_commit are
# deliberately absent: they describe the capture device, not the drive.
DRIVE_REPORTS = ("drive_status", "drive_config", "drive_rom", "drive_vendor_id", "tape_status")

# Members that identify *which* drive and tape a capture came from. Two
# reporting captures that disagree on any of them were made on different
# Greaseweazles, drives or cartridges, and merging them is suspicious.
DRIVE_IDENTITY = ("device_serial", "drive_vendor_id", "tape_status")


def reporting_drive(sources: list[dict]) -> dict | None:
    """The TWRF header of the first source whose drive reported anything.

    ``sources`` is a TWTI ``sources`` array (TWS-2 section 4.5). A source
    "reported" when at least one of :data:`DRIVE_REPORTS` is non-null in its
    ``twrf``; captures whose drive stayed silent are skipped. Returns None
    when no source reported.

    When later reporting sources disagree with the chosen one on a member of
    :data:`DRIVE_IDENTITY` (each side non-null and non-empty), the first is
    still kept and a WARNING names every disagreeing member. Nothing extra is
    recorded in the image: the ``sources`` array already keeps every capture's
    reports, so the disagreement stays recoverable from the header itself.
    """
    reporting = []
    for n, source in enumerate(sources):
        twrf = source.get("twrf") or {}
        if all(twrf.get(key) is None for key in DRIVE_REPORTS):
            log.debug("source %d (%s): no drive reports; skipped", n, source.get("file", "?"))
            continue
        reporting.append((n, source, twrf))
    if not reporting:
        log.debug(
            "none of %d sources carries a drive report; drive identity left null", len(sources)
        )
        return None
    first_n, first_source, first = reporting[0]
    log.debug("source %d (%s) speaks for the drive", first_n, first_source.get("file", "?"))
    for n, source, twrf in reporting[1:]:
        differing = [
            f"{key} {first[key]!r} vs {twrf[key]!r}"
            for key in DRIVE_IDENTITY
            if first.get(key) not in (None, "")
            and twrf.get(key) not in (None, "")
            and first[key] != twrf[key]
        ]
        if differing:
            log.warning(
                "source %d (%s) disagrees with source %d (%s): %s; "
                "keeping source %d's drive identity -- were these captures of the same tape "
                "on the same drive?",
                n,
                source.get("file", "?"),
                first_n,
                first_source.get("file", "?"),
                ", ".join(differing),
                first_n,
            )
    return first


def build_image(
    passes: list[list[RawSector]],
    out: Path,
    *,
    sources: list[dict],
    tw_commit: str | None = None,
    progress: Progress = NULL_PROGRESS,
) -> TapeImage:
    """Merge ``passes``, correct every segment and write a TWTI image to ``out``.

    ``sources`` is one provenance dict per capture (its TWRF header under
    ``"twrf"``); the first that carries drive reports names the drive.
    ``tw_commit`` stamps which build of the physical decoder produced it.
    """
    source_meta = sources
    log.debug("merging %d passes", len(passes))
    union_stats = merge.UnionStats()
    merged = list(merge.union(passes, progress=progress, stats=union_stats))
    # "Added" = coordinates no earlier pass had (another track, or a re-read
    # that got further); "fixed" = a CRC-good copy replacing a CRC-bad one.
    log.info(
        "merge: %s, %s sectors -> %s unique; later passes added %s, fixed %s CRC-bad",
        _plural(union_stats.passes, "pass", "passes"),
        f"{union_stats.seen:,}",
        f"{union_stats.unique:,}",
        f"{union_stats.filled_new:,}",
        f"{union_stats.filled_upgraded:,}",
    )
    log.debug("merged to %d sectors; locating the header segment", len(merged))

    # The header places right under any geometry; then the header's own
    # geometry places everything else (floppy tracks per side). See
    # volume.locate_header, shared with the pipeline and `qicsilver identify`.
    located = volume_mod.locate_header(merged, Geometry(tracks=28, segments_per_track=207))
    if located is None:
        log.debug(
            "locate_header found no header segment in %d merged sectors; refusing", len(merged)
        )
        raise ValueError("no header segment in these captures")
    vol, bsm, geom, segs = located.vol, located.bsm, located.geometry, located.segs
    by_seg = {s.seg: s for s in segs.values()}

    total = geom.total_segments()
    _log_header(located, total)
    log.debug(
        "header geometry %s: %d segments, %d bad per the bad-sector map, %d placed",
        geom,
        total,
        len(bsm.bad_segments),
        len(by_seg),
    )
    entries: list[SegmentEntry] = []
    data: dict[int, bytes] = {}
    # BAD/MISSING are common (whole unread tracks), so they're counted here
    # and summarized after the loop rather than logged per segment.
    n_bad = n_missing = 0
    # A MISSING segment still has a size: the sectors the map excludes are not
    # data. Record its mask so readers (qiclib.extract) size its hole right
    # instead of assuming a full 29 sectors.
    excluded = bsm.excluded_slots()
    log.info("correcting %s segments (Reed-Solomon)...", f"{total:,}")
    with progress.task("correcting segments", total=total, unit="segments") as bar:
        for n in range(total):
            bar.advance()
            if n in bsm.bad_segments:
                n_bad += 1
                entries.append(SegmentEntry(SegmentState.BAD))
                continue
            seg = by_seg.get(n)
            if seg is None:
                n_missing += 1
                mask = sum(1 << k for k in excluded.get(n, ()))
                entries.append(SegmentEntry(SegmentState.MISSING, excluded_mask=mask))
                continue
            res = seg_mod.correct_segment(seg)
            mask = sum(1 << k for k in seg.excluded)
            data[n] = res.data
            entries.append(
                SegmentEntry(_FROM_RS[res.status], res.erasure_count, len(res.data), mask)
            )
    log.debug(
        "segment correction: %d bad-map segments skipped, %d missing, %d corrected/kept",
        n_bad,
        n_missing,
        len(data),
    )
    tally = {state: 0 for state in SegmentState}
    for entry in entries:
        tally[entry.state] += 1
    log.info(
        "segments: %s clean, %s corrected, %s uncorrectable, %s missing, %s bad (map)",
        *(
            f"{tally[state]:,}"
            for state in (
                SegmentState.CLEAN,
                SegmentState.CORRECTED,
                SegmentState.UNCORRECTABLE,
                SegmentState.MISSING,
                SegmentState.BAD,
            )
        ),
    )

    first = reporting_drive(source_meta) or {}
    header = {
        "format": "TWTI",
        "version": VERSION,
        "created": datetime.now(UTC).isoformat(timespec="seconds"),
        "tw_commit": tw_commit,
        "segment_count": total,
        "segment_stride": SEGMENT_STRIDE,
        "geometry": asdict(geom),
        "qic80_header": asdict(vol),
        "drive": {
            k: first.get(k)
            for k in (
                "device_serial",
                "drive_status",
                "drive_config",
                "drive_rom",
                "drive_vendor_id",
                "tape_status",
                "rate_kbps",
                "firmware_commit",
            )
        },  # fmt: skip
        "sources": source_meta,
    }
    img = TapeImage(header=header, entries=entries)
    log.info("writing %s segments (%s) to %s...", f"{total:,}", _mb(total * SEGMENT_STRIDE), out)
    started = time.perf_counter()
    img.save(out, lambda n: data.get(n, b""), progress=progress)
    log.info(
        "wrote %s: %s in %.1f s",
        out.name,
        _mb(out.stat().st_size),
        time.perf_counter() - started,
    )
    return TapeImage.open(out)


def _plural(count: int, one: str, many: str) -> str:
    return f"{count:,} {one if count == 1 else many}"


def _mb(size: int) -> str:
    """A byte count as decimal megabytes, the unit rich's file-size bars use."""
    return f"{size / 1e6:,.1f} MB"


def _log_header(located: volume_mod.LocatedHeader, total: int) -> None:
    """Say what the header segment told us: who the tape is and its layout.

    Split over several lines so each stays readable when rich wraps the log
    (tape names and the cartridge description can each be long).
    """
    vol, bsm, geom = located.vol, located.bsm, located.geometry
    log.info(
        "header: segment %d (%s), tape name %s, format code %d",
        located.header.seg,
        located.header_status.name.lower(),
        repr(vol.tape_name.strip()) if vol.tape_name.strip() else "(none)",
        vol.format_code,
    )
    log.info(
        "geometry: %d tracks x %d segs/track = %s segments",
        geom.tracks,
        geom.segments_per_track,
        f"{total:,}",
    )
    log.info(
        "bad-sector map: %s whole segments + %s sectors",
        f"{len(bsm.bad_segments):,}",
        f"{len(bsm.bad_lsns):,}",
    )
    guess = cartridge_mod.guess(geom.tracks, geom.segments_per_track)
    log.info("cartridge: %s", guess.describe())

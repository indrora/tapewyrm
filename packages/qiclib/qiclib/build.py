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
from dataclasses import replace as copy_of
from datetime import UTC, datetime
from pathlib import Path

from tapewyrm_archive.progress import NULL_PROGRESS, Progress
from tapewyrm_archive.twti import SEGMENT_STRIDE, VERSION, SegmentEntry, SegmentState, TapeImage

from qiclib import cartridge as cartridge_mod
from qiclib import header as header_mod
from qiclib import merge, rs
from qiclib import segment as seg_mod
from qiclib import vote as vote_mod
from qiclib.geometry import Geometry
from qiclib.mergelog import MergeLog, ReadInfo, format_fill
from qiclib.types import RawSector, SegmentStatus

log = logging.getLogger(__name__)

# The geometry the header is first looked for under: it places right under any
# (volume.locate_header), and the header then says the real one.
_FALLBACK = Geometry(tracks=28, segments_per_track=207)

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
    best: merge.Best = merge.Best.SECTOR,
    rank: merge.Rank = merge.Rank.CRC,
    vote: bool = True,
    progress: Progress = NULL_PROGRESS,
) -> TapeImage:
    """Merge ``passes``, correct every segment and write a TWTI image to ``out``.

    ``sources`` is one provenance dict per capture (its TWRF header under
    ``"twrf"``); the first that carries drive reports names the drive.
    ``tw_commit`` stamps which build of the physical decoder produced it.
    ``best`` and ``rank`` choose whole reads of a track or segment before the
    merge (:func:`qiclib.merge.select`); the default, by sector, is the plain
    union. ``vote`` rebuilds what no single read got right from several
    (:mod:`qiclib.vote`): a sector by byte-wise majority when its CRC then
    checks, and a segment that would not correct one RS column at a time,
    checked against its spare parity. Every segment is checked against its
    spare parity either way (:func:`qiclib.rs.verify`), and how each track
    came together is logged track by track (:mod:`qiclib.mergelog`).
    """
    source_meta = sources
    best, rank = merge.Best(best), merge.Rank(rank)
    # What each read held, before select empties the losers: the per-track
    # log lists every read of a track, used or not.
    reads = [
        _read_info(i, sectors, source_meta[i] if i < len(source_meta) else {})
        for i, sectors in enumerate(passes)
    ]
    if best is not merge.Best.SECTOR:
        passes = _select(passes, source_meta, best, rank, progress)
    votes = vote_mod.VoteStats()
    suspects = vote_mod.suspect_copies(passes) if vote else {}
    voted = vote_mod.vote_sectors(suspects, stats=votes) if vote else []
    log.debug("merging %d passes (+ %d voted sectors)", len(passes), len(voted))
    union_stats = merge.UnionStats()
    origin: dict[tuple[int, int, int], int] = {}
    # The voted sectors go in as one more pass, last: each is CRC-good where
    # no read was, so it fills exactly those coordinates (an "upgrade" to the
    # union, which is why it is taken back out of "fixed" below).
    inputs = [*passes, voted] if voted else passes
    merged = list(merge.union(inputs, progress=progress, stats=union_stats, origin=origin))
    # "Added" = coordinates no earlier pass had (another track, or a re-read
    # that got further); "fixed" = a CRC-good copy replacing a CRC-bad one.
    log.info(
        "merge: %s, %s sectors -> %s unique; later reads added %s, fixed %s CRC-bad",
        _plural(len(passes), "read", "reads"),
        f"{union_stats.seen - len(voted):,}",
        f"{union_stats.unique:,}",
        f"{union_stats.filled_new:,}",
        f"{union_stats.filled_upgraded - len(voted):,}",
    )
    if vote:
        log.info(
            "vote: %s sectors read only CRC-bad; %s of them read %d+ times, %s rebuilt by "
            "majority with their CRC checking",
            f"{votes.suspect:,}",
            f"{votes.tried:,}",
            vote_mod.VOTE_QUORUM,
            f"{len(voted):,}",
        )
    log.debug("merged to %d sectors; locating the header segment", len(merged))

    # The header places right under any geometry; then the header's own
    # geometry places everything else (floppy tracks per side). See
    # volume.locate_header, shared with the pipeline and `qicsilver identify`.
    located = header_mod.locate_header(merged, _FALLBACK)
    if located is None:
        log.debug(
            "locate_header found no header segment in %d merged sectors; refusing", len(merged)
        )
        if best is not merge.Best.SECTOR:
            # The first locate, over every pass, found one (else _select
            # would have raised): the reads chosen left it out.
            raise ValueError(
                f"no header segment in the reads --best={best.value} chose; "
                "try --best=sector, which keeps every read's sectors"
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
    report = MergeLog(
        reads,
        geom,
        origin=origin,
        votes=votes,
        voted_index=len(passes) if voted else None,
    )
    log.info("correcting %s segments (Reed-Solomon)...", f"{total:,}")
    with progress.task("correcting segments", total=total, unit="segments") as bar:
        for n in range(total):
            bar.advance()
            if n in bsm.bad_segments:
                n_bad += 1
                entries.append(SegmentEntry(SegmentState.BAD))
                report.segment(n, None, SegmentState.BAD)
                continue
            seg = by_seg.get(n)
            if seg is None:
                n_missing += 1
                mask = sum(1 << k for k in excluded.get(n, ()))
                entries.append(SegmentEntry(SegmentState.MISSING, excluded_mask=mask))
                report.segment(n, None, SegmentState.MISSING)
                continue
            if vote and seg_mod.classify(seg) is SegmentStatus.UNCORRECTABLE:
                vote_mod.rescue_segment(seg, suspects, stats=votes)
            # Before the correction, which writes into the sectors; verify
            # never does.
            parity = rs.verify(seg)
            # Also before it: a segment that was never written corrects into
            # garbage, and its fill is how the log tells it from damage.
            fill = format_fill(seg) if parity is rs.Parity.INCONSISTENT else None
            res = seg_mod.correct_segment(seg)
            mask = sum(1 << k for k in seg.excluded)
            data[n] = res.data
            state = _FROM_RS[res.status]
            entries.append(SegmentEntry(state, res.erasure_count, len(res.data), mask))
            report.segment(n, seg, state, parity=parity, erasures=res.erasure_count, fill=fill)
    report.finish()
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
        "segments: %s clean, %s corrected, %s uncorrectable, %s missing, %s bad (map); "
        "%s rescued by RS columns, %s with spare parity that disagrees, %s unwritten "
        "(format fill)",
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
        f"{report.totals['rescued']:,}",
        f"{report.totals['inconsistent']:,}",
        f"{report.totals['unwritten']:,}",
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


def _read_info(index: int, sectors: list[RawSector], source: dict) -> ReadInfo:
    """One read as the per-track log names it: its file name, track and sector counts.

    ``source`` is its provenance dict, which may lack any member (a test's,
    or one from a caller that has no TWRF header): the read is then
    "capture N" of an unknown track.
    """
    twrf = source.get("twrf") or {}
    return ReadInfo(
        name=Path(str(source.get("file", f"capture {index + 1}"))).name,
        track=int(twrf.get("track", -1)),
        sectors=len(sectors),
        clean=sum(1 for sec in sectors if sec.data_crc_ok),
    )


def _select(
    passes: list[list[RawSector]],
    sources: list[dict],
    best: merge.Best,
    rank: merge.Rank,
    progress: Progress,
) -> list[list[RawSector]]:
    """The reads :func:`qiclib.merge.select` keeps at ``best``, logged by capture name.

    Scoring needs the tape's geometry and bad-sector map, which come from its
    header, so the header is located first in the union of *every* pass,
    exactly as the plain build does. That locate runs on copies of the
    sectors: when the header's sector 0 is damaged it Reed-Solomon corrects
    the candidate segment, and the solve writes into the sectors it is given
    (:func:`qiclib.rs.correct`), which would leave one pass's header sectors
    repaired and CRC-good before the passes are compared.
    """
    log.info("choosing the best read of each %s (ranked by %s)", best.value, rank.value)
    everything = [copy_of(sec) for sec in merge.union(passes)]
    located = header_mod.locate_header(everything, _FALLBACK)
    if located is None:
        log.debug("no header segment in the union of all %d passes; refusing", len(passes))
        raise ValueError("no header segment in these captures")
    tracks = [int(src.get("twrf", {}).get("track", -1)) for src in sources]
    names = [str(src.get("file", f"capture {i + 1}")) for i, src in enumerate(sources)]
    stats = merge.SelectStats()
    chosen = merge.select(
        passes,
        best,
        tracks=tracks,
        geometry=located.geometry,
        bsm=located.bsm,
        rank=rank,
        progress=progress,
        stats=stats,
    )
    if best is merge.Best.TRACK:
        for track, i in sorted(stats.winners.items()):
            rivals = sum(1 for t in tracks if t == track) - 1
            if rivals:
                log.info("track %2d: using %s over %d other read(s)", track, names[i], rivals)
    else:
        for i, won in enumerate(stats.won):
            log.info("%s: supplies %s segments", names[i], f"{won:,}")
    log.info(
        "best by %s: %s contested, %s sectors of the losing reads left out",
        best.value,
        f"{stats.contested:,}",
        f"{stats.dropped:,}",
    )
    return chosen


def _plural(count: int, one: str, many: str) -> str:
    return f"{count:,} {one if count == 1 else many}"


def _mb(size: int) -> str:
    """A byte count as decimal megabytes, the unit rich's file-size bars use."""
    return f"{size / 1e6:,.1f} MB"


def _log_header(located: header_mod.LocatedHeader, total: int) -> None:
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

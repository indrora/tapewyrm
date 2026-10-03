"""Top-level decode pipeline (DESIGN.md §6.4, §6A.5, §13.5).

Wires the codec stages into ``decode(caps) -> (filesets, RecoveryReport)``:

    load + recover sectors from each cap   (flux.load -> mfm.recover_sectors)
      -> merge.union                       (multi-pass union, before RS)
      -> place                             ((fsd,ftk,fsc) -> segment bins)
      -> correct each segment              (RS erasure decode)
      -> parse_header                      (format params + BSM)
      -> volume_streams                    (per-file-set Volume Data Area bytes)
      -> qic113.extract per file set       (directory tree + files)

The Geometry is built from the capture header (segments_per_track / tracks /
tape_format).
"""

from __future__ import annotations

import logging

from qiclib import merge, qic113
from qiclib import segment as seg_mod
from qiclib import volume as volume_mod
from qiclib.geometry import Geometry, fallback_spt
from qiclib.types import FileSet, RawSector, SegmentStatus
from tapewyrm_archive.twrf import RawFluxCapture

from tapewyrm.codec import flux, mfm
from tapewyrm.types import RecoveryReport

log = logging.getLogger(__name__)


def _geometry_for(caps: list[RawFluxCapture]) -> Geometry:
    """Build a Geometry from the capture headers (DESIGN.md §7.1, §7.3)."""
    spt = 0
    tracks = 0
    for cap in caps:
        spt = spt or cap.header.segments_per_track
        tracks = tracks or cap.header.tracks
    if not spt:
        log.debug("no capture header gives segments_per_track; using fallback_spt(None)")
    spt = spt or fallback_spt(None)
    if not tracks:
        log.debug("no capture header gives tracks; defaulting to 28")
    tracks = tracks or 28
    log.debug("decode geometry: %d tracks x %d segments/track", tracks, spt)
    return Geometry(tracks=tracks, segments_per_track=spt)


def _recover_pass(cap: RawFluxCapture) -> list[RawSector]:
    """Load + recover sectors from one capture (flux -> intervals -> sectors)."""
    log.debug("loading flux from capture (%d bytes)", len(cap.flux))
    stream, _markers = flux.load(cap)
    log.debug("recovering sectors from %d intervals", len(stream.intervals))
    return list(mfm.recover_sectors(stream, cap.header.rate_kbps))


def decode(caps: list[RawFluxCapture]) -> tuple[list[FileSet], RecoveryReport]:
    """Full offline decode of one or more captures into file sets + a report."""
    log.info("decoding %d capture(s)", len(caps))
    geom = _geometry_for(caps)

    # 1. Recover sectors from every capture, then union across passes (before RS).
    per_pass = [_recover_pass(cap) for cap in caps]
    log.debug("recovered %r sectors per pass; merging", [len(p) for p in per_pass])
    merged = list(merge.union(per_pass))

    # 2-3. Place self-locating sectors into segment bins, find the header (the
    #      first defect-free segment), and re-place under its geometry with the
    #      bad-sector map applied. The map must land BEFORE RS -- an excluded
    #      sector is not part of the codeword (QIC-80-MC Rev N 6.2.5).
    report = RecoveryReport()
    log.debug("locating header segment among %d merged sectors", len(merged))
    located = volume_mod.locate_header(merged, geom)
    if located is None:
        log.debug("no header segment found in %d sectors; returning empty result", len(merged))
        report.notes.append("no header segment found; cannot reassemble volumes")
        return [], report
    vol, bsm, segs = located.vol, located.bsm, located.segs
    report.expected_bad = len(bsm.bad_segments) + len(bsm.bad_lsns)

    # 4. RS erasure-decode each segment; record per-segment status.
    log.debug("RS-correcting %d segments", len(segs))
    for key, seg in segs.items():
        result = seg_mod.correct_segment(seg)
        report.segment_status[key] = result.status
        if result.status is SegmentStatus.CORRECTED:
            report.segments_corrected[key] = result.corrected_count
        if result.status is SegmentStatus.UNCORRECTABLE:
            log.debug(
                "segment %r uncorrectable (%d erasures); queueing for recapture",
                key,
                result.erasure_count,
            )
            report.unexpected_bad += 1
            report.recapture.append(key)

    # 5. Per-file-set Volume Data Area byte streams.
    log.debug("assembling volume data streams")
    streams = volume_mod.volume_streams(segs, vol, bsm)

    # 6. QIC-113 extraction per file set.
    filesets: list[FileSet] = []
    for vtbl, byte_stream in streams:
        log.debug("extracting QIC-113 file set from %d-byte stream", len(byte_stream))
        filesets.append(qic113.extract(byte_stream, vtbl))

    return filesets, report

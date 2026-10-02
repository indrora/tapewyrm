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

from tapewyrm.codec import flux, merge, mfm, qic113
from tapewyrm.codec import segment as seg_mod
from tapewyrm.codec import volume as volume_mod
from tapewyrm.rawflux.container import RawFluxCapture
from tapewyrm.tape.geometry import Geometry, fallback_spt
from tapewyrm.types import (
    FileSet,
    RawSector,
    RecoveryReport,
    SegmentStatus,
)


def _geometry_for(caps: list[RawFluxCapture]) -> Geometry:
    """Build a Geometry from the capture headers (DESIGN.md §7.1, §7.3)."""
    spt = 0
    tracks = 0
    for cap in caps:
        spt = spt or cap.header.segments_per_track
        tracks = tracks or cap.header.tracks
    spt = spt or fallback_spt(None)
    tracks = tracks or 28
    return Geometry(tracks=tracks, segments_per_track=spt)


def _recover_pass(cap: RawFluxCapture) -> list[RawSector]:
    """Load + recover sectors from one capture (flux -> intervals -> sectors)."""
    stream, _markers = flux.load(cap)
    return list(mfm.recover_sectors(stream, cap.header.rate_kbps))


def decode(caps: list[RawFluxCapture]) -> tuple[list[FileSet], RecoveryReport]:
    """Full offline decode of one or more captures into file sets + a report."""
    geom = _geometry_for(caps)

    # 1. Recover sectors from every capture, then union across passes (before RS).
    per_pass = [_recover_pass(cap) for cap in caps]
    merged = list(merge.union(per_pass))

    # 2-3. Place self-locating sectors into segment bins, find the header (the
    #      first defect-free segment), and re-place under its geometry with the
    #      bad-sector map applied. The map must land BEFORE RS -- an excluded
    #      sector is not part of the codeword (QIC-80-MC Rev N 6.2.5).
    report = RecoveryReport()
    located = volume_mod.locate_header(merged, geom)
    if located is None:
        report.notes.append("no header segment found; cannot reassemble volumes")
        return [], report
    vol, bsm, segs = located.vol, located.bsm, located.segs
    report.expected_bad = len(bsm.bad_segments) + len(bsm.bad_lsns)

    # 4. RS erasure-decode each segment; record per-segment status.
    for key, seg in segs.items():
        result = seg_mod.correct_segment(seg)
        report.segment_status[key] = result.status
        if result.status is SegmentStatus.CORRECTED:
            report.segments_corrected[key] = result.corrected_count
        if result.status is SegmentStatus.UNCORRECTABLE:
            report.unexpected_bad += 1
            report.recapture.append(key)

    # 5. Per-file-set Volume Data Area byte streams.
    streams = volume_mod.volume_streams(segs, vol, bsm)

    # 6. QIC-113 extraction per file set.
    filesets: list[FileSet] = []
    for vtbl, byte_stream in streams:
        filesets.append(qic113.extract(byte_stream, vtbl))

    return filesets, report

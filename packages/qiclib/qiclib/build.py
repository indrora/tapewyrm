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
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from tapewyrm_archive.progress import NULL_PROGRESS, Progress
from tapewyrm_archive.twti import SEGMENT_STRIDE, VERSION, SegmentEntry, SegmentState, TapeImage

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
    merged = list(merge.union(passes))
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
                entries.append(SegmentEntry(SegmentState.MISSING))
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

    first: dict = next((m["twrf"] for m in source_meta if "drive_config" in m["twrf"]), {})
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
    if not first:
        log.debug("no source capture carries drive_config; drive identity left empty")
    img = TapeImage(header=header, entries=entries)
    log.info("writing %s...", out)
    img.save(out, lambda n: data.get(n, b""))
    log.info(f"wrote {out}: {total} segments {img.counts()}")
    return TapeImage.open(out)

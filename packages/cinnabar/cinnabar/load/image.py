"""A TWTI / TWTZ tape image (TWS-2) as tracks of segments on the byte axis.

An image has no time left in it: ``tw convert`` placed every recovered
sector by its ID field into a 29,696-byte slot per segment (TWS-2 section
6), and the segment table (section 5) says what became of each. So a track
here is ``segments_per_track`` slots laid end to end, segment ``n`` of the
track at byte ``29,696 n``, and the records lane shows the sector slots
with the table's verdict rather than any tape structure.

What a segment *holds* is decided the way ``qicsilver identify`` and
``extract`` decide it (:func:`qiclib.identify.from_image` through
:class:`cinnabar.load.volumes.Layout`).
"""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path

from qiclib import identify
from tapewyrm_archive import twti
from tapewyrm_archive.progress import NULL_PROGRESS, Progress
from tapewyrm_archive.twti import SegmentState, TapeImage

from cinnabar.annotate import sectors as sectors_mod
from cinnabar.load.volumes import Layout, volume_track
from cinnabar.model import (
    SEGMENT_BYTES,
    VOLUME_BASE,
    Axis,
    AxisKind,
    ByteMap,
    ByteRun,
    Segment,
    Tape,
    Track,
)

log = logging.getLogger(__name__)

_OVERVIEW = {
    SegmentState.CLEAN: "clean",
    SegmentState.CORRECTED: "corrected",
    SegmentState.UNCORRECTABLE: "uncorrectable",
    SegmentState.MISSING: "missing",
    SegmentState.BAD: "bad",
}


def open_image(path: Path, progress: Progress = NULL_PROGRESS) -> Tape:
    img = TapeImage.open(path, progress=progress)
    geometry = img.header.get("geometry", {})
    tracks = int(geometry.get("tracks", 0))
    spt = int(geometry.get("segments_per_track", 0))
    if tracks <= 0 or spt <= 0:
        # No geometry: show everything as one track (TWS-2 section 9.2 rule 5).
        tracks, spt = 1, len(img.entries)
    info = identify.from_image(img)
    present = [
        n
        for n, e in enumerate(img.entries)
        if e.state not in (SegmentState.MISSING, SegmentState.BAD) and e.data_len
    ]
    layout = Layout(
        info, img.segment, dup_header=img.qic80_segment("dup_header_seg", required=False)
    )
    kind = twti.sniff(path) or "TWTI"
    detail = (("file", path.name), ("format", kind), *layout.summary())
    if (tracks, spt) != (info.vol.tracks, info.vol.segments_per_track):
        # The slots are laid out by the image's own geometry member, which
        # tw convert may have calibrated past what the header says.
        detail += (("image geometry", f"{tracks} tracks x {spt} segments"),)

    @lru_cache(maxsize=4)
    def _load(track: int) -> Track:
        segments: list[Segment] = []
        overview: list[str] = []
        for tps in range(spt):
            seg_abs = track * spt + tps
            if seg_abs >= len(img.entries):
                break
            entry = img.entries[seg_abs]
            base = tps * SEGMENT_BYTES
            data = img.segment(seg_abs)
            bmap = ByteMap.identity(base, SEGMENT_BYTES)
            fields = layout.fields(seg_abs, data, bmap) if seg_abs in present else []
            holds = layout.holds(seg_abs)
            seg_detail = (
                ("segment", f"{seg_abs} (track {track}, {tps})"),
                ("state", entry.state.name),
                ("erasures", str(entry.erasures)),
                ("data bytes", f"{entry.data_len:,}"),
                ("excluded mask", f"0x{entry.excluded_mask:08x}"),
                ("holds", holds or "(unknown)"),
            )
            segments.append(
                Segment(
                    tps,
                    base,
                    base + SEGMENT_BYTES,
                    f"Segment {tps}",
                    sectors_mod.slot_spans(entry, base),
                    fields,
                    [ByteRun(base, base + len(data), data)] if data else [],
                    entry.state.name,
                    seg_detail,
                )
            )
            overview.append(_OVERVIEW[entry.state])
        return Track(
            f"Track {track}",
            track,
            Axis(AxisKind.BYTES),
            "QIC-80",
            segments,
            overview,
            (("track", str(track)), ("segments", str(len(segments)))),
        )

    labels = {
        VOLUME_BASE + k: f"Volume {k}: {v.entry.description.strip() or '(unnamed)'}"
        for k, v in enumerate(layout.volumes)
    }

    def load(track: int, progress: Progress = NULL_PROGRESS) -> Track:
        if track >= VOLUME_BASE:
            vol = layout.volumes[track - VOLUME_BASE]
            with progress.task(f"decompressing volume {track - VOLUME_BASE}", total=None):
                return volume_track(vol, track, labels[track])
        with progress.task(f"placing track {track}", total=None):
            return _load(track)

    numbers = list(range(tracks)) + sorted(labels)
    return Tape(str(path), kind, numbers, load, detail, labels, layout.warnings)

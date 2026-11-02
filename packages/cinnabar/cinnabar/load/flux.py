"""TWRF raw flux captures (TWS-1) as tracks on the time axis, decoded as you look.

One capture is one pass over one track, so one TWRF file is one track and
a directory of them (``tw dump``'s output) is a tape, tracks taken from
each header's ``track`` member.

Opening a track is quick: read the header, tokenize the stream
(:func:`tapewyrm_archive.twrf.parse_body`, a few seconds), cut the pass
into segments at the drive's INDEX pulses (TWS-1 section 5.2: one per
segment found during Logical Forward, the cue pulses in the first 50 ms
ignored; a capture with no pulses is cut into half-second pieces) and
decide the physical format from the first two seconds of flux
(:func:`cinnabar.decode.jobs.detect_physical`). The track comes back at
once with every segment a "decoding" placeholder and its flux lane
already drawable. Then :class:`cinnabar.decode.track.TrackDecoder` works
through the segments in worker processes, nearest the view first, and
:class:`FluxTrack` turns each result into the segment's lanes:

- QIC-80: sectors with their ID and data fields on the time axis, the
  segment's bytes assembled by slot (what an image would hold), and the
  logical lanes from :class:`cinnabar.load.volumes.Layout` once the tape
  has been identified from the sectors seen so far
  (:func:`qiclib.identify.from_sectors`: it needs the header segment,
  which track 0 has at its start, so track 0 is where the logical lanes
  fill first).
- QIC-100-MC: records with their preamble, markers, data, ID and CRCC
  pieces (:mod:`cinnabar.annotate.qic100`); the bytes are the data fields.

The window listens for landings through ``Track.decoder``
(:meth:`FluxTrack.on_segment`) and tells it where the user is looking
(:meth:`FluxTrack.focus`).
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Sequence
from pathlib import Path

import numpy as np
from qiclib import geometry as geometry_mod
from qiclib import identify
from qiclib.types import RawSector
from tapewyrm_archive.progress import NULL_PROGRESS, Progress
from tapewyrm_archive.twrf import header_to_dict, parse_body, read_header
from tapewyrm_archive.types import CaptureHeader

from cinnabar.annotate import qic100 as qic100_ann
from cinnabar.annotate import sectors as sectors_mod
from cinnabar.decode import jobs
from cinnabar.decode.track import TrackDecoder
from cinnabar.load.volumes import Layout, volume_track
from cinnabar.model import (
    PASS_BASE,
    SECTOR_BYTES,
    SEGMENT_BYTES,
    VOLUME_BASE,
    Axis,
    AxisKind,
    ByteMap,
    ByteRun,
    Flux,
    Segment,
    Shape,
    Span,
    Tape,
    Track,
)

log = logging.getLogger(__name__)

CUE_WINDOW_S = 0.050  # INDEX pulses before this are the drive's cue, not segments
DETECT_S = 2.0  # flux used to decide the physical format
PIECE_S = 0.5  # segment length when a capture has no INDEX pulses
IDENTIFY_AFTER = 6  # segments decoded before the first identification attempt

_PHYSICAL_NAMES = {"qic80": "QIC-80", "qic100": "QIC-100-MC"}


class _Identification:
    """One tape's identification, shared by its tracks, attempted as sectors arrive."""

    def __init__(self) -> None:
        self.layout: Layout | None = None
        self.ftk_per_side = geometry_mod.FTK_PER_SIDE
        self._tried_with = 0
        self._lock = threading.Lock()
        self.on_identified: list[Callable[[Layout], None]] = []

    def attempt(self, sectors: Sequence[RawSector], header: CaptureHeader, *, final: bool) -> bool:
        """Try to identify from ``sectors``; True when the layout is new."""
        with self._lock:
            if self.layout is not None:
                return False
            if not final and len(sectors) < IDENTIFY_AFTER * 32:
                return False
            if len(sectors) == self._tried_with and not final:
                return False
            self._tried_with = len(sectors)
            try:
                info = identify.from_sectors(sectors, drive=header_to_dict(header))
            except ValueError as exc:
                log.debug("identification: %s", exc)
                return False
            self.ftk_per_side = info.vol.max_ftk + 1 if info.vol.max_ftk else self.ftk_per_side
            segments: dict[int, bytearray] = {}
            for sec in sectors:
                if not sec.id_crc_ok:
                    continue
                seg_abs = geometry_mod.coord_to_seg(sec.fsd, sec.ftk, sec.fsc, self.ftk_per_side)
                slot = geometry_mod.sector_in_segment(sec.fsc)
                if slot < sectors_mod.DATA_SECTORS:
                    buf = segments.setdefault(seg_abs, bytearray(SEGMENT_BYTES))
                    buf[slot * SECTOR_BYTES : (slot + 1) * SECTOR_BYTES] = sec.data
            self.layout = Layout(info, segments.keys(), lambda s: bytes(segments.get(s, b"")))
            log.info(
                "tape identified: %s, %d volumes",
                info.vol.tape_name.strip(),
                len(self.layout.entries),
            )
        for listener in self.on_identified:
            listener(self.layout)
        return True


class FluxTrack:
    """One capture as a :class:`Track` that fills in as its segments decode."""

    def __init__(
        self, path: Path, number: int, ident: _Identification, progress: Progress = NULL_PROGRESS
    ) -> None:
        self.path = path
        self.number = number
        self.ident = ident
        hdr, flux_at = read_header(path)
        self.header = hdr
        with progress.task(f"reading {path.name}", total=None):
            blob = path.read_bytes()
        ps = parse_body(blob[flux_at:], progress=progress)
        del blob
        self.sample_hz = ps.sample_clock_hz
        intervals = jobs.intervals_array(ps.intervals)
        del ps.intervals[:]
        self.flux = Flux(intervals, self.sample_hz)
        duration = max(ps.span_s, self.flux.duration)
        log.info("%s: %.1f s of tape, %s transitions", path.name, duration, f"{len(intervals):,}")

        index_times = np.asarray(ps.index_ticks, dtype=np.float64) / self.sample_hz
        index_times = index_times[index_times >= CUE_WINDOW_S]
        if len(index_times):
            edges = [0.0, *index_times.tolist(), max(duration, float(index_times[-1]) + 1e-3)]
            titles = ["Lead-in"] + [f"Segment {k}" for k in range(len(index_times))]
        else:
            n = max(int(np.ceil(duration / PIECE_S)), 1)
            edges = [k * PIECE_S for k in range(n)] + [duration]
            titles = [f"Piece {k}" for k in range(n)]
        self.windows = [
            (edges[k], edges[k + 1]) for k in range(len(edges) - 1) if edges[k + 1] > edges[k]
        ]
        self.titles = titles[: len(self.windows)]

        probe = intervals[: self.flux.interval_index(DETECT_S)].tolist()
        with progress.task("detecting the physical format", total=None):
            self.physical, bitcell_s = jobs.detect_physical(probe, self.sample_hz, hdr.rate_kbps)
        log.info(
            "%s: %s, bitcell %.3f us", path.name, _PHYSICAL_NAMES[self.physical], bitcell_s * 1e6
        )

        self.sectors_by_segment: dict[int, list[RawSector]] = {}
        self._listeners: list[Callable[[int], None]] = []
        with progress.task(f"queueing {len(self.windows)} segments", total=None):
            self.decoder = TrackDecoder(
                intervals, self.sample_hz, self.physical, bitcell_s, self.windows
            )
        self.track = Track(
            f"Track {number}" if number < PASS_BASE else f"Pass {number - PASS_BASE}",
            number,
            Axis(AxisKind.TIME),
            _PHYSICAL_NAMES[self.physical],
            [self._placeholder(i) for i in range(len(self.windows))],
            ["pending"] * len(self.windows),
            (
                ("file", path.name),
                ("track", str(hdr.track)),
                ("direction", hdr.direction.value),
                ("rate", f"{hdr.rate_kbps} kbit/s"),
                ("duration", f"{duration:.1f} s"),
                ("physical", _PHYSICAL_NAMES[self.physical]),
                ("index pulses", str(len(index_times))),
            ),
            self.flux,
            self,
        )
        self.decoder.on_result(self._landed)
        with progress.task("starting decode workers", total=None):
            self.decoder.submit_all(0)

    # -- the Track.decoder protocol ------------------------------------------

    def on_segment(self, listener: Callable[[int], None]) -> None:
        self._listeners.append(listener)

    def focus(self, index: int) -> None:
        self.decoder.focus(index)

    def pending(self) -> int:
        return self.decoder.pending()

    def wait(self) -> None:
        self.decoder.wait()
        self._identify(final=True)

    def close(self) -> None:
        self.decoder.close()

    # -- segments ----------------------------------------------------------

    def _placeholder(self, i: int) -> Segment:
        start, end = self.windows[i]
        return Segment(
            i,
            start,
            end,
            self.titles[i],
            [Span(start, end, "decoding…", "", "pending", Shape.CHEVRON)],
            [],
            [],
            "decoding",
            (("segment", self.titles[i]), ("state", "decoding")),
        )

    def _landed(self, res: jobs.SegmentResult) -> None:
        self.flux.add_chunk(
            *self.windows[res.index], res.trace_times, res.trace_bits, res.trace_clock, res.bits
        )
        if res.physical == "qic100":
            self.track.segments[res.index] = self._qic100_segment(res)
        else:
            self.sectors_by_segment[res.index] = [
                RawSector(s.fsd, s.ftk, s.fsc, s.data, s.id_crc_ok, s.data_crc_ok, s.deleted)
                for s in res.sectors
            ]
            self.track.segments[res.index] = self._qic80_segment(res)
            if self._identify(final=False):
                self._reannotate()
        self.track.overview[res.index] = self.track.segments[res.index].state_token()
        for listener in self._listeners:
            listener(res.index)

    def _identify(self, *, final: bool) -> bool:
        if self.physical != "qic80" or self.ident.layout is not None:
            return False
        sectors = [s for secs in self.sectors_by_segment.values() for s in secs]
        if self.ident.attempt(sectors, self.header, final=final):
            self._reannotate()
            return True
        return False

    def _reannotate(self) -> None:
        """The tape just got identified: fill every decoded segment's logical lanes."""
        for i, seg in enumerate(self.track.segments):
            if seg.state != "decoding" and (res := self.decoder.result(i)) is not None:
                self.track.segments[i] = self._qic80_segment(res)
        for listener in self._listeners:
            listener(-1)

    def _qic80_segment(self, res: jobs.SegmentResult) -> Segment:
        start, end = self.windows[res.index]
        secs = self.sectors_by_segment.get(res.index, [])
        positions = [(s.id_start, s.id_end, s.data_start, s.data_end) for s in res.sectors]
        ftk = self.ident.ftk_per_side
        votes: dict[int, int] = {}
        for sec in secs:
            if sec.id_crc_ok:
                seg_abs = geometry_mod.coord_to_seg(sec.fsd, sec.ftk, sec.fsc, ftk)
                votes[seg_abs] = votes.get(seg_abs, 0) + 1
        seg_abs = max(votes, key=lambda s: votes[s]) if votes else None
        data = bytearray(SEGMENT_BYTES)
        runs: list[tuple[int, int, float, float]] = []
        byte_runs: list[ByteRun] = []
        good = 0
        for sec, (_i0, _i1, d0, d1) in zip(secs, positions, strict=True):
            if seg_abs is None or not sec.id_crc_ok:
                continue
            if geometry_mod.coord_to_seg(sec.fsd, sec.ftk, sec.fsc, ftk) != seg_abs:
                continue
            if sec.data_crc_ok:
                good += 1
            slot = geometry_mod.sector_in_segment(sec.fsc)
            if slot >= sectors_mod.DATA_SECTORS:
                continue
            off = slot * SECTOR_BYTES
            data[off : off + SECTOR_BYTES] = sec.data
            field0 = d0 + (d1 - d0) * 4 / jobs.DATA_FIELD_BYTES  # past sync + mark
            field1 = d1 - (d1 - d0) * 2 / jobs.DATA_FIELD_BYTES  # before the CRC
            runs.append((off, SECTOR_BYTES, field0, field1))
            byte_runs.append(ByteRun(field0, field1, sec.data, off))
        layout = self.ident.layout
        holds = layout.holds(seg_abs) if (layout is not None and seg_abs is not None) else ""
        fields: list[Span] = []
        if layout is not None and seg_abs is not None and runs:
            fields = layout.fields(seg_abs, bytes(data), ByteMap(runs))
        if res.error:
            state = "error"
        elif not secs:
            state = "empty"
        elif good == 32:
            state = "clean"
        else:
            state = "partial"
        detail = (
            ("segment", self.titles[res.index]),
            ("logical segment", str(seg_abs) if seg_abs is not None else "(no readable ID)"),
            ("sectors read", str(len(secs))),
            ("data CRC good", str(good)),
            ("holds", holds or "(unknown)"),
        ) + ((("error", res.error),) if res.error else ())
        return Segment(
            res.index,
            start,
            end,
            self.titles[res.index],
            sectors_mod.sector_spans(secs, positions, [holds] * len(secs)),
            fields,
            sorted(byte_runs, key=lambda r: r.start),
            state,
            detail,
        )

    def _qic100_segment(self, res: jobs.SegmentResult) -> Segment:
        start, end = self.windows[res.index]
        records = qic100_ann.record_spans(res.records)
        byte_runs = [
            ByteRun(*r.data, r.data_field)
            for r in res.records
            if r.data is not None and r.data_field
        ]
        ok = sum(1 for r in res.records if r.crcc_ok)
        state = (
            "error"
            if res.error
            else (
                "empty" if not res.records else ("clean" if ok == len(res.records) else "partial")
            )
        )
        frames = sorted({r.frame for r in res.records if r.frame is not None})
        detail = (
            ("segment", self.titles[res.index]),
            ("records", str(len(res.records))),
            ("crcc good", str(ok)),
            ("frames", ", ".join(map(str, frames)) if frames else "(none)"),
        ) + ((("error", res.error),) if res.error else ())
        return Segment(
            res.index, start, end, self.titles[res.index], records, [], byte_runs, state, detail
        )


def open_captures(paths: Sequence[Path]) -> Tape:
    if not paths:
        raise ValueError("no TWRF captures to open")
    headers = {p: read_header(p)[0] for p in paths}
    by_track: dict[int, Path] = {}
    for i, p in enumerate(paths):
        t = headers[p].track
        by_track[t if t >= 0 and t not in by_track else PASS_BASE + i] = p
    numbers = sorted(by_track)
    first = headers[by_track[numbers[0]]]
    source = str(paths[0].parent if len(paths) > 1 else paths[0])
    ident = _Identification()
    open_tracks: dict[int, FluxTrack] = {}
    labels: dict[int, str] = {}

    def volumes_known(layout: Layout) -> None:
        for k, vol in enumerate(layout.volumes):
            number = VOLUME_BASE + k
            labels[number] = f"Volume {k}: {vol.entry.description.strip() or '(unnamed)'}"
            if number not in tape.track_numbers:
                tape.track_numbers.append(number)
        tape.labels.update(labels)

    ident.on_identified.append(volumes_known)

    def load(number: int, progress: Progress = NULL_PROGRESS) -> Track:
        if number >= VOLUME_BASE:
            if ident.layout is None:
                raise ValueError("the tape has not been identified yet; decode track 0 first")
            vol = ident.layout.volumes[number - VOLUME_BASE]
            with progress.task(f"decompressing volume {number - VOLUME_BASE}", total=None):
                return volume_track(vol, number, labels[number])
        if number not in open_tracks:
            if len(open_tracks) >= 2:  # a track's intervals are big; keep two
                oldest = next(iter(open_tracks))
                open_tracks.pop(oldest).close()
            open_tracks[number] = FluxTrack(by_track[number], number, ident, progress)
        return open_tracks[number].track

    tape_detail = (
        ("source", source),
        ("captures", str(len(paths))),
        ("drive", f"vendor {first.drive_vendor_id}" if first.drive_vendor_id else "(unknown)"),
        ("tape format", first.tape_format.name),
    )
    tape = Tape(source, "TWRF", numbers, load, tape_detail)
    return tape


def open_directory(path: Path) -> Tape:
    return open_captures(sorted(p for p in path.iterdir() if p.suffix == ".twrf"))

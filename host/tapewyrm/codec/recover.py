"""Dump directory -> the volume's byte stream, holes and all.

Takes the ``track-NN.raw`` files written by ``tw dump`` and runs the whole
decode: GW stream -> PLL -> sectors -> segments placed with the header's
geometry -> bad-sector map -> Reed-Solomon -> QIC-113/122 extents. Each
extent knows its uncompressed offset, so the volume is assembled by offset
into a :class:`SparseVolume`: lost segments become known holes instead of
silently shifting everything after them.
"""

from __future__ import annotations

import bisect
import pickle
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from tapewyrm.codec import gwstream, mfm, place, qic122
from tapewyrm.codec import segment as seg_mod
from tapewyrm.codec import volume as volume_mod
from tapewyrm.tape.geometry import Geometry
from tapewyrm.types import RawSector, SegmentStatus

SECTOR_CACHE = "sectors.pkl"


@dataclass
class SparseVolume:
    """Byte ranges of a volume; reads report whether they were complete."""

    size: int
    _starts: list[int] = field(default_factory=list)
    _chunks: list[bytes] = field(default_factory=list)

    def add(self, offset: int, data: bytes) -> None:
        i = bisect.bisect_left(self._starts, offset)
        self._starts.insert(i, offset)
        self._chunks.insert(i, data)

    def read(self, offset: int, length: int) -> tuple[bytes, int]:
        """Bytes [offset, offset+length) with holes zero-filled, and how many
        of them are missing."""
        out = bytearray(length)
        have = 0
        i = max(0, bisect.bisect_right(self._starts, offset) - 1)
        end = offset + length
        while i < len(self._starts) and self._starts[i] < end:
            s, c = self._starts[i], self._chunks[i]
            a, b = max(s, offset), min(s + len(c), end)
            if a < b:
                out[a - offset : b - offset] = c[a - s : b - s]
                have += b - a
            i += 1
        return bytes(out), length - have

    def coverage(self) -> int:
        """Bytes of [0, size) present."""
        return sum(
            min(len(c), max(0, self.size - s))
            for s, c in zip(self._starts, self._chunks, strict=True)
        )


@dataclass
class RecoveredVolume:
    header: volume_mod.VolumeInfo
    vtbl: volume_mod.VtblEntry
    stream: SparseVolume  # data section + directory section, by uncompressed offset
    missing_segments: list[int]
    uncorrectable_segments: list[int]
    corrected_segments: int


def load_sectors(dump_dir: Path, log: Callable[[str], None] = print) -> list[RawSector]:
    """Decode every track capture to sectors (cached in ``sectors.pkl``)."""
    cache = dump_dir / SECTOR_CACHE
    tracks = sorted(dump_dir.glob("track-*.raw"))
    if cache.exists() and all(cache.stat().st_mtime >= t.stat().st_mtime for t in tracks):
        sectors: list[RawSector] = pickle.loads(cache.read_bytes())
        return sectors
    sectors = []
    for t in tracks:
        ps = gwstream.parse(t.read_bytes())
        got = mfm.recover_sectors_from_flux(ps.intervals, ps.sample_clock_hz, 500)
        log(f"{t.name}: {len(got)} sectors")
        sectors += got
    cache.write_bytes(pickle.dumps(sectors))
    return sectors


def recover(dump_dir: Path, log: Callable[[str], None] = print) -> RecoveredVolume:
    """Decode a dump directory into the first volume's byte stream."""
    sectors = load_sectors(dump_dir, log)
    # The header (segment 0, side 0) places right under any geometry; then the
    # header's own geometry places everything else (floppy tracks per side!).
    segs = place.place(sectors, Geometry(tracks=28, segments_per_track=207))
    header = next((s for s in segs.values() if s.seg == 0), None)
    if header is None:
        raise ValueError("no header segment in this dump")
    vol, bsm = volume_mod.parse_header(header)
    geom = Geometry(vol.tracks, vol.segments_per_track, ftk_per_side=vol.max_ftk + 1)
    segs = place.place(sectors, geom)
    volume_mod.apply_bsm(segs, bsm)
    by_seg = {s.seg: s for s in segs.values()}

    vt_seg = by_seg.get(vol.first_data_seg)
    if vt_seg is None:
        raise ValueError(f"volume table segment {vol.first_data_seg} not recovered")
    vt_entries = volume_mod.parse_volume_table(vt_seg)
    if not vt_entries:
        raise ValueError("volume table is empty")
    vt = vt_entries[0]

    size = (vt.data_section_size or 0) + (vt.dir_section_size or 0)
    stream = SparseVolume(size=size)
    missing, uncorrectable, corrected = [], [], 0
    for n in range(vt.start_seg, vt.end_seg + 1):
        if n in bsm.bad_segments:
            continue
        seg = by_seg.get(n)
        if seg is None:
            missing.append(n)
            continue
        res = seg_mod.correct_segment(seg)
        if res.status is SegmentStatus.UNCORRECTABLE:
            uncorrectable.append(n)
            continue
        corrected += res.status is SegmentStatus.CORRECTED
        try:
            ext = qic122.decode_extent(res.data)
        except qic122.Qic122Error:
            uncorrectable.append(n)
            continue
        stream.add(ext.uncompressed_offset, ext.data)
    return RecoveredVolume(vol, vt, stream, missing, uncorrectable, corrected)

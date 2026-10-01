"""TWTI: the logical tape image (`tw convert` output).

Step two of ``tw dump -> tw convert -> tw extract``. A TWTI file is the tape
after the QIC-80 layer is done with it: every sector placed by its own address
using the header segment's geometry, the bad-sector map applied, every segment
Reed-Solomon corrected, in segment order -- plus how sure we are of each one.
It knows nothing about backup formats; that is ``tw extract``'s job.

Layout (little endian)::

    "TWTI"  u16 version  u32 header_len  header (JSON, UTF-8)
    segment table: segment_count x 8 bytes
        u8  status          (SegmentState: missing/clean/corrected/uncorrectable/bad)
        u8  erasures        sectors RS had to rebuild (or couldn't)
        u16 data_len        bytes of real data in this segment's slot
        u32 excluded_mask   bit k = sector k excluded by the bad-sector map
    segment data: segment_count x SEGMENT_STRIDE bytes (29 KB each, zero-padded)

The fixed stride gives random access by segment number. A segment's data is
its corrected data rows (29 sectors, fewer when the bad-sector map excludes
some); missing segments are all zero. The JSON header carries the drive
identity and the source TWRF headers, so provenance survives every step.
"""

from __future__ import annotations

import json
import mmap
import struct
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import IntEnum
from pathlib import Path

from tapewyrm.codec import gwstream, merge, mfm, place
from tapewyrm.codec import segment as seg_mod
from tapewyrm.codec import volume as volume_mod
from tapewyrm.rawflux.container import header_to_dict, read_header
from tapewyrm.tape.geometry import Geometry
from tapewyrm.types import RawSector, SegmentStatus

MAGIC = b"TWTI"
VERSION = 1
SEGMENT_STRIDE = 29 * 1024
_PREAMBLE = struct.Struct("<4sHI")
_ENTRY = struct.Struct("<BBHI")
# Bare device streams from before TWRF carry no header; they were all QIC-80.
LEGACY_RAW_RATE_KBPS = 500


class SegmentState(IntEnum):
    MISSING = 0  # no sector of it was read
    CLEAN = 1  # every sector CRC-good
    CORRECTED = 2  # Reed-Solomon rebuilt 1-3 sectors
    UNCORRECTABLE = 3  # > 3 sectors bad or missing: partial data kept
    BAD = 4  # the bad-sector map marks the whole segment unusable


_FROM_RS = {
    SegmentStatus.CLEAN: SegmentState.CLEAN,
    SegmentStatus.CORRECTED: SegmentState.CORRECTED,
    SegmentStatus.UNCORRECTABLE: SegmentState.UNCORRECTABLE,
    SegmentStatus.MISSING: SegmentState.MISSING,
}


@dataclass(frozen=True)
class SegmentEntry:
    state: SegmentState
    erasures: int = 0
    data_len: int = 0
    excluded_mask: int = 0


@dataclass
class TapeImage:
    header: dict
    entries: list[SegmentEntry] = field(default_factory=list)
    _data: bytes | mmap.mmap = b""
    _data_at: int = 0

    def segment(self, n: int) -> bytes:
        """Segment ``n``'s data (``data_len`` bytes; empty if missing)."""
        e = self.entries[n]
        start = self._data_at + n * SEGMENT_STRIDE
        return bytes(self._data[start : start + e.data_len])

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for e in self.entries:
            out[e.state.name] = out.get(e.state.name, 0) + 1
        return out

    # --- persistence ---

    def save(self, path: Path, segment_data: Callable[[int], bytes]) -> None:
        """Write the image; ``segment_data(n)`` supplies each segment's bytes."""
        hdr = json.dumps(self.header, indent=1).encode("utf-8")
        with path.open("wb") as f:
            f.write(_PREAMBLE.pack(MAGIC, VERSION, len(hdr)))
            f.write(hdr)
            for e in self.entries:
                f.write(_ENTRY.pack(e.state, e.erasures, e.data_len, e.excluded_mask))
            for n in range(len(self.entries)):
                f.write(segment_data(n).ljust(SEGMENT_STRIDE, b"\x00"))

    @classmethod
    def open(cls, path: Path) -> TapeImage:
        f = path.open("rb")
        mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
        magic, version, hlen = _PREAMBLE.unpack_from(mm, 0)
        if magic != MAGIC or version != VERSION:
            raise ValueError(f"{path}: not a TWTI v{VERSION} tape image")
        header = json.loads(mm[_PREAMBLE.size : _PREAMBLE.size + hlen])
        at = _PREAMBLE.size + hlen
        count = header["segment_count"]
        entries = [
            SegmentEntry(SegmentState(s), e, n, m)
            for s, e, n, m in _ENTRY.iter_unpack(mm[at : at + count * _ENTRY.size])
        ]
        return cls(header=header, entries=entries, _data=mm, _data_at=at + count * _ENTRY.size)


# ---------------------------------------------------------------------------
# tw convert
# ---------------------------------------------------------------------------


def capture_files(sources: Iterable[Path]) -> list[Path]:
    """Expand dump directories into their track captures (TWRF, else legacy .raw)."""
    out: list[Path] = []
    for src in sources:
        if src.is_dir():
            out += sorted(src.glob("track-*.twrf")) or sorted(src.glob("track-*.raw"))
        else:
            out.append(src)
    return out


def _decode_capture(path: Path) -> tuple[list[RawSector], dict]:
    blob = path.read_bytes()
    if path.suffix == ".twrf":
        hdr, flux_at = read_header(path)
        flux, rate, meta = blob[flux_at:], hdr.rate_kbps, header_to_dict(hdr)
    else:
        flux, rate, meta = blob, LEGACY_RAW_RATE_KBPS, {"rate_kbps": LEGACY_RAW_RATE_KBPS}
    ps = gwstream.parse(flux)
    sectors = mfm.recover_sectors_from_flux(ps.intervals, ps.sample_clock_hz, rate)
    return sectors, {
        "file": str(path),
        "verified": ps.verified,
        "sectors": len(sectors),
        "twrf": meta,
    }


def convert(sources: Iterable[Path], out: Path, *, log: Callable[[str], None] = print) -> TapeImage:
    """Build a TWTI image from one or more dumps (passes are merged)."""
    from tapewyrm.buildinfo import host_build

    files = capture_files(sources)
    if not files:
        raise ValueError("no track captures found")
    passes, source_meta = [], []
    for path in files:
        sectors, meta = _decode_capture(path)
        log(f"{path.name}: {meta['sectors']} sectors at {meta['twrf']['rate_kbps']} kbps")
        passes.append(sectors)
        source_meta.append(meta)
    merged = list(merge.union(passes))

    # The header (segment 0, side 0) places right under any geometry; then the
    # header's own geometry places everything else (floppy tracks per side).
    segs = place.place(merged, Geometry(tracks=28, segments_per_track=207))
    header_seg = next((s for s in segs.values() if s.seg == 0), None)
    if header_seg is None:
        raise ValueError("no header segment (segment 0) in these captures")
    vol, bsm = volume_mod.parse_header(header_seg)
    geom = Geometry(vol.tracks, vol.segments_per_track, ftk_per_side=vol.max_ftk + 1)
    segs = place.place(merged, geom)
    volume_mod.apply_bsm(segs, bsm)
    by_seg = {s.seg: s for s in segs.values()}

    total = geom.total_segments()
    entries: list[SegmentEntry] = []
    data: dict[int, bytes] = {}
    for n in range(total):
        if n in bsm.bad_segments:
            entries.append(SegmentEntry(SegmentState.BAD))
            continue
        seg = by_seg.get(n)
        if seg is None:
            entries.append(SegmentEntry(SegmentState.MISSING))
            continue
        res = seg_mod.correct_segment(seg)
        mask = sum(1 << k for k in seg.excluded)
        data[n] = res.data
        entries.append(SegmentEntry(_FROM_RS[res.status], res.erasure_count, len(res.data), mask))

    first: dict = next((m["twrf"] for m in source_meta if "drive_config" in m["twrf"]), {})
    header = {
        "format": "TWTI",
        "version": VERSION,
        "created": datetime.now(UTC).isoformat(timespec="seconds"),
        "tw_commit": host_build().commit,
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
    img.save(out, lambda n: data.get(n, b""))
    log(f"wrote {out}: {total} segments {img.counts()}")
    return TapeImage.open(out)

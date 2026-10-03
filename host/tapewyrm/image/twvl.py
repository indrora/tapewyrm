"""TWVL: one extracted backup volume (`tw extract` output).

Step three of ``tw dump -> tw convert -> tw extract``. Extraction is the
QIC-113 layer: read the volume table from a TWTI tape image, decompress each
volume's segments (QIC-122 extents, QIC-113 section 9) and lay the bytes out
by their uncompressed offsets. Segments that couldn't be read leave holes,
which are recorded rather than silently shifting what follows.

Layout::

    "TWVL"  u16 version  u32 header_len  header (JSON)  volume bytes

The header carries the volume-table entry (description, date, flags, section
sizes, the raw 128-byte record), the tape's identity, the byte ranges that are
missing, and the source image. The volume bytes are the File Set Data Section
followed by the File Set Directory Section (QIC-113 "directory last" order),
which is what ``contrib/qic2tar.py`` turns into a tar.
"""

from __future__ import annotations

import bisect
import json
import logging
import struct
from dataclasses import asdict, dataclass, field
from pathlib import Path

from tapewyrm.codec import qic122
from tapewyrm.codec import volume as volume_mod
from tapewyrm.image.twti import SegmentState, TapeImage
from tapewyrm.progress import NULL_PROGRESS, Progress

log = logging.getLogger(__name__)

MAGIC = b"TWVL"
VERSION = 1
_PREAMBLE = struct.Struct("<4sHI")


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
class Volume:
    header: dict
    data: bytes

    @property
    def holes(self) -> list[tuple[int, int]]:
        return [(a, b) for a, b in self.header["holes"]]

    def read(self, offset: int, length: int) -> tuple[bytes, int]:
        """Bytes and how many of them fall in holes (zero-filled)."""
        end = offset + length
        missing = sum(max(0, min(b, end) - max(a, offset)) for a, b in self.holes)
        return self.data[offset:end].ljust(length, b"\x00"), missing

    def save(self, path: Path) -> None:
        hdr = json.dumps(self.header, indent=1).encode("utf-8")
        log.debug(
            "writing TWVL volume %s: %d-byte header, %d data bytes", path, len(hdr), len(self.data)
        )
        with path.open("wb") as f:
            f.write(_PREAMBLE.pack(MAGIC, VERSION, len(hdr)))
            f.write(hdr)
            f.write(self.data)

    @classmethod
    def load(cls, path: Path) -> Volume:
        log.debug("loading TWVL volume %s", path)
        blob = path.read_bytes()
        magic, version, hlen = _PREAMBLE.unpack_from(blob, 0)
        if magic != MAGIC or version != VERSION:
            log.debug(
                "%s: magic %r version %d != %r version %d; refusing",
                path,
                magic,
                version,
                MAGIC,
                VERSION,
            )
            raise ValueError(f"{path}: not a TWVL v{VERSION} volume")
        at = _PREAMBLE.size + hlen
        return cls(header=json.loads(blob[_PREAMBLE.size : at]), data=blob[at:])


def _vtbl_dict(e: volume_mod.VtblEntry) -> dict:
    d = asdict(e)
    d["signature"] = e.signature.decode("ascii", "replace")
    d["raw"] = e.raw.hex()
    d["date_decoded"] = volume_mod.decode_short_date(e.date)
    return d


def extract(image_path: Path, out_dir: Path, *, progress: Progress = NULL_PROGRESS) -> list[Path]:
    """Write every volume on the tape image as ``vol-NN.twvl`` in ``out_dir``."""
    log.info("extracting volumes from %s...", image_path.name)
    img = TapeImage.open(image_path)
    q80 = img.header["qic80_header"]
    vt_seg = q80["first_data_seg"]
    vt_entry = img.entries[vt_seg]
    log.debug(
        "volume table at segment %d (first_data_seg): state %s, %d erasures, data_len %d",
        vt_seg,
        vt_entry.state.name,
        vt_entry.erasures,
        vt_entry.data_len,
    )
    if vt_entry.state in (SegmentState.MISSING, SegmentState.BAD):
        log.debug("volume table segment %d is %s; refusing", vt_seg, vt_entry.state.name)
        raise ValueError(f"volume table segment {vt_seg} was not recovered")
    if vt_entry.state is SegmentState.UNCORRECTABLE:
        # Not refused (behaviour unchanged), but the table may be garbage.
        log.debug("volume table segment %d is UNCORRECTABLE; parsing partial data anyway", vt_seg)
    log.debug("parsing volume table from segment %d", vt_seg)
    vtbl = volume_mod.parse_volume_table_data(img.segment(vt_seg))
    log.debug("volume table: %d entries", len(vtbl))
    if not vtbl:
        log.debug("volume table segment %d parsed to no entries; nothing to extract", vt_seg)
    log.debug("creating output directory %s", out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    with progress.task("extracting volumes", total=len(vtbl), unit="volumes") as overall:
        for k, e in enumerate(vtbl):
            size = (e.data_section_size or 0) + (e.dir_section_size or 0)
            log.debug(
                "volume %d %r: segments %d..%d, compressed=%r, data_section_size=%r, "
                "dir_section_size=%r -> %d bytes",
                k,
                e.description,
                e.start_seg,
                e.end_seg,
                e.compressed,
                e.data_section_size,
                e.dir_section_size,
                size,
            )
            if e.end_seg < e.start_seg:
                log.debug(
                    "volume %d: end_seg %d < start_seg %d; no segments will be read",
                    k,
                    e.end_seg,
                    e.start_seg,
                )
            elif e.end_seg >= len(img.entries):
                log.debug(
                    "volume %d: end_seg %d is past the image's %d segments; "
                    "extraction will fail with IndexError",
                    k,
                    e.end_seg,
                    len(img.entries),
                )
            if size == 0:
                log.debug(
                    "volume %d: section sizes give 0 bytes (missing/None in the table?); "
                    "volume body will be empty",
                    k,
                )
            if e.compressed is None:
                log.debug(
                    "volume %d: compressed flag is None; treating segments as QIC-122 extents", k
                )
            stream = SparseVolume(size=size)
            lost: list[int] = []
            n_bad = 0
            with progress.task(
                f"volume {k}", total=e.end_seg + 1 - e.start_seg, unit="segments"
            ) as bar:
                for n in range(e.start_seg, e.end_seg + 1):
                    bar.advance()
                    st = img.entries[n].state
                    if st is SegmentState.BAD:
                        log.debug("segment %d (volume %d): BAD per bad-sector map; skipping", n, k)
                        n_bad += 1
                        continue
                    if st in (SegmentState.MISSING, SegmentState.UNCORRECTABLE):
                        log.debug("segment %d (volume %d): state %s; marking lost", n, k, st.name)
                        lost.append(n)
                        continue
                    data = img.segment(n)
                    if e.compressed is False:
                        # Uncompressed volume: segments are laid end to end.
                        stream.add((n - e.start_seg) * len(data), data)
                        continue
                    try:
                        ext = qic122.decode_extent(data)
                    except qic122.Qic122Error as exc:
                        log.debug(
                            "segment %d (volume %d): QIC-122 decode failed (state %s, "
                            "%d bytes): %s; marking lost",
                            n,
                            k,
                            st.name,
                            len(data),
                            exc,
                        )
                        lost.append(n)
                        continue
                    if ext.uncompressed_offset + len(ext.data) > size:
                        log.debug(
                            "segment %d (volume %d): extent at offset %d + %d bytes runs "
                            "past volume size %d; bytes beyond it are dropped",
                            n,
                            k,
                            ext.uncompressed_offset,
                            len(ext.data),
                            size,
                        )
                    stream.add(ext.uncompressed_offset, ext.data)
            log.debug(
                "volume %d: %d segments skipped (BAD), %d lost, %d of %d bytes covered",
                k,
                n_bad,
                len(lost),
                stream.coverage(),
                size,
            )
            body, _ = stream.read(0, size)
            holes = _holes(stream, size)
            vol = Volume(
                header={
                    "format": "TWVL",
                    "version": VERSION,
                    "volume_index": k,
                    "tape_name": q80["tape_name"],
                    "vtbl": _vtbl_dict(e),
                    "data_section_size": e.data_section_size,
                    "dir_section_size": e.dir_section_size,
                    "holes": holes,
                    "lost_segments": lost,
                    "source_image": str(image_path),
                    "drive": img.header.get("drive"),
                },
                data=body,
            )
            path = out_dir / f"vol-{k:02d}.twvl"
            log.debug("volume %d: %d holes; saving to %s", k, len(holes), path)
            vol.save(path)
            missing = sum(b - a for a, b in holes)
            log.info(
                f"{path}: {e.description!r}, {size:,} bytes, {missing:,} missing "
                f"({len(lost)} segments lost)"
            )
            written.append(path)
            overall.advance()
    return written


def _holes(stream: SparseVolume, size: int) -> list[list[int]]:
    """[start, end) ranges of ``stream`` not covered by any extent."""
    holes: list[list[int]] = []
    pos = 0
    for s, c in sorted(zip(stream._starts, stream._chunks, strict=True)):
        if s > pos:
            holes.append([pos, min(s, size)])
        pos = max(pos, s + len(c))
    if pos < size:
        holes.append([pos, size])
    return [h for h in holes if h[0] < h[1]]

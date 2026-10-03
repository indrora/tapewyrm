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

This module is the file format only; extraction from a TWTI image lives
in ``tapewyrm.image.extract`` (tapewyrm-host) for now.
"""

from __future__ import annotations

import bisect
import json
import logging
import struct
from dataclasses import dataclass, field
from pathlib import Path

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


def find_holes(stream: SparseVolume, size: int) -> list[list[int]]:
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

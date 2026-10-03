"""TWVL: one extracted backup volume (`qicsilver extract` output).

Step three of ``tw dump -> tw convert -> qicsilver extract``. Extraction is the
QIC-113 layer: read the volume table from a TWTI tape image, decompress each
volume's segments (QIC-122 extents, QIC-113 section 9) and lay the bytes out
by their uncompressed offsets. Segments that couldn't be read leave holes,
which are recorded rather than silently shifting what follows.

Layout::

    "TWVL"  u16 version  u32 header_len  header (JSON)  volume bytes

The header carries the volume-table entry (description, date, flags, section
sizes, the raw 128-byte record), the tape's identity, the volume's size, the
byte ranges that are missing, and the source image (as ``dir/name`` only,
``tapewyrm_archive.provenance``). The volume bytes are the File Set Data Section
followed by the File Set Directory Section (QIC-113 "directory last" order),
which is what ``qicsilver tar`` turns into a tar.

This module is the file format only; extraction from a TWTI image lives
in ``qiclib.extract``.

:meth:`Volume.load` refuses a file cut short (TWS-3 section 6.2) with
``tapewyrm_archive.errors.TruncatedFileError``: the preamble, the header, or
the volume bytes, judged against the header's ``volume_size`` (or, in files
written before that member existed, against the furthest hole end and
``directory_offset``, which a writer never puts past the end).
"""

from __future__ import annotations

import bisect
import json
import logging
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tapewyrm_archive.errors import MalformedFileError, TruncatedFileError

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
        of them are missing.

        Extents can overlap (QIC-122 extents at the same offset from two
        segments, or a misread table). Where they do, the later-starting one
        supplies the bytes, and each byte is counted as present once: the
        count used to add every extent's length, so overlaps pushed the
        missing count down, even below zero.
        """
        out = bytearray(length)
        end = offset + length
        have = 0
        # Bytes of [offset, covered_to) are already counted as present. The
        # extents are sorted by start, so each clipped range starts at or
        # after the previous one and only its part past covered_to is new.
        covered_to = offset
        # Scan from the first extent, not from a bisect on ``offset``: an
        # extent that starts well before ``offset`` can still reach into the
        # range past a shorter one that starts later.
        for s, c in zip(self._starts, self._chunks, strict=True):
            if s >= end:
                break
            a, b = max(s, offset), min(s + len(c), end)
            if a < b:
                out[a - offset : b - offset] = c[a - s : b - s]
                have += max(0, b - max(a, covered_to))
                covered_to = max(covered_to, b)
        return bytes(out), length - have

    def coverage(self) -> int:
        """Bytes of [0, size) present (overlapping extents counted once)."""
        return self.size - sum(b - a for a, b in find_holes(self, self.size))


@dataclass
class Volume:
    header: dict
    data: bytes

    @property
    def holes(self) -> list[tuple[int, int]]:
        return [(a, b) for a, b in self.header["holes"]]

    def read(self, offset: int, length: int) -> tuple[bytes, int]:
        """Bytes and how many of them are missing (zero-filled).

        Missing means in a hole (TWS-3 6.2 rule 5) or past the end of the
        volume bytes (rule 6). The ranges are merged before counting, so
        overlapping holes -- or a hole that runs past the end -- count each
        byte once (section 8.1: ``holes`` is not trusted to be well-formed).
        """
        end = offset + length
        size = len(self.data)
        ranges = sorted([*self.holes, (size, max(end, size))])
        missing = 0
        counted_to = offset
        for a, b in ranges:
            a, b = max(a, counted_to), min(b, end)
            if a < b:
                missing += b - a
                counted_to = b
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
        """Read a TWVL file, refusing one that is truncated or malformed.

        Raises :class:`TruncatedFileError` when the file is shorter than its
        preamble, its header, or the volume size the header implies
        (:func:`_implied_size`), :class:`MalformedFileError` for a header
        that is not a JSON object of the right shape, and ``ValueError``
        for a file that is not a TWVL at all.
        """
        log.debug("loading TWVL volume %s", path)
        blob = path.read_bytes()
        if len(blob) < _PREAMBLE.size:
            if blob[: len(MAGIC)] == MAGIC:
                raise TruncatedFileError(path, "TWVL", "preamble", _PREAMBLE.size, len(blob))
            log.debug("%s: %d bytes is shorter than the TWVL preamble; refusing", path, len(blob))
            raise ValueError(f"{path}: not a TWVL v{VERSION} volume")
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
        header_end = _PREAMBLE.size + hlen
        if len(blob) < header_end:
            raise TruncatedFileError(path, "TWVL", "JSON header", header_end, len(blob))
        header = _parse_header(path, blob[_PREAMBLE.size : header_end])
        data = blob[header_end:]
        size = _implied_size(header)
        if size is not None and len(data) < size:
            log.debug("%s: %d volume bytes, header implies %d; refusing", path, len(data), size)
            raise TruncatedFileError(path, "TWVL", "volume bytes", header_end + size, len(blob))
        return cls(header=header, data=data)


def _parse_header(path: Path, raw: bytes) -> dict[str, Any]:
    """The JSON header, checked for the shape :class:`Volume` relies on."""

    def malformed(problem: str) -> MalformedFileError:
        log.debug("%s: %s; refusing", path, problem)
        return MalformedFileError(path, "TWVL", problem)

    try:
        # JSONDecodeError and UnicodeDecodeError are both ValueErrors.
        header = json.loads(raw)
    except ValueError as exc:
        raise malformed(f"the header is not valid UTF-8 JSON ({exc})") from exc
    if not isinstance(header, dict):
        raise malformed(f"the header is a JSON {type(header).__name__}, not an object")
    fmt = header.get("format", "TWVL")
    if fmt != "TWVL":
        raise malformed(f"the header's format member is {fmt!r}, not 'TWVL'")
    holes = header.get("holes", [])
    if not isinstance(holes, list) or not all(
        isinstance(h, list) and len(h) == 2 and all(type(x) is int for x in h) for h in holes
    ):
        raise malformed("holes must be a list of [start, end] integer pairs")
    header.setdefault("holes", holes)
    size = header.get("volume_size")
    if size is not None and (type(size) is not int or size < 0):
        raise malformed(f"volume_size is {size!r}; it must be a non-negative integer")
    return header


def _implied_size(header: dict[str, Any]) -> int | None:
    """How many volume bytes the header says the file holds, at least.

    ``volume_size`` when present (TWS-3 3.1). Files written before that
    member existed give only a lower bound: no writer puts a hole end or
    ``directory_offset`` past the end of the volume bytes (section 5), so the
    furthest of them must still be inside the file. None when there is
    nothing to go on (no holes, no directory offset): truncation of such an
    old file cannot be told from a short volume.
    """
    if header.get("volume_size") is not None:
        return int(header["volume_size"])
    bounds = [end for _start, end in header["holes"]]
    if type(header.get("directory_offset")) is int:
        bounds.append(header["directory_offset"])
    return max(bounds, default=None)


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

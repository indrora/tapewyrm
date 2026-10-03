"""TWTI: the logical tape image (`tw convert` output).

Step two of ``tw dump -> tw convert -> qicsilver extract``. A TWTI file is the tape
after the QIC-80 layer is done with it: every sector placed by its own address
using the header segment's geometry, the bad-sector map applied, every segment
Reed-Solomon corrected, in segment order -- plus how sure we are of each one.
It knows nothing about backup formats; that is ``qicsilver extract``'s job.

This module is the file format only (read, write, random access). Building
an image from flux lives in ``tapewyrm.image.convert`` (tapewyrm-host).

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
import logging
import mmap
import struct
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path

from tapewyrm_archive.progress import NULL_PROGRESS, Progress

log = logging.getLogger(__name__)

MAGIC = b"TWTI"
VERSION = 1
SEGMENT_STRIDE = 29 * 1024
_PREAMBLE = struct.Struct("<4sHI")
_ENTRY = struct.Struct("<BBHI")


class SegmentState(IntEnum):
    MISSING = 0  # no sector of it was read
    CLEAN = 1  # every sector CRC-good
    CORRECTED = 2  # Reed-Solomon rebuilt 1-3 sectors
    UNCORRECTABLE = 3  # > 3 sectors bad or missing: partial data kept
    BAD = 4  # the bad-sector map marks the whole segment unusable


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

    def save(
        self,
        path: Path,
        segment_data: Callable[[int], bytes],
        *,
        progress: Progress = NULL_PROGRESS,
    ) -> None:
        """Write the image; ``segment_data(n)`` supplies each segment's bytes.

        ``progress`` gets one "writing image" task in segments: each is a
        29 KB write, so a per-segment update is far off any hot path.
        """
        hdr = json.dumps(self.header, indent=1).encode("utf-8")
        log.debug(
            "writing TWTI image %s: %d-byte header, %d segments",
            path,
            len(hdr),
            len(self.entries),
        )
        with path.open("wb") as f:
            f.write(_PREAMBLE.pack(MAGIC, VERSION, len(hdr)))
            f.write(hdr)
            for e in self.entries:
                f.write(_ENTRY.pack(e.state, e.erasures, e.data_len, e.excluded_mask))
            with progress.task("writing image", total=len(self.entries), unit="segments") as bar:
                for n in range(len(self.entries)):
                    f.write(segment_data(n).ljust(SEGMENT_STRIDE, b"\x00"))
                    bar.advance()

    @classmethod
    def open(cls, path: Path) -> TapeImage:
        log.debug("opening TWTI image %s", path)
        f = path.open("rb")
        mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
        magic, version, hlen = _PREAMBLE.unpack_from(mm, 0)
        if magic != MAGIC or version != VERSION:
            log.debug(
                "%s: magic %r version %d != %r version %d; refusing",
                path,
                magic,
                version,
                MAGIC,
                VERSION,
            )
            raise ValueError(f"{path}: not a TWTI v{VERSION} tape image")
        header = json.loads(mm[_PREAMBLE.size : _PREAMBLE.size + hlen])
        at = _PREAMBLE.size + hlen
        count = header["segment_count"]
        log.debug("%s: %d-byte header, %d segment entries", path, hlen, count)
        entries = [
            SegmentEntry(SegmentState(s), e, n, m)
            for s, e, n, m in _ENTRY.iter_unpack(mm[at : at + count * _ENTRY.size])
        ]
        return cls(header=header, entries=entries, _data=mm, _data_at=at + count * _ENTRY.size)


# ---------------------------------------------------------------------------
# tw convert
# ---------------------------------------------------------------------------

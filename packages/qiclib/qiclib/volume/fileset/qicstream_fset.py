"""The ``qicstream-fset`` parser: QICStream v2 "FSET" volumes, a stream of control codes.

What QICStream v2 wrote is not a directory section at all but a *stream*:
control codes one byte at a time, each announcing a file header, a
directory header, a data chunk or a move up the tree, with the catalog
(the same headers again, without data) usually at the end. The layout is
Dmitry Brant's reverse-engineering in QICStreamReader's
``qicstreamv2/Program.cs`` (Apache-2.0; read, never depended on). No tape
on the bench carries it, so the profile that names this parser
(``volume/profiles/qicstream-v2.toml``) is ``untested`` and the synthetic
volumes of :func:`qiclib.testing.builders.build_fset_volume` are the only
ones it has met. Line numbers below are ``Program.cs``'s.

**The volume header** (lines 59-74 and 167-194; the ``Vtbl2Record`` in
``headerview/Program.cs`` reads the same shape with ``VTBL`` in place of
``FSET``)::

    0-3      unknown
    4-7      "FSET"
    8-0x3B   unknown
    0x3C     u16 length of the volume label, then the label
             a zero pad byte when the label ends on an odd offset
    then     u16 length of the drive name, then the drive name ("C:")

**The stream** (lines 37-56 and 214-307): after the header every byte is a
control code or the data a code announced::

    1   contents start (no data)
    2   catalog start: headers follow with no data, skipped until the next 1
    3   up to the parent directory (pop)
    5   a file: a header, then its data chunks
    6   a directory: a header; it becomes the current directory (push)
    9   a data chunk: one junk byte, a u16 length, then that many bytes.
        A file is the sum of its chunks: one under 64 KiB, several above.
    0   a pad byte between codes, for 16-bit alignment (line 55)

Any other byte is skipped, one at a time, as the C# loop ignores it.

**A header** after a 5 or a 6 (``FileHeader``, lines 319-346) is a 3-byte
prefix -- a byte that is always 1, then ``structLen`` as a u16, 0 meaning
the end of the archive -- followed by ``structLen`` bytes::

    0        DOS attribute byte (bit 0 read-only, bit 4 directory)
    4-7      u32 time_t: modification time, seconds since 1970
    8-11     i32 size (0 for a directory)
    0x16-    the name, ASCII, ``structLen - 0x16`` bytes

Those offsets are the *code's* (lines 337-342), which indexes the bytes
after the prefix. The comment block above it (lines 77-95) tabulates the
same fields 3 higher -- attributes at 3-4, the date at 7-A, a 64-bit size
at B-12, the name at 0x19 -- which is the same layout counted from the
prefix, except that it calls the size 64-bit where the code reads 32. The
code is what extracted real tapes, so this parser follows the code; bytes
12-21 of the struct are not read.

What this parser makes of it: a :class:`~qiclib.volume.fileset.entry.VolumeEntry`
per directory header (code 6 in the contents region, its path from the
push/pop stack) and per file header (code 5), the file's ``data_offset``
the first byte of its first chunk and its ``size`` the header's. Chunks
summing short of the size are ``missing``, as are chunk bytes in holes.
Tar member names are the paths as they are, like the Basic-DOS parser's:
the drive name stays in the header and the log. The checks it adds: the
``FSET`` magic is at 4, and the directory stack is balanced when the
stream ends (every push popped, no pop past the root). A length that runs
past the volume -- the label, the drive name, a ``structLen``, a chunk
prefix -- raises, so the probe is fatal: a length field pointing past the
buffer is the classic sign of the wrong layout, exactly what
:meth:`~qiclib.volume.fileset.base.FileSetParser.probe` turns into ``fatal``. A
chunk whose *bytes* are cut off by the end of the volume keeps what is
there and counts the rest missing, since that is what a tape whose last
segments were lost looks like.

Two deliberate departures from the C# walk, both for a damaged tape. A
data chunk met outside a file (its header fell in a hole, or a size
understated its chunks) is skipped whole, where the C# would scan its
bytes as control codes. And a file's chunks end at the first byte that is
neither a pad nor a chunk, where the C# scans forward for the next 9
without limit and would swallow the next file's data when a size
overstates its chunks.

TODO: the catalog region repeats every header without data; a volume whose
contents region has holes could fill its listing from there, raising
``directory-reconstructed``. TODO: a file above 64 KiB is several chunks
with 4-byte chunk prefixes between them, which the contiguous
``data_offset``/``size`` read in ``qicsilver.entries.entry_data`` cannot
follow; :class:`~qiclib.volume.fileset.entry.VolumeEntry` needs an extent list
first (its own TODO), and until then only single-chunk files tar right.
"""

from __future__ import annotations

import logging
import struct
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tapewyrm_archive.twvl import Volume

from qiclib.volume.fileset.base import FileSetParser, Reading, register
from qiclib.volume.fileset.common import volume_name
from qiclib.volume.fileset.entry import DIR_MODE, RO_MODE, RW_MODE, VolumeEntry
from qiclib.volume.table import VtblEntry
from qiclib.volume.verdict import Check, VolumeWarning

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# The format's constants (qicstreamv2/Program.cs)
# ---------------------------------------------------------------------------

MAGIC = b"FSET"  # at volume byte 4 (line 170)
MAGIC_OFFSET = 4
LABEL_LENGTH_OFFSET = 0x3C  # u16; the label itself starts at 0x3E (lines 168-177)
#: The control codes (the ``ControlCode`` enum, lines 110-119) and the pad byte.
CONTENTS_START, CATALOG_START, PARENT_DIR, FILE, DIRECTORY, DATA_CHUNK = 1, 2, 3, 5, 6, 9
PAD = 0
#: A header's prefix: a byte that is always 1, then ``structLen`` (lines 330-332)...
HEADER_PREFIX = struct.Struct("<BH")
#: ...and, inside the struct, the attribute byte at 0, the time_t at 4 and the
#: i32 size at 8 (lines 337-339); the name runs from 0x16 to the end (lines
#: 341-342). Bytes 12-21 are not read.
HEADER_FIELDS = struct.Struct("<B3xIi")
NAME_OFFSET = 0x16
#: A data chunk's prefix after its code: a junk byte and the u16 length (lines 282-283).
CHUNK_PREFIX = struct.Struct("<BH")

_DOS_READ_ONLY = 0x01


# ---------------------------------------------------------------------------
# The walk: bytes to records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FsetHeader:
    """One file or directory header, as the C# ``FileHeader`` reads it (lines 327-345)."""

    offset: int  # of the 3-byte prefix, in the volume
    length: int  # prefix and struct together
    attrs: int
    mtime: int | None  # a time_t of 0 is "not recorded", as the Extended parser treats a null
    size: int
    name: str


@dataclass
class FsetRecord:
    """One entry of the contents region: its header, its path and, for a file, its chunks."""

    header: FsetHeader
    path: str
    is_dir: bool
    chunks: list[tuple[int, int]] = field(default_factory=list)  # (first byte, length) each


@dataclass
class FsetScan:
    """What :func:`scan` found: the volume header's strings and every contents record.

    ``depth`` is how many directories were still pushed when the stream
    ended and ``underflows`` how many pops met an empty stack; both are 0
    on a stream read right. ``magic`` is whatever sat at bytes 4-7.
    """

    magic: bytes
    label: str
    drive: str
    records: list[FsetRecord]
    depth: int
    underflows: int
    catalog_headers: int
    skipped: int  # bytes passed over: pads and codes the format does not define


def _take(data: bytes, offset: int, length: int, what: str) -> bytes:
    """``length`` bytes at ``offset``, or a ``ValueError`` naming what ran past the volume."""
    if length < 0 or offset + length > len(data):
        log.debug(
            "%s: %d bytes at %d run past the %d-byte volume; raising",
            what,
            length,
            offset,
            len(data),
        )
        raise ValueError(
            f"{what}: {length} bytes at offset {offset} run past the {len(data)}-byte volume"
        )
    return data[offset : offset + length]


def _ascii(raw: bytes) -> str:
    """ASCII, as the C# decodes it; trailing NULs dropped, anything else replaced (U+FFFD)."""
    return raw.rstrip(b"\x00").decode("ascii", errors="replace")


def read_header(data: bytes, offset: int) -> FsetHeader | None:
    """The header whose prefix is at ``offset``; ``None`` when its structLen is 0.

    A structLen of 0 is the end of the archive. The C# tests that for a
    file's header only (``Valid``, lines 260-264) and would push a null
    name for a directory's; here it ends the walk either way.
    """
    always_one, struct_len = HEADER_PREFIX.unpack(
        _take(data, offset, HEADER_PREFIX.size, "a header prefix")
    )
    if struct_len == 0:
        log.debug("header at %d: structLen 0; the end of the archive", offset)
        return None
    if always_one != 1:
        log.debug("header at %d: prefix byte is %d, not 1; reading on", offset, always_one)
    body = _take(data, offset + HEADER_PREFIX.size, struct_len, f"the header at {offset}")
    if struct_len < NAME_OFFSET:
        # The C# computes a negative name length here and throws.
        log.debug(
            "header at %d: structLen %d is shorter than the fixed part; raising", offset, struct_len
        )
        raise ValueError(
            f"the header at {offset} claims {struct_len} bytes, fewer than the {NAME_OFFSET} "
            "before its name"
        )
    attrs, time_t, size = HEADER_FIELDS.unpack_from(body)
    if size < 0:
        log.debug("header at %d: size %d is negative; raising", offset, size)
        raise ValueError(f"the header at {offset} claims a negative size, {size}")
    return FsetHeader(
        offset,
        HEADER_PREFIX.size + struct_len,
        attrs,
        time_t or None,
        size,
        _ascii(body[NAME_OFFSET:]),
    )


def _chunk(data: bytes, offset: int) -> tuple[int, int]:
    """(first data byte, length) of the chunk whose code is at ``offset``.

    A length past the end of the volume is clamped to it: the bytes that
    are there are the file's, and the rest are missing.
    """
    _junk, length = CHUNK_PREFIX.unpack(
        _take(data, offset + 1, CHUNK_PREFIX.size, f"the chunk prefix at {offset}")
    )
    start = offset + 1 + CHUNK_PREFIX.size
    if start + length > len(data):
        log.debug(
            "chunk at %d: %d bytes run past the %d-byte volume; keeping what is there",
            offset,
            length,
            len(data),
        )
        length = len(data) - start
    return start, length


def _read_chunks(data: bytes, pos: int, size: int) -> tuple[list[tuple[int, int]], int]:
    """A file's chunks from ``pos`` until they sum to ``size`` or something else begins.

    Returns the chunks and where the stream continues. The C# (lines
    273-298) scans forward for the next 9 without limit; this stops at the
    first byte that is neither a pad nor a chunk (see the module docstring).
    """
    chunks: list[tuple[int, int]] = []
    got = 0
    while got < size:
        while pos < len(data) and data[pos] == PAD:  # a pad may precede a chunk (line 55)
            pos += 1
        if pos >= len(data) or data[pos] != DATA_CHUNK:
            log.debug(
                "file chunks end at %d with %d of %d bytes: %s",
                pos,
                got,
                size,
                "the volume ends" if pos >= len(data) else f"code {data[pos]} is not a chunk",
            )
            break
        start, length = _chunk(data, pos)
        chunks.append((start, length))
        got += length
        pos = start + length
    return chunks, pos


def scan(data: bytes) -> FsetScan:
    """Walk the volume's bytes -- the header, then the control codes -- into records.

    Pure bytes, no hole map (:meth:`QicstreamFsetParser.read` adds that), so
    a test can feed it a stream straight from the builder. Raises
    ``ValueError`` for any length that runs past the volume.
    """
    size = len(data)
    head = _take(data, 0, LABEL_LENGTH_OFFSET + 2, "the volume header")
    magic = head[MAGIC_OFFSET : MAGIC_OFFSET + len(MAGIC)]
    if magic != MAGIC:
        log.debug(
            "volume bytes 4-7 are %s, not %s; reading on for the check", magic.hex(), MAGIC.hex()
        )
    (label_len,) = struct.unpack_from("<H", head, LABEL_LENGTH_OFFSET)
    pos = LABEL_LENGTH_OFFSET + 2
    label = _ascii(_take(data, pos, label_len, "the volume label"))
    pos += label_len
    if pos % 2:  # the drive name starts on an even offset (lines 184-188)
        pos += 1
    (drive_len,) = struct.unpack("<H", _take(data, pos, 2, "the drive name length"))
    pos += 2
    drive = _ascii(_take(data, pos, drive_len, "the drive name"))
    pos += drive_len
    log.debug("FSET header: label %r, drive %r; the stream starts at %d", label, drive, pos)

    records: list[FsetRecord] = []
    stack: list[str] = []
    catalog = False
    underflows = catalog_headers = skipped = 0
    while pos < size:
        at, code = pos, data[pos]
        pos += 1
        if code == CATALOG_START:
            log.debug("%d: catalog start; headers are skipped until the next contents start", at)
            catalog = True
        elif code == CONTENTS_START:
            log.debug("%d: contents start", at)
            catalog = False
        elif code == PARENT_DIR:
            if catalog:
                continue
            if stack:
                stack.pop()
            else:
                log.debug("%d: up from the root; nothing to pop", at)
                underflows += 1
        elif code in (FILE, DIRECTORY):
            header = read_header(data, pos)
            if header is None:
                break
            pos = header.offset + header.length
            if catalog:
                catalog_headers += 1
                continue
            path = "/".join([*stack, header.name])
            if code == DIRECTORY:
                stack.append(header.name)
                records.append(FsetRecord(header, path, True))
                continue
            chunks, pos = _read_chunks(data, pos, header.size)
            records.append(FsetRecord(header, path, False, chunks))
        elif code == DATA_CHUNK:
            start, length = _chunk(data, at)
            log.debug("%d: a %d-byte chunk outside any file; skipped whole", at, length)
            pos = start + length
        else:
            skipped += 1  # pads and undefined codes; summarised below, not logged each
    log.debug(
        "FSET stream: %d records, %d catalog headers, %d bytes skipped, "
        "%d directories left open, %d pops past the root",
        len(records),
        catalog_headers,
        skipped,
        len(stack),
        underflows,
    )
    return FsetScan(magic, label, drive, records, len(stack), underflows, catalog_headers, skipped)


# ---------------------------------------------------------------------------
# The parser
# ---------------------------------------------------------------------------


class QicstreamFsetParser(FileSetParser):
    name = "qicstream-fset"
    description = "QICStream v2 'FSET' stream: control codes, file headers and data chunks"

    def read(
        self, vol: Volume, vtbl: VtblEntry, source: Path, params: Mapping[str, Any]
    ) -> Reading:
        """Scan the stream, then place each record against the volume's hole map.

        ``vtbl`` goes unused: the stream carries its own structure, and the
        table entry's section sizes say nothing about it (if the Rev N
        reading of that entry even applies; see the profile).
        """
        vol_name = volume_name(vol, source)
        log.debug("%s: scanning the FSET stream (%d volume bytes)", vol_name, len(vol.data))
        found = scan(vol.data)
        log.info(
            f"tape {vol.header.get('tape_name')!r}: FSET label {found.label!r}, drive "
            f"{found.drive!r}: {len(found.records)} entries ({found.catalog_headers} catalog "
            f"headers skipped); {vol.missing(0, len(vol.data)):,} of {len(vol.data):,} volume "
            "bytes missing"
        )
        entries: list[VolumeEntry] = []
        warnings: set[VolumeWarning] = set()
        for rec in found.records:
            h = rec.header
            if vol.missing(h.offset, h.length):
                # A header read out of a hole is zeros, and so is what the walk
                # made of everything after it: the listing cannot be trusted.
                log.debug(
                    "%s: header bytes %d-%d fall in a hole",
                    rec.path,
                    h.offset,
                    h.offset + h.length - 1,
                )
                warnings.add(VolumeWarning.DIRECTORY_DAMAGED)
            if rec.is_dir:
                entries.append(
                    VolumeEntry(
                        rec.path, rec.path, True, 0, h.mtime, h.attrs, DIR_MODE, None, 0, False
                    )
                )
                continue
            got = sum(length for _, length in rec.chunks)
            in_holes = sum(vol.missing(start, length) for start, length in rec.chunks)
            missing = min(h.size, max(0, h.size - got) + in_holes)
            if missing:
                log.debug(
                    "%s: damaged (%d of %d bytes missing: the chunks hold %d, %d of them in holes)",
                    rec.path,
                    missing,
                    h.size,
                    got,
                    in_holes,
                )
            mode = RO_MODE if h.attrs & _DOS_READ_ONLY else RW_MODE
            entries.append(
                VolumeEntry(
                    rec.path,
                    rec.path,
                    False,
                    h.size,
                    h.mtime,
                    h.attrs,
                    mode,
                    rec.chunks[0][0] if rec.chunks else None,
                    missing,
                    False,  # the format has no "unreadable at backup" flag that we know of
                )
            )
        checks = [
            Check(
                "FSET magic", found.magic == MAGIC, 2, f"volume bytes 4-7 are {found.magic.hex()}"
            ),
            # Weight 1: a tape cut short mid-tree leaves directories open too,
            # and that is damage, not the wrong layout.
            Check(
                "directory stack balanced",
                found.depth == 0 and found.underflows == 0,
                detail=f"{found.depth} directories left open, {found.underflows} pops past the root",
            ),
        ]
        return Reading(entries, checks, warnings)


register(QicstreamFsetParser())

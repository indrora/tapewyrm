"""TWTI / TWTZ: the logical tape image (`tw convert` output).

Step two of ``tw dump -> tw convert -> qicsilver extract``. A TWTI file is the tape
after the QIC-80 layer is done with it: every sector placed by its own address
using the header segment's geometry, the bad-sector map applied, every segment
Reed-Solomon corrected, in segment order -- plus how sure we are of each one.
It knows nothing about backup formats; that is ``qicsilver extract``'s job.

This module is the file format only (read, write, random access). Building
an image lives in ``qiclib.build``, which ``tw convert``
(``tapewyrm.image.convert``, tapewyrm-cli) feeds with the sectors it
decodes from flux. The byte layout is owned by ``docs/spec/twti.md`` (TWS-2).

Layout (little endian)::

    "TWTI"  u16 version  u32 header_len  header (JSON, UTF-8)
    segment table: segment_count x 8 bytes
        u8  status          (SegmentState: missing/clean/corrected/uncorrectable/bad)
        u8  erasures        sectors RS had to rebuild (or couldn't)
        u16 data_len        bytes of real data in this segment's slot
        u32 excluded_mask   bit k = sector k excluded by the bad-sector map
    segment data: segment_count x SEGMENT_STRIDE bytes (29 KiB each, zero-padded)

The fixed stride gives random access by segment number. A segment's data is
its corrected data rows (29 sectors, fewer when the bad-sector map excludes
some); missing segments are all zero. The JSON header carries the drive
identity and the source TWRF headers, so provenance survives every step.

Sparse TWTI
-----------
The fixed stride is expensive on paper: a QIC-3020 tape has ~59,000 segments,
so one captured track still makes a 1.75 GB image, nearly all zeros.
:meth:`TapeImage.save` therefore *seeks over* every slot that is empty or
all-zero instead of writing it, and sets the final length with ``truncate``.
On filesystems with holes (APFS, ext4, XFS, btrfs, ZFS) the zeros cost no
disk; holes read back as zeros, so the bytes are identical to a dense file
and readers need not care. (NTFS only makes holes for files flagged sparse,
which Python cannot do portably; there the file is simply dense. APFS also
densifies files of 16 MiB or less that have interior holes -- measured, not
documented -- which only matters for toy images.) ``ls`` shows the logical
length; ``du`` shows what it really costs.

TWTZ: zstd-compressed TWTI
--------------------------
A ``.twtz`` file is a TWTI byte stream compressed as Zstandard data --
exactly what ``.tar.zst`` is to ``.tar`` -- so ``zstd -d x.twtz`` gives a valid
``x.twti``. TWS-2 section 7 lets it be one or more zstd frames: readers
accept several, and writers SHOULD write one, which :meth:`TapeImage.save`
does (one compressor, one frame). It is for moving and archiving images: the zero slots compress to
almost nothing, wherever the file lands.

- Writing is chosen by suffix: :meth:`TapeImage.save` to a ``*.twtz`` path
  streams through the compressor (``tapewyrm_archive._zstd``), never holding
  the image in memory.
- Reading is chosen by *magic*, not suffix (:func:`sniff`): ``b"TWTI"`` is
  mapped directly; the zstd frame magic ``28 B5 2F FD`` is stream-decompressed
  into a temporary sparse ``.twti`` (zero runs skipped, as above) which is
  then mapped like any other image and deleted on :meth:`TapeImage.close`,
  on garbage collection, or at interpreter exit. Random access into a zstd
  stream would need a seekable-frame index; a sparse temp file gets the same
  ``segment(n)`` API for the cost of one sequential pass.
"""

from __future__ import annotations

import json
import logging
import mmap
import os
import struct
import tempfile
import weakref
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path
from typing import IO, Any, Literal, overload

from tapewyrm_archive.errors import MalformedFileError, TruncatedFileError
from tapewyrm_archive.progress import NULL_PROGRESS, Progress

log = logging.getLogger(__name__)

MAGIC = b"TWTI"
# Every Zstandard frame starts with this magic number (RFC 8878 §3.1.1,
# 0xFD2FB528 little endian). A TWTZ file starts with one: it is one or more
# frames (readers take them all), and TapeImage.save writes exactly one.
ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
COMPRESSED_SUFFIX = ".twtz"
VERSION = 1
SEGMENT_STRIDE = 29 * 1024
_PREAMBLE = struct.Struct("<4sHI")
_ENTRY = struct.Struct("<BBHI")

# Decompression granularity for TWTZ -> sparse temp file. Chunks are read at
# _CHUNK and, unless wholly zero, scanned in _BLOCK pieces so a mostly-empty
# chunk still leaves holes. 4 KiB is the allocation block of APFS and ext4;
# a smaller block could not make a smaller hole.
_CHUNK = 1 << 20
_BLOCK = 4096


class SegmentState(IntEnum):
    MISSING = 0  # no sector of it was read
    CLEAN = 1  # every sector CRC-good
    CORRECTED = 2  # Reed-Solomon rebuilt 1-3 sectors
    UNCORRECTABLE = 3  # > 3 sectors bad or missing: partial data kept
    BAD = 4  # the bad-sector map marks the whole segment unusable


# The state values a version-1 table may hold; 5-255 are reserved (TWS-2 5.2).
_STATES = frozenset(int(state) for state in SegmentState)


@dataclass(frozen=True)
class SegmentEntry:
    state: SegmentState
    erasures: int = 0
    data_len: int = 0
    excluded_mask: int = 0


def sniff(path: Path) -> str | None:
    """Which tape image ``path`` is by its magic: ``"TWTI"``, ``"TWTZ"`` or None.

    Suffixes are ignored on purpose: a renamed image must still open. Any
    zstd stream is reported as TWTZ here; :meth:`TapeImage.open` checks the
    TWTI preamble inside once it is decompressed.
    """
    with path.open("rb") as f:
        magic = f.read(4)
    if magic == MAGIC:
        return "TWTI"
    if magic == ZSTD_MAGIC:
        return "TWTZ"
    log.debug("%s: magic %r is neither %r nor zstd %r", path, magic, MAGIC, ZSTD_MAGIC)
    return None


def _is_zero(buf: bytes | memoryview) -> bool:
    # bytes.count runs in C; a Python any() over 29 KiB per segment would not.
    # bytes(b) of a bytes is b itself; of a memoryview block, a 4 KiB copy.
    return bytes(buf).count(0) == len(buf)


def _release(mm: mmap.mmap | None, f: IO[bytes] | None, temp: str | None) -> None:
    """Finalizer body: unmap, close and (for TWTZ) delete the temp image.

    Module-level and given only what it frees, never the TapeImage itself, or
    ``weakref.finalize`` would keep the image alive forever.
    """
    if mm is not None:
        mm.close()
    if f is not None:
        f.close()
    if temp is not None:
        log.debug("removing temporary decompressed image %s", temp)
        try:
            os.unlink(temp)
        except FileNotFoundError:
            pass


@dataclass
class TapeImage:
    header: dict
    entries: list[SegmentEntry] = field(default_factory=list)
    _data: bytes | mmap.mmap = b""
    _data_at: int = 0
    # Set by open(): releases the mapping (and a TWTZ temp file). Not data.
    _finalizer: weakref.finalize | None = field(default=None, repr=False, compare=False)
    # Set by _parse(): the file the image came from and its kind, so a header
    # member found wrong after opening (qic80_segment, qic80_str) is reported
    # against the file like any other malformed-file error. An image built in
    # memory (qiclib.build) has no file.
    _path: Path | None = field(default=None, repr=False, compare=False)
    _kind: str = field(default="TWTI", repr=False, compare=False)

    def segment(self, n: int) -> bytes:
        """Segment ``n``'s data (``data_len`` bytes; empty if missing)."""
        e = self.entries[n]
        start = self._data_at + n * SEGMENT_STRIDE
        return bytes(self._data[start : start + e.data_len])

    # --- qic80_header members, checked at the point of use ---
    #
    # Why here and not in _parse: TWS-2 says qic80_header is REQUIRED of
    # writers, but readers "MUST tolerate the absence of any member other than
    # those they need" (section 4.3), and a reader that wants only segment data
    # needs only segment_count (section 9.2, rule 5) -- tw inspect, for one,
    # reports on an image whatever its FPR copy holds. So open() cannot refuse
    # an image for lacking first_data_seg. What it can do is own the check, so
    # every consumer that *does* need a member (qicsilver extract and identify)
    # gets the same validation and the same error: the member's JSON type
    # (section 11.2: check the type of each member used; true/false are not
    # integers) and, for a segment number, its range against segment_count
    # (section 11.2: check every segment number from the header before use).
    # Section 9.2 rule 13 states the whole rule.

    def _malformed(self, problem: str) -> MalformedFileError:
        where = self._path if self._path is not None else "tape image"
        log.debug("%s: %s; refusing", where, problem)
        return MalformedFileError(where, self._kind, problem)

    def _qic80_member(self, name: str, required: bool) -> Any:
        """``qic80_header[name]`` as stored, or None when absent and not ``required``."""
        fpr = self.header.get("qic80_header")
        if fpr is None and not required:
            log.debug("header has no qic80_header; %s treated as absent", name)
            return None
        if fpr is None:
            raise self._malformed(
                f"the header lacks the REQUIRED member qic80_header, so qic80_header.{name} "
                "is missing"
            )
        if not isinstance(fpr, dict):
            raise self._malformed(f"qic80_header is a JSON {type(fpr).__name__}, not an object")
        if name not in fpr:
            if not required:
                log.debug("qic80_header has no %s; treated as absent", name)
                return None
            raise self._malformed(f"the header lacks qic80_header.{name}, which is needed here")
        return fpr[name]

    @overload
    def qic80_segment(self, name: str, *, required: Literal[True] = ...) -> int: ...
    @overload
    def qic80_segment(self, name: str, *, required: bool) -> int | None: ...
    def qic80_segment(self, name: str, *, required: bool = True) -> int | None:
        """Segment number ``qic80_header[name]``, checked to be a segment of this image.

        Raises :class:`MalformedFileError` naming the file and the member when
        it is absent (and ``required``), not an integer, or outside
        ``0 .. segment_count - 1``. Returns None for an absent optional member.
        """
        value = self._qic80_member(name, required)
        if value is None and not required:
            return None
        # type() rather than isinstance: bool is an int subclass.
        if type(value) is not int:
            raise self._malformed(f"qic80_header.{name} is {value!r}; it must be an integer")
        if not 0 <= value < len(self.entries):
            raise self._malformed(
                f"qic80_header.{name} is {value}, but the image holds segments "
                f"0..{len(self.entries) - 1}"
            )
        return value

    @overload
    def qic80_str(self, name: str, *, required: Literal[True] = ...) -> str: ...
    @overload
    def qic80_str(self, name: str, *, required: bool) -> str | None: ...
    def qic80_str(self, name: str, *, required: bool = True) -> str | None:
        """String member ``qic80_header[name]`` (e.g. ``tape_name``), type-checked.

        Raises :class:`MalformedFileError` naming the file and the member when
        it is absent (and ``required``) or not a string.
        """
        value = self._qic80_member(name, required)
        if value is None and not required:
            return None
        if not isinstance(value, str):
            raise self._malformed(f"qic80_header.{name} is {value!r}; it must be a string")
        return value

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for e in self.entries:
            out[e.state.name] = out.get(e.state.name, 0) + 1
        return out

    def close(self) -> None:
        """Unmap the image and delete a TWTZ's temporary file (idempotent)."""
        if self._finalizer is not None:
            self._finalizer()

    def __enter__(self) -> TapeImage:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --- persistence ---

    def save(
        self,
        path: Path,
        segment_data: Callable[[int], bytes],
        *,
        progress: Progress = NULL_PROGRESS,
    ) -> None:
        """Write the image; ``segment_data(n)`` supplies each segment's bytes.

        A ``.twtz`` path is written zstd-compressed, anything else as a
        sparse TWTI (module docstring). ``progress`` gets one "writing image"
        task in segments: each is at most a 29 KiB write, so a per-segment
        update is far off any hot path.
        """
        hdr = json.dumps(self.header, indent=1).encode("utf-8")
        compressed = path.suffix.lower() == COMPRESSED_SUFFIX
        log.debug(
            "writing %s image %s: %d-byte header, %d segments",
            "TWTZ" if compressed else "sparse TWTI",
            path,
            len(hdr),
            len(self.entries),
        )
        if compressed:
            from tapewyrm_archive._zstd import zstd

            with zstd.ZstdFile(path, "wb") as zf:
                self._write(zf, hdr, segment_data, progress, sparse=False)
        else:
            with path.open("wb") as f:
                self._write(f, hdr, segment_data, progress, sparse=True)

    def _write(
        self,
        f: IO[bytes],
        hdr: bytes,
        segment_data: Callable[[int], bytes],
        progress: Progress,
        *,
        sparse: bool,
    ) -> None:
        """The TWTI byte stream, to a seekable file (``sparse``) or a compressor.

        Sparse: each slot is written only if it holds a non-zero byte, at its
        absolute offset; the rest are left as holes and the length is fixed
        by ``truncate`` at the end (seeking past EOF alone does not extend a
        file). Streamed: every slot is written in full, padding and all --
        a zstd stream cannot seek, and zeros compress to almost nothing.
        """
        f.write(_PREAMBLE.pack(MAGIC, VERSION, len(hdr)))
        f.write(hdr)
        for e in self.entries:
            f.write(_ENTRY.pack(e.state, e.erasures, e.data_len, e.excluded_mask))
        base = _PREAMBLE.size + len(hdr) + len(self.entries) * _ENTRY.size
        skipped = 0
        with progress.task("writing image", total=len(self.entries), unit="segments") as bar:
            for n in range(len(self.entries)):
                data = segment_data(n)
                if not sparse:
                    f.write(data.ljust(SEGMENT_STRIDE, b"\x00"))
                elif data and not _is_zero(data):
                    f.seek(base + n * SEGMENT_STRIDE)
                    f.write(data)
                else:
                    skipped += 1
                bar.advance()
        if sparse:
            end = base + len(self.entries) * SEGMENT_STRIDE
            log.debug(
                "left %d of %d segment slots as holes; setting length to %d bytes",
                skipped,
                len(self.entries),
                end,
            )
            f.truncate(end)

    @classmethod
    def open(cls, path: Path, *, progress: Progress = NULL_PROGRESS) -> TapeImage:
        """Map a TWTI image, or a TWTZ one via a temporary sparse TWTI.

        The format comes from the magic (:func:`sniff`), not the suffix.
        ``progress`` gets a "decompressing image" task in compressed bytes
        for TWTZ, and nothing for TWTI (mapping is instant).
        """
        log.debug("opening tape image %s", path)
        kind = sniff(path)
        temp: str | None = None
        if kind == "TWTZ":
            temp = _decompress_to_temp(path, progress)
            source = Path(temp)
        else:
            source = path
        f = None
        mm: mmap.mmap | None = None
        try:
            f = source.open("rb")
            # mmap refuses a zero-length file; an empty "image" goes through
            # _parse like any other short one so it gets the same diagnosis.
            if os.fstat(f.fileno()).st_size:
                mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
            img = cls._parse(path, mm if mm is not None else b"", compressed=temp is not None)
            if img is None:
                raise ValueError(f"{path}: not a TWTI v{VERSION} tape image")
        except BaseException:
            _release(mm, f, temp)
            raise
        img._finalizer = weakref.finalize(img, _release, mm, f, temp)
        return img

    @classmethod
    def _parse(
        cls,
        path: Path,
        buf: bytes | mmap.mmap,
        *,
        compressed: bool = False,
        data_area: bool = True,
    ) -> TapeImage | None:
        """Preamble, header and segment table out of a mapped TWTI; None if not one.

        None means "this is not a TWTI at all" (wrong magic or version).
        A file that *is* one but breaks TWS-2 section 9.2 raises instead:
        :class:`TruncatedFileError` when it is shorter than its own preamble,
        header and table say (``L``, TWS-2 section 2), and
        :class:`MalformedFileError` for a bad header or table entry.

        Truncation is judged by the file's *length*, never by what is
        allocated: a sparse image's holes count toward its length (that is
        what ``truncate`` at the end of :meth:`save` is for), so a sparse
        file of the right length is complete however little disk it uses.
        Every length check happens before any per-segment structure is built,
        so a corrupt ``segment_count`` cannot make us allocate for segments
        the file does not hold. For a TWTZ (``compressed``), ``buf`` is the
        decompressed stream and the sizes in messages count decompressed
        bytes; a TWTZ whose stream is complete but holds a short TWTI is just
        as truncated as a short TWTI.

        ``data_area=False`` is for a caller holding only a *prefix* of the
        image (``tapewyrm_archive.inspect``, which decompresses no more of a
        TWTZ than the preamble, header and table): everything up to the end
        of the segment table is checked as usual, the data area's length is
        not, and the image returned must not be asked for segment data.
        """
        kind = "TWTZ" if compressed else "TWTI"
        note = " (decompressed)" if compressed else ""
        found = len(buf)

        def truncated(part: str, expected: int) -> TruncatedFileError:
            log.debug("%s: %s needs %d bytes, file has %d; refusing", path, part, expected, found)
            return TruncatedFileError(path, kind, part, expected, found, note=note)

        def malformed(problem: str) -> MalformedFileError:
            log.debug("%s: %s; refusing", path, problem)
            return MalformedFileError(path, kind, problem)

        if found < _PREAMBLE.size:
            # Our magic followed by too few bytes is a cut-off image; anything
            # else this short is simply not one.
            if bytes(buf[: len(MAGIC)]) == MAGIC:
                raise truncated("preamble", _PREAMBLE.size)
            log.debug("%s: %d bytes is shorter than the TWTI preamble; refusing", path, found)
            return None
        magic, version, hlen = _PREAMBLE.unpack_from(buf, 0)
        if magic != MAGIC or version != VERSION:
            log.debug(
                "%s: magic %r version %d != %r version %d; refusing",
                path,
                magic,
                version,
                MAGIC,
                VERSION,
            )
            return None

        # --- JSON header (TWS-2 section 4, reader rules 3, 5 and 6) ---
        header_end = _PREAMBLE.size + hlen
        if found < header_end:
            raise truncated("JSON header", header_end)
        try:
            # JSONDecodeError and UnicodeDecodeError are both ValueErrors.
            header = json.loads(buf[_PREAMBLE.size : header_end])
        except ValueError as exc:
            raise malformed(f"the header is not valid UTF-8 JSON ({exc})") from exc
        if not isinstance(header, dict):
            raise malformed(f"the header is a JSON {type(header).__name__}, not an object")
        count = header.get("segment_count")
        # type() rather than isinstance: JSON true/false are bools, and bool
        # is an int subclass we do not want to accept as a count.
        if type(count) is not int or count < 0:
            raise malformed(f"segment_count is {count!r}; it must be a non-negative integer")
        # Both members are REQUIRED of writers but readers need only
        # segment_count (rule 5), so an absent one is fine; a wrong one is not.
        stride = header.get("segment_stride", SEGMENT_STRIDE)
        if stride != SEGMENT_STRIDE:
            raise malformed(
                f"segment_stride is {stride!r}; version {VERSION} images use {SEGMENT_STRIDE}"
            )
        fmt = header.get("format", "TWTI")
        if fmt != "TWTI":
            raise malformed(f"the header's format member is {fmt!r}, not 'TWTI'")

        # --- lengths first: segment table, then the whole data area ---
        table_end = header_end + count * _ENTRY.size
        if found < table_end:
            raise truncated("segment table", table_end)
        length = table_end + count * SEGMENT_STRIDE
        if data_area and found < length:
            raise truncated("segment data area", length)
        if not data_area:
            log.debug("%s: prefix only; not checking the %d-byte data area", path, length)
        log.debug("%s: %d-byte header, %d segment entries, %d bytes", path, hlen, count, length)

        # --- segment table entries (reader rules 7 and 8) ---
        entries: list[SegmentEntry] = []
        for n, (state, erasures, data_len, mask) in enumerate(
            _ENTRY.iter_unpack(buf[header_end:table_end])
        ):
            if state not in _STATES:
                raise malformed(f"segment {n} has reserved state value {state}")
            if data_len > SEGMENT_STRIDE:
                raise malformed(
                    f"segment {n} has data_len {data_len:,}, more than its "
                    f"{SEGMENT_STRIDE:,}-byte slot"
                )
            entries.append(SegmentEntry(SegmentState(state), erasures, data_len, mask))
        return cls(
            header=header,
            entries=entries,
            _data=buf,
            _data_at=table_end,
            _path=path,
            _kind=kind,
        )


def _temp_image(path: Path) -> tuple[Any, str]:
    """A new temp ``.twti`` in the system temp dir, else next to ``path``."""
    try:
        fd, name = tempfile.mkstemp(prefix=f"{path.stem}-", suffix=".twti")
    except OSError as exc:
        log.debug("no temp file in %s (%r); using %s", tempfile.gettempdir(), exc, path.parent)
        fd, name = tempfile.mkstemp(prefix=f".{path.stem}-", suffix=".twti", dir=path.parent)
    return os.fdopen(fd, "wb"), name


def _decompress_to_temp(path: Path, progress: Progress) -> str:
    """Stream-decompress a TWTZ into a sparse temp TWTI; return its path.

    Zero runs are seeked over (whole chunks first, then 4 KiB blocks) so a
    mostly-blank tape costs only its real data on disk, then ``truncate``
    sets the length a trailing hole would otherwise leave short.
    """
    from tapewyrm_archive._zstd import zstd

    packed = path.stat().st_size
    out, name = _temp_image(path)
    log.info("decompressing %s (%.1f MB) to %s", path.name, packed / 1e6, name)
    length = written = 0
    try:
        with (
            out,
            path.open("rb") as raw,
            zstd.ZstdFile(raw, "rb") as zf,
            progress.task("decompressing image", total=packed, unit="bytes") as bar,
        ):
            while chunk := zf.read(_CHUNK):
                if not _is_zero(chunk):
                    view = memoryview(chunk)
                    for at in range(0, len(chunk), _BLOCK):
                        block = view[at : at + _BLOCK]
                        if not _is_zero(block):
                            out.seek(length + at)
                            out.write(block)
                            written += len(block)
                length += len(chunk)
                bar.update(raw.tell())
            out.truncate(length)
    except EOFError as exc:
        # ZstdFile raises EOFError when the input ends mid-frame: the .twtz
        # was cut short. Whatever did decompress is not trusted.
        _release(None, None, name)
        log.debug("%s: zstd stream ended mid-frame after %d bytes: %r", path, length, exc)
        raise TruncatedFileError(
            path,
            "TWTZ",
            "zstd stream",
            note=f": it ends without its end-of-frame marker after {length:,} decompressed bytes",
        ) from exc
    except zstd.ZstdError as exc:
        _release(None, None, name)
        log.debug("%s: zstd stream is corrupt: %r", path, exc)
        raise MalformedFileError(path, "TWTZ", f"the zstd stream is corrupt ({exc})") from exc
    except BaseException:
        _release(None, None, name)
        raise
    log.info(
        "decompressed %s: %.1f MB image, %.1f MB of it non-zero",
        path.name,
        length / 1e6,
        written / 1e6,
    )
    return name

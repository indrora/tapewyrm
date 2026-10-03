"""Header inspection for every Tapewyrm file: TWRF, TWTI, TWTZ and TWVL.

The library half of ``tw inspect FILE``. It sits beside the three formats of
``tw dump -> tw convert -> qicsilver extract`` (TWS-1 ``twrf``, TWS-2
``twti``, TWS-3 ``twvl``) and reads any of them *only as far as its header*:

- :func:`inspect` identifies the file by its magic (TWS-2 section 8: never by
  its name), reads the preamble and the JSON header, keeps the header bytes
  exactly as stored (so ``--json`` can print them verbatim), and adds what is
  cheap: the file size, the size of the body behind the header, and for a
  tape image the segment-state counts from the segment table -- the table
  only, never the 29 KB data slots.
- :func:`describe` turns an :class:`Inspection` into titled sections of
  (label, value) rows with every value already formatted -- thousands
  separators, MB, the drive's QIC-117 report bytes decoded -- and nothing
  else. It is presentation-neutral on purpose: this package is stdlib-only
  (STYLE.md section 2), so the CLI owns the rich tables these become.

Cheap means bounded by the header, not by the file. A TWRF's marker summary
is *not* included: markers ride inside the flux stream and can only be found
by walking the whole body token by token (``twrf.parse_body``, seconds on a
60 MB capture). A TWTZ is stream-decompressed only until the segment table
ends, through an incremental ``ZstdDecompressor`` capped with ``max_length``,
so a 59,000-segment image costs a few hundred KB of zstd input rather than a
1.75 GB temporary file (compare ``twti._decompress_to_temp``).

Validation is the readers' own, so ``tw inspect`` refuses exactly what the
readers refuse: the TWRF header goes through ``twrf._read_header``, the TWTI
preamble, header and table through ``TapeImage._parse`` (with the data-area
length check left to us, since we hold only a prefix), and the TWVL header
through ``twvl._parse_header``. Short files raise
:class:`~tapewyrm_archive.errors.TruncatedFileError`, broken ones
:class:`~tapewyrm_archive.errors.MalformedFileError`, and a file that is none
of the four a plain ``ValueError``. The one thing a TWTZ inspection cannot
check is that its stream is complete past the table: that needs the whole
stream, which is what this module exists to avoid.
"""

from __future__ import annotations

import io
import json
import logging
import struct
from dataclasses import dataclass, field, replace
from os import PathLike
from pathlib import Path
from typing import Any, BinaryIO

from tapewyrm_archive import qic117, twrf, twti, twvl
from tapewyrm_archive.errors import MalformedFileError, TruncatedFileError
from tapewyrm_archive.qic80date import format_short_date
from tapewyrm_archive.types import TapeFormat

log = logging.getLogger(__name__)

# Every format shares the same 10-byte preamble: magic, u16 version, u32
# header length (TWS-1 section 3, TWS-2 section 3, TWS-3 section 2.1).
_PREAMBLE = struct.Struct("<4sHI")

# Versions each format's reader accepts, so inspect refuses what they refuse.
_VERSIONS: dict[str, tuple[int, ...]] = {
    "TWRF": twrf.READABLE_VERSIONS,
    "TWTI": (twti.VERSION,),
    "TWTZ": (twti.VERSION,),
    "TWVL": (twvl.VERSION,),
}

# Compressed bytes fed to the zstd decompressor per read. Small, because the
# whole point is to stop soon after the segment table: the preamble, header
# and table of a 59,000-segment image decompress to ~480 KB, and the zero
# slots after them compress so well that a big read would mostly be wasted.
_ZSTD_READ = 64 * 1024

_MB = 1_000_000  # decimal megabytes, as the rest of the package logs them


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Inspection:
    """What :func:`inspect` learned about one file, header first.

    ``header_bytes`` are the stored JSON bytes, untouched (whitespace and key
    order included); ``header`` is the same JSON parsed. ``body_size`` is the
    length on disk behind the header: the flux body of a TWRF, the volume
    bytes of a TWVL, the segment table plus data area of a TWTI, and None for
    a TWTZ (whose length on disk is compressed). ``image_size`` is a tape
    image's logical TWTI length as its header and table imply it -- for a
    TWTZ, its decompressed length. ``segment_counts`` maps each
    ``SegmentState`` name present to how many segments are in it, in state
    order. ``bytes_read`` is how much of the file on disk was read, which for
    a TWTZ is the compressed prefix that held the header and table.
    """

    path: Path
    kind: str  # "TWRF", "TWTI", "TWTZ" or "TWVL"
    version: int
    header_len: int
    header_bytes: bytes
    header: dict[str, Any]
    file_size: int
    bytes_read: int
    body_size: int | None = None
    image_size: int | None = None
    segment_counts: dict[str, int] | None = None

    @property
    def header_text(self) -> str:
        """The stored header as text, for printing it verbatim (``--json``)."""
        return self.header_bytes.decode("utf-8", errors="replace")


@dataclass(frozen=True)
class Section:
    """One titled block of a human-readable description: (label, value) rows."""

    title: str
    rows: list[tuple[str, str]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Reading: magic, then a growing prefix of the file (or of a TWTZ's stream)
# ---------------------------------------------------------------------------


def sniff(path: str | PathLike[str]) -> str | None:
    """Which Tapewyrm format ``path`` holds by its magic, or None.

    ``"TWTI"`` and ``"TWTZ"`` come from :func:`twti.sniff` (any zstd stream
    counts as TWTZ until its content is checked); ``"TWRF"`` and ``"TWVL"``
    from their own magics. The file name is never consulted.
    """
    path = Path(path)
    image = twti.sniff(path)
    if image is not None:
        return image
    with path.open("rb") as f:
        magic = f.read(4)
    for kind, expected in (("TWRF", twrf.MAGIC), ("TWVL", twvl.MAGIC)):
        if magic == expected:
            return kind
    log.debug("%s: magic %r is not TWRF, TWTI, TWTZ or TWVL", path, magic)
    return None


class _Prefix:
    """The leading bytes of a file, or of a TWTZ's decompressed stream, on demand.

    :meth:`ensure` grows ``buf`` to at least ``n`` bytes and no further (the
    zstd decompressor's ``max_length`` stops it exactly at ``n``), so how
    much is read is set by what the caller asked for, not by the file. It
    stops short only at the end of the data; the caller turns a short buffer
    into the format's own truncation error, as the readers do.

    A TWTZ written by ``TapeImage.save`` is one zstd frame, but ``zstd -d``
    accepts concatenated frames, so a finished frame with input left over
    starts a fresh decompressor on the rest rather than ending the stream.
    """

    def __init__(self, f: BinaryIO, path: Path, *, compressed: bool) -> None:
        self._f = f
        self._path = path
        self.buf = bytearray()
        self.bytes_read = 0
        self._done = False
        self._pending = b""  # compressed input left over after a finished frame
        self._dec: Any = None
        if compressed:
            from tapewyrm_archive._zstd import zstd

            self._zstd = zstd
            self._dec = zstd.ZstdDecompressor()

    def ensure(self, n: int) -> bytes:
        """Read until ``buf`` holds ``n`` bytes or the data ends; return ``buf``."""
        while len(self.buf) < n and not self._done:
            if self._dec is None:
                chunk = self._f.read(n - len(self.buf))
                self.bytes_read += len(chunk)
                if not chunk:
                    log.debug("%s: end of file after %d bytes", self._path, len(self.buf))
                    self._done = True
                self.buf += chunk
            else:
                self._inflate(n)
        return bytes(self.buf)

    def _inflate(self, n: int) -> None:
        """One step of decompression toward ``n`` bytes of output."""
        dec = self._dec
        data = b""
        if dec.needs_input:
            data, self._pending = self._pending, b""
            if not data:
                data = self._f.read(_ZSTD_READ)
                self.bytes_read += len(data)
            if not data:
                # Either a clean end after a whole frame or a stream cut
                # mid-frame; both just mean "no more bytes" to the caller.
                log.debug("%s: zstd input ends at %d decompressed bytes", self._path, len(self.buf))
                self._done = True
                return
        try:
            self.buf += dec.decompress(data, max_length=n - len(self.buf))
        except self._zstd.ZstdError as exc:
            log.debug("%s: zstd stream is corrupt: %r", self._path, exc)
            raise MalformedFileError(
                self._path, "TWTZ", f"the zstd stream is corrupt ({exc})"
            ) from exc
        if dec.eof:
            log.debug("%s: zstd frame ended; continuing with any next frame", self._path)
            self._pending = dec.unused_data
            self._dec = self._zstd.ZstdDecompressor()


def _preamble(prefix: _Prefix, path: Path, kind: str) -> tuple[int, int, bytes]:
    """Check the preamble and read the header; return (version, header_len, header bytes).

    The version is checked *before* the header length is trusted, so a file
    of an unreadable version is refused without reading (or decompressing)
    however many bytes its length field claims.
    """
    note = " (decompressed)" if kind == "TWTZ" else ""
    buf = prefix.ensure(_PREAMBLE.size)
    if len(buf) < _PREAMBLE.size:
        log.debug("%s: %d bytes is shorter than the preamble; refusing", path, len(buf))
        raise TruncatedFileError(path, kind, "preamble", _PREAMBLE.size, len(buf), note=note)
    _, version, header_len = _PREAMBLE.unpack_from(buf)
    readable = _VERSIONS[kind]
    if version not in readable:
        log.debug("%s: version %d not in %s; refusing", path, version, readable)
        raise MalformedFileError(
            path,
            kind,
            f"it is version {version}, and only version {readable[-1]} is read",
        )
    header_end = _PREAMBLE.size + header_len
    buf = prefix.ensure(header_end)
    if len(buf) < header_end:
        log.debug("%s: header needs %d bytes, have %d; refusing", path, header_end, len(buf))
        raise TruncatedFileError(path, kind, "JSON header", header_end, len(buf), note=note)
    return version, header_len, buf[_PREAMBLE.size : header_end]


# ---------------------------------------------------------------------------
# inspect()
# ---------------------------------------------------------------------------


def inspect(path: str | PathLike[str]) -> Inspection:
    """Identify ``path`` by magic and read its header (module docstring).

    Raises ``ValueError`` when the file is not one of the four formats,
    :class:`TruncatedFileError` when it is shorter than its own preamble,
    header (or a tape image's segment table, or a TWVL's ``volume_size``)
    say, and :class:`MalformedFileError` when a header breaks its spec.
    """
    path = Path(path)
    log.debug("inspecting %s", path)
    kind = sniff(path)
    if kind is None:
        raise ValueError(
            f"{path}: not a Tapewyrm file (it does not start with the TWRF, TWTI, "
            "TWVL or zstd magic)"
        )
    file_size = path.stat().st_size
    log.debug("%s: %s by magic, %d bytes on disk", path, kind, file_size)
    with path.open("rb") as f:
        prefix = _Prefix(f, path, compressed=kind == "TWTZ")
        version, header_len, header_bytes = _preamble(prefix, path, kind)
        header_end = _PREAMBLE.size + header_len
        # What every format shares; each branch fills in the rest with replace().
        common = Inspection(
            path=path,
            kind=kind,
            version=version,
            header_len=header_len,
            header_bytes=header_bytes,
            header={},
            file_size=file_size,
            bytes_read=0,
        )
        if kind == "TWRF":
            # The reader's own checks, run on the bytes we already hold.
            twrf._read_header(io.BytesIO(prefix.ensure(header_end)), str(path))
            return replace(
                common,
                header=json.loads(header_bytes),
                bytes_read=prefix.bytes_read,
                body_size=file_size - header_end,
            )
        if kind == "TWVL":
            header = twvl._parse_header(path, header_bytes)
            volume_size = header["volume_size"]  # _parse_header checked it
            if file_size - header_end < volume_size:
                log.debug("%s: volume bytes short of %d; refusing", path, volume_size)
                raise TruncatedFileError(
                    path, "TWVL", "volume bytes", header_end + volume_size, file_size
                )
            return replace(
                common,
                header=header,
                bytes_read=prefix.bytes_read,
                body_size=file_size - header_end,
            )
        return _inspect_image(prefix, path, kind, file_size, header_end, common)


def _inspect_image(
    prefix: _Prefix,
    path: Path,
    kind: str,
    file_size: int,
    header_end: int,
    common: Inspection,
) -> Inspection:
    """The TWTI/TWTZ part of :func:`inspect`: read on through the segment table."""
    header_bytes = common.header_bytes
    # Peek at segment_count only to know how far to read; whether the header
    # is valid at all is TapeImage._parse's call, made just below.
    count = None
    try:
        peeked = json.loads(header_bytes)
        if isinstance(peeked, dict) and type(peeked.get("segment_count")) is int:
            count = max(0, peeked["segment_count"])
    except ValueError:
        log.debug("%s: header does not parse; letting the TWTI reader say why", path)
    if count is not None:
        prefix.ensure(header_end + count * twti._ENTRY.size)
    image = twti.TapeImage._parse(
        path, bytes(prefix.buf), compressed=kind == "TWTZ", data_area=False
    )
    if image is None:
        # Our magic and version, yet _parse says "not a TWTI": only possible
        # for a TWTZ whose stream holds something else.
        log.debug("%s: decompressed stream is not a TWTI image; refusing", path)
        raise ValueError(f"{path}: a zstd stream, but not a TWTI v{twti.VERSION} image inside")
    table_end = header_end + len(image.entries) * twti._ENTRY.size
    image_size = table_end + len(image.entries) * twti.SEGMENT_STRIDE
    if kind == "TWTI" and file_size < image_size:
        # TWS-2 9.2: judged by length, so a sparse file of the right length
        # is complete however little of it is allocated.
        log.debug(
            "%s: data area needs %d bytes, file has %d; refusing", path, image_size, file_size
        )
        raise TruncatedFileError(path, "TWTI", "segment data area", image_size, file_size)
    raw_counts = image.counts()
    counts = {
        state.name: raw_counts[state.name]
        for state in twti.SegmentState
        if state.name in raw_counts
    }
    return replace(
        common,
        header=image.header,
        bytes_read=prefix.bytes_read,
        body_size=file_size - header_end if kind == "TWTI" else None,
        image_size=image_size,
        segment_counts=counts,
    )


# ---------------------------------------------------------------------------
# Value formatting
# ---------------------------------------------------------------------------


def _int(value: object) -> int | None:
    """``value`` if it is a JSON integer (not a bool), else None."""
    return value if type(value) is int else None


def _number(value: object) -> str:
    """An integer with thousands separators; anything else as it is."""
    number = _int(value)
    return f"{number:,}" if number is not None else _text(value)


def _text(value: object) -> str:
    """A header value as display text: None reads as "not recorded"."""
    if value is None:
        return "not recorded"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, str):
        return value if value else "(empty)"
    return str(value)


def _size(value: object) -> str:
    """Bytes with separators, plus decimal MB once it is past a megabyte."""
    number = _int(value)
    if number is None:
        return _text(value)
    if number < _MB:
        return f"{number:,} bytes"
    return f"{number:,} bytes ({number / _MB:,.1f} MB)"


def _share(part: int, whole: int) -> str:
    """``part`` with separators and its percentage of ``whole``."""
    return f"{part:,} ({_percent(part, whole)})"


def _percent(part: int, whole: int) -> str:
    return f"{100 * part / whole:.2f}%" if whole > 0 else "n/a"


def _byte(value: int) -> str:
    return f"0x{value:02x}"


def _geometry(tracks: object, segments: object) -> str:
    """ "40 tracks x 1,475 segments = 59,000" -- or the raw values if unset."""
    track_count, per_track = _int(tracks), _int(segments)
    if not track_count or not per_track:
        return f"not recorded ({_text(tracks)} x {_text(segments)})"
    return f"{track_count:,} tracks x {per_track:,} segments = {track_count * per_track:,}"


def _short_date(packed: object) -> str:
    """A QIC-80 packed short date (TWS-2 4.3) as ISO-style text, raw word alongside."""
    value = _int(packed)
    if value is None:
        return _text(packed)
    when = format_short_date(value)
    return f"{when} (0x{value:08x})" if when else f"none (0x{value:08x})"


# --- the drive's QIC-117 report bytes (TWS-1 4.3) ---


def _drive_status(value: object) -> str:
    raw = _int(value)
    if raw is None:
        return _text(value)
    status = qic117.DriveStatus.decode(raw)
    flags = [
        name
        for name, on in (
            ("ready", status.ready),
            ("error", status.error),
            ("cartridge present", status.cartridge_present),
            ("write protected", status.write_protect),
            ("new cartridge", status.new_cartridge),
            ("referenced", status.referenced),
            ("at BOT", status.at_bot),
            ("at EOT", status.at_eot),
        )
        if on
    ]
    return f"{_byte(raw)} -> {', '.join(flags) if flags else 'no flags set'}"


def _drive_config(value: object) -> str:
    raw = _int(value)
    if raw is None:
        return _text(value)
    config = qic117.DriveConfig.decode(raw)
    rate = f"{config.rate_kbps:,} kbit/s"
    if config.rate_ambiguous:
        rate += " (or 4 Mbit/s, by drive type)"
    parts = [rate]
    if config.qic80_mode:
        parts.append("QIC-80 mode")
    if config.extra_length:
        parts.append("extra-length tape")
    return f"{_byte(raw)} -> {', '.join(parts)}"


def _vendor(value: object) -> str:
    raw = _int(value)
    if raw is None:
        return _text(value)
    make, model, name = qic117.decode_vendor_id(raw)
    if raw in qic117.LEGACY_VENDOR_IDS:
        return f"0x{raw:04x} -> {name}"
    return f"0x{raw:04x} -> {name} (make {make}, model {model})"


def _tape_status(value: object) -> str:
    raw = _int(value)
    if raw is None:
        return _text(value)
    status = qic117.TapeStatus.decode(raw)
    tape_type = qic117.TAPE_TYPES.get(status.tape_type, f"tape type {status.tape_type}")
    wide = ", wide" if status.wide else ""
    return f"{_byte(raw)} -> {status.format.name}, {tape_type}{wide}"


def _rom(value: object) -> str:
    raw = _int(value)
    return _text(value) if raw is None else f"{_byte(raw)} ({raw})"


def _tape_format(value: object) -> str:
    raw = _int(value)
    if raw is None or raw not in TapeFormat._value2member_map_:
        return _text(value)
    return f"{TapeFormat(raw).name} ({raw})"


def _drive_rows(drive: dict[str, Any], *, with_rate: bool) -> list[tuple[str, str]]:
    """The drive report bytes, decoded; shared by TWRF headers and ``drive`` objects."""
    serial = drive.get("device_serial")
    rows = [
        ("Capture device", _text(serial) if serial else "unknown"),
        ("Drive status", _drive_status(drive.get("drive_status"))),
        ("Drive config", _drive_config(drive.get("drive_config"))),
        ("ROM version", _rom(drive.get("drive_rom"))),
        ("Vendor", _vendor(drive.get("drive_vendor_id"))),
        ("Tape status", _tape_status(drive.get("tape_status"))),
    ]
    if with_rate:
        rows.append(("Capture rate", _kbps(drive.get("rate_kbps"))))
        rows.append(("Firmware commit", _text(drive.get("firmware_commit"))))
    return rows


def _kbps(value: object) -> str:
    return f"{_number(value)} kbit/s" if _int(value) is not None else _text(value)


# ---------------------------------------------------------------------------
# describe()
# ---------------------------------------------------------------------------


def describe(inspection: Inspection) -> list[Section]:
    """The inspection as titled sections of formatted (label, value) rows.

    Header values are shown as stored (ISO date strings untouched); raw
    report bytes are shown with their decoding. Members a header lacks or
    holds with an unexpected type show as their text rather than failing:
    the readers already refused what they cannot use, and a description
    should still describe the rest.
    """
    log.debug("describing %s (%s)", inspection.path, inspection.kind)
    if inspection.kind == "TWRF":
        return _describe_twrf(inspection)
    if inspection.kind == "TWVL":
        return _describe_twvl(inspection)
    return _describe_image(inspection)


def _file_section(inspection: Inspection, *more: tuple[str, str]) -> Section:
    rows = [
        ("Path", str(inspection.path)),
        ("Format", f"{inspection.kind} version {inspection.version}"),
        ("File size", _size(inspection.file_size)),
        ("Header", _size(inspection.header_len)),
        *more,
    ]
    return Section("File", rows)


def _describe_twrf(inspection: Inspection) -> list[Section]:
    header = inspection.header
    direction = _text(header.get("direction"))
    if header.get("physical_reverse") is True:
        direction += ", read physically reversed"
    dirty = header.get("firmware_dirty")
    firmware = _text(header.get("firmware_commit"))
    if dirty is True:
        firmware += " (dirty)"
    return [
        _file_section(inspection, ("Flux body", _size(inspection.body_size))),
        Section(
            "Capture",
            [
                ("Track", f"{_number(header.get('track'))} ({direction})"),
                ("Pass", _number(header.get("pass_id"))),
                ("Captured", _text(header.get("utc"))),
                ("Tape format", _tape_format(header.get("tape_format"))),
                ("Rate", _kbps(header.get("rate_kbps"))),
                ("Sample clock", f"{_number(header.get('sample_clock_hz'))} Hz"),
                (
                    "Geometry",
                    _geometry(header.get("tracks"), header.get("segments_per_track")),
                ),
                ("Sectors per segment", _number(header.get("sectors_per_segment"))),
            ],
        ),
        Section("Drive", _drive_rows(header, with_rate=False)),
        Section(
            "Software",
            [("tw commit", _text(header.get("tw_commit"))), ("Firmware commit", firmware)],
        ),
    ]


def _describe_image(inspection: Inspection) -> list[Section]:
    header = inspection.header
    count = _int(header.get("segment_count")) or 0
    if inspection.kind == "TWTZ":
        sizes = [("Image size", f"{_size(inspection.image_size)} decompressed")]
    else:
        sizes = [("Image size", _size(inspection.image_size))]
    sections = [
        _file_section(
            inspection,
            *sizes,
            ("Created", _text(header.get("created"))),
            ("tw commit", _text(header.get("tw_commit"))),
        )
    ]

    geometry = header.get("geometry")
    if isinstance(geometry, dict):
        shape = _geometry(geometry.get("tracks"), geometry.get("segments_per_track"))
        tracks, per_track = _int(geometry.get("tracks")), _int(geometry.get("segments_per_track"))
        if tracks and per_track and tracks * per_track != count:
            shape += f" (but segment_count is {count:,})"
        sections.append(
            Section(
                "Geometry",
                [
                    ("Segments", shape),
                    ("Sectors per segment", _number(geometry.get("sectors_per_segment"))),
                    ("Floppy tracks per side", _number(geometry.get("ftk_per_side"))),
                    ("Segment stride", _size(header.get("segment_stride"))),
                ],
            )
        )

    counts = inspection.segment_counts or {}
    rows = [(name.lower(), _share(n, count)) for name, n in counts.items()]
    sections.append(Section("Segments", rows or [("segments", "none")]))

    fpr = header.get("qic80_header")
    if isinstance(fpr, dict):
        sections.append(_fpr_section(fpr))

    drive = header.get("drive")
    if isinstance(drive, dict):
        sections.append(Section("Drive", _drive_rows(drive, with_rate=True)))

    sources = header.get("sources")
    if isinstance(sources, list) and sources:
        sections.append(Section("Sources", [_source_row(source) for source in sources]))
    return sections


def _fpr_section(fpr: dict[str, Any]) -> Section:
    """The header segment's format parameter record (TWS-2 4.3)."""
    revision = _int(fpr.get("revision"))
    revision_text = {0x0D: "Rev M", 0x0C: "Rev L", 0: "before Rev L"}.get(
        revision if revision is not None else -1, ""
    )
    rows = [
        ("Tape name", _text(fpr.get("tape_name"))),
        ("Signature", "valid" if fpr.get("valid_signature") is True else "not valid"),
        ("Format code", _number(fpr.get("format_code"))),
        (
            "QIC-80 revision",
            f"{_rom(fpr.get('revision'))} {revision_text}".rstrip(),
        ),
        ("Geometry", _geometry(fpr.get("tracks"), fpr.get("segments_per_track"))),
        (
            "Header segments",
            f"{_number(fpr.get('header_seg'))} and {_number(fpr.get('dup_header_seg'))}",
        ),
        (
            "Data segments",
            f"{_number(fpr.get('first_data_seg'))} to {_number(fpr.get('last_data_seg'))}",
        ),
        ("Formatted", _short_date(fpr.get("format_date"))),
        ("First formatted", _short_date(fpr.get("initial_format_date"))),
        ("Last written", _short_date(fpr.get("write_date"))),
        ("Named", _short_date(fpr.get("name_date"))),
        ("Format count", _number(fpr.get("format_count"))),
        ("Segments written", _number(fpr.get("segments_written"))),
        ("Re-format error", _text(fpr.get("reformat_error"))),
        ("Manufacturer", _text(fpr.get("manufacturer"))),
        ("Lot code", _text(fpr.get("lot_code"))),
    ]
    return Section("Tape header (QIC-80 format parameters)", rows)


def _source_row(source: object) -> tuple[str, str]:
    """One TWS-2 4.5 source as "file: track 3 reverse, pass 1, utc; verified; N sectors"."""
    if not isinstance(source, dict):
        return ("source", _text(source))
    stored = source.get("twrf")
    capture: dict[str, Any] = stored if isinstance(stored, dict) else {}
    verified = "verified" if source.get("verified") is True else "NOT verified"
    value = (
        f"track {_number(capture.get('track'))} {_text(capture.get('direction'))}, "
        f"pass {_number(capture.get('pass_id'))}, {_text(capture.get('utc'))}; "
        f"{verified}; {_number(source.get('sectors'))} sectors"
    )
    return (_text(source.get("file")), value)


# VTBL byte 56 flag bits (TWS-3 3.2, QIC-80 section 8).
_VTBL_FLAGS = {0: "vendor specific", 4: "segment spanning", 5: "directory last"}


def _flags(value: object) -> str:
    raw = _int(value)
    if raw is None:
        return _text(value)
    names = [_VTBL_FLAGS.get(bit, f"bit {bit}") for bit in range(8) if raw >> bit & 1]
    return f"{_byte(raw)} -> {', '.join(names) if names else 'none'}"


def _vtbl_date(vtbl: dict[str, Any]) -> str:
    decoded = vtbl.get("date_decoded")
    if isinstance(decoded, list) and len(decoded) == 6 and all(type(x) is int for x in decoded):
        packed = _int(vtbl.get("date"))
        raw = f" (0x{packed:08x})" if packed is not None else ""
        return "{:04d}-{:02d}-{:02d} {:02d}:{:02d}:{:02d}".format(*decoded) + raw
    return _short_date(vtbl.get("date"))


def _describe_twvl(inspection: Inspection) -> list[Section]:
    header = inspection.header
    volume_size = _int(header.get("volume_size")) or 0
    sections = [
        _file_section(inspection, ("Volume bytes", _size(volume_size))),
        Section(
            "Volume",
            [
                ("Index", _number(header.get("volume_index"))),
                ("Tape name", _text(header.get("tape_name"))),
                ("Source image", _text(header.get("source_image"))),
                ("Data section", _size(header.get("data_section_size"))),
                ("Directory section", _size(header.get("dir_section_size"))),
                ("Directory offset", _number(header.get("directory_offset"))),
            ],
        ),
    ]

    vtbl = header.get("vtbl")
    if isinstance(vtbl, dict):
        start, end = _int(vtbl.get("start_seg")), _int(vtbl.get("end_seg"))
        span = (
            f"{start:,} to {end:,} ({end - start + 1:,} segments)"
            if start is not None and end is not None and end >= start
            else f"{_text(vtbl.get('start_seg'))} to {_text(vtbl.get('end_seg'))}"
        )
        sections.append(
            Section(
                "Volume table entry",
                [
                    ("Description", _text(vtbl.get("description"))),
                    ("Signature", _text(vtbl.get("signature"))),
                    ("Segments", span),
                    ("Date", _vtbl_date(vtbl)),
                    ("Flags", _flags(vtbl.get("flags"))),
                    ("OS type", _number(vtbl.get("os_type"))),
                    ("Compressed", _text(vtbl.get("compressed"))),
                    ("Compression code", _number(vtbl.get("compression_code"))),
                    ("Cartridge sequence", _number(vtbl.get("multi_cartridge_seq"))),
                    ("Source label", _text(vtbl.get("source_label"))),
                ],
            )
        )

    holes = header.get("holes", [])
    hole_bytes = sum(b - a for a, b in holes if b > a)  # _parse_header checked the shape
    lost = header.get("lost_segments")
    lost_list = [n for n in lost if type(n) is int] if isinstance(lost, list) else []
    shown = ", ".join(f"{n:,}" for n in lost_list[:10])
    if len(lost_list) > 10:
        shown += f", ... {len(lost_list) - 10:,} more"
    sections.append(
        Section(
            "Recovery",
            [
                (
                    "Holes",
                    f"{len(holes):,} ranges, {_size(hole_bytes)}, "
                    f"{_percent(hole_bytes, volume_size)} of the volume"
                    if holes
                    else "none",
                ),
                (
                    "Lost segments",
                    f"{len(lost_list):,}: {shown}" if lost_list else "none",
                ),
            ],
        )
    )

    drive = header.get("drive")
    if isinstance(drive, dict):
        sections.append(Section("Drive", _drive_rows(drive, with_rate=True)))
    return sections

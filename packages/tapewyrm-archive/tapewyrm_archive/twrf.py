"""RawFluxCapture and the TWRF flux-body parser (DESIGN.md §7.1, §6A.6, §13.4).

The byte layout is owned by ``docs/spec/twrf.md`` (TWS-1); this module is its
reference reader and writer.

On-disk format (deliberately dead simple and lossless):

    magic   "TWRF" (4 bytes)
    version u16 little-endian
    hlen    u32 little-endian  (length of the JSON header that follows)
    header  JSON (the CaptureHeader fields)
    flux    the verbatim device stream (TWS-1 §5): GW flux encoding, markers inside

No re-encoding: the flux bytes are stored exactly as they came off the device.

The flux body (TWS-1 §5, §6)
----------------------------
The body is Greaseweazle's ``CMD_READ_FLUX`` stream (``rdata_encode_flux()`` in
firmware/src/floppy.c) with Tapewyrm's markers riding GW's opcode escape:

    1..249                 one interval of that many ticks
    250..254, b            two-byte interval; ``b`` is 1..255 and CAN be 0xFF
    FF 01 N28              INDEX, N28 ticks after GW's sample cursor
    FF 02 N28 F9           long interval (N28 + 249), counted as flux data
    FF 02 N28              dead time (no flux), added to the next interval
    FF F0..F4 len payload  Tapewyrm marker (WireMarker)
    00                     end of stream

Because a two-byte interval may end in 0xFF and INDEX/SPACE carry four N28
argument bytes, the body can only be read by walking it token by token. Every
helper here (``iter_markers``, ``flux_data_only``, ``RawFluxCapture.markers``
/ ``segments`` / ``end_marker`` / ``verify``) goes through the ONE tokenizer,
:func:`parse_body`, which ``tapewyrm.codec.gwstream`` also uses for ``tw
convert`` and ``tw dump``. (An earlier version of this module scanned for 0xFF
and assumed a "0xFF 0xFF" stuffing rule that the device never used; on a real
capture that miscounted the flux data and failed ``verify``.)

Marker payload layouts (little-endian), shared with firmware/src/qic/qic.c:
    SESSION_START : rate:u16, clock:u32, tpt:u16, direction:u8, pass_id:u16
    SEGMENT       : ticks:u32, index:u32
    EVENT         : code:u8
    END           : reason:u8, flux_count:u32, byte_count:u32, checksum:u32
    HEARTBEAT     : (empty)
"""

from __future__ import annotations

import dataclasses
import json
import logging
import struct
from collections.abc import Iterable, Iterator
from enum import IntEnum
from pathlib import Path
from typing import BinaryIO, NamedTuple

from tapewyrm_archive.errors import MalformedFileError, TruncatedFileError
from tapewyrm_archive.progress import NULL_PROGRESS, Progress
from tapewyrm_archive.types import CaptureHeader, Direction, Marker, MarkerKind, TapeFormat

log = logging.getLogger(__name__)

MAGIC = b"TWRF"
# v2 (2026-10-01): the header gains the drive's raw QIC-117 report bytes
# (status, configuration -- hence the bit rate --, ROM, vendor ID, tape status)
# and the tw/firmware commits. Version 2 is the only version read: version 1
# predates the release, nobody holds v1 captures but us, and the cure for one
# is to dump the tape again (no compatibility before release, STYLE.md §2).
FORMAT_VERSION = 2
READABLE_VERSIONS = (2,)
_PREAMBLE = struct.Struct("<4sHI")  # magic, version, header length
ESC = 0xFF  # GW's opcode escape: introduces FLUXOP_* and Tapewyrm markers
_FLUXOP_INDEX = 1  # GW cdc_acm_protocol.h: FF 01 N28
_FLUXOP_SPACE = 2  # GW cdc_acm_protocol.h: FF 02 N28 (and FF 02 N28 F9 in-loop)
_LONG_TAIL = 249  # the byte after an in-loop SPACE that makes it a long interval

# Stream bytes between progress updates: often enough for a smooth bar, rare
# enough that the update costs nothing next to the per-byte loop.
_PROGRESS_EVERY = 1 << 20


class WireMarker(IntEnum):
    """Marker opcodes as they sit in a TWRF flux run (after ``ESC``).

    These are the firmware's ``protocol.Marker`` codes. Once written into a
    file they are part of the *file format*, so the archive package owns its
    own copy rather than importing the generated, hardware-side
    ``tapewyrm.link.protocol``. The host package's tests assert the two
    tables are identical, so firmware and file format cannot drift apart
    silently.
    """

    SESSION_START = 0xF0
    SEGMENT = 0xF1
    EVENT = 0xF2
    END = 0xF3
    HEARTBEAT = 0xF4


# WireMarker (on-wire 0xF0..) <-> types.MarkerKind (semantic 0..4)
_WIRE_TO_KIND = {
    WireMarker.SESSION_START: MarkerKind.SESSION_START,
    WireMarker.SEGMENT: MarkerKind.SEGMENT,
    WireMarker.EVENT: MarkerKind.EVENT,
    WireMarker.END: MarkerKind.END,
    WireMarker.HEARTBEAT: MarkerKind.HEARTBEAT,
}
_KIND_TO_WIRE = {v: k for k, v in _WIRE_TO_KIND.items()}


# ---------------------------------------------------------------------------
# Stream encoders (the firmware's byte forms; used to synthesize fixtures)
# ---------------------------------------------------------------------------


def _n28(value: int) -> bytes:
    """GW's N28: 28 bits, 7 per byte, low group first, bit 0 of each byte set."""
    return bytes(
        [
            (1 | (value << 1)) & 0xFF,
            (1 | (value >> 6)) & 0xFF,
            (1 | (value >> 13)) & 0xFF,
            (1 | (value >> 20)) & 0xFF,
        ]
    )


def encode_interval(ticks: int) -> bytes:
    """One flux transition exactly as ``rdata_encode_flux()`` writes it.

    1..249 is one byte; 250..1524 is two bytes whose second byte is
    ``1 + (ticks - 250) % 255`` (so 0xFF whenever that remainder is 254);
    anything longer is ``FF 02 N28(ticks - 249) F9``.
    """
    if not 1 <= ticks < (1 << 28) + _LONG_TAIL:
        log.debug("interval of %d ticks is outside 1..2^28+248; refusing", ticks)
        raise ValueError(f"flux interval {ticks} ticks cannot be encoded")
    if ticks < 250:
        return bytes([ticks])
    high = (ticks - 250) // 255
    if high < 5:
        return bytes([250 + high, 1 + (ticks - 250) % 255])
    return bytes([ESC, _FLUXOP_SPACE]) + _n28(ticks - _LONG_TAIL) + bytes([_LONG_TAIL])


def encode_index(ticks: int) -> bytes:
    """A GW INDEX opcode: ``FF 01 N28``, ``ticks`` after the sample cursor."""
    return bytes([ESC, _FLUXOP_INDEX]) + _n28(ticks)


def encode_space(ticks: int) -> bytes:
    """A GW dead-time SPACE opcode: ``FF 02 N28`` (no transition)."""
    return bytes([ESC, _FLUXOP_SPACE]) + _n28(ticks)


def frame_marker(kind: MarkerKind, payload: bytes = b"") -> bytes:
    """Encode one marker as an opcode-escape frame: ``FF code len payload``."""
    if len(payload) > 0xFF:
        log.debug("marker %s payload is %d bytes > 255; refusing", kind.name, len(payload))
        raise ValueError("marker payload too long for u8 length field")
    return bytes([ESC, int(_KIND_TO_WIRE[kind]), len(payload)]) + payload


# ---------------------------------------------------------------------------
# Flux-body parser (TWS-1 §5.5): the one tokenizer of the device stream
# ---------------------------------------------------------------------------
#
# Why one parser, and why it lives here: the TWRF body IS the GW stream, so the
# archive package (which tapewyrm-cli depends on, never the reverse) owns the
# tokenizer, and ``tapewyrm.codec.gwstream.parse`` is a thin re-export of
# :func:`parse_body`. The alternative -- a second, simpler tokenizer here and
# gwstream keeping its own hot loop -- would leave two parsers to drift apart,
# which is exactly how the old 0xFF-scanning helpers went wrong. Sharing costs
# nothing on ``tw convert``: the loop below is gwstream's loop moved, with the
# extra bookkeeping (``skips``, marker offsets) only in the rare opcode
# branches, never on the one- and two-byte interval paths that carry ~all the
# bytes. Measured on captures/3m-unknown-1/track-00.twrf (2026-10-03),
# ``tapewyrm.image.convert.decode_capture`` best-of-3: 15.97 s with the loop in
# gwstream, 16.03 s with it here (+0.4 %, inside the 2 % budget set for this).


@dataclasses.dataclass(frozen=True)
class StreamEnd:
    """The firmware's END marker: why the run stopped + its accounting."""

    reason: int  # protocol.EndReason
    flux_count: int
    byte_count: int
    checksum: int


class BodyMarker(NamedTuple):
    """One marker as found in the body, still raw (payload undecoded)."""

    code: WireMarker
    payload: bytes
    interval: int  # number of intervals decoded before it
    offset: int  # byte offset of its 0xFF within the body


@dataclasses.dataclass
class ParsedStream:
    """Everything one walk of a flux body yields (TWS-1 §5.5, §7.2)."""

    intervals: list[int] = dataclasses.field(default_factory=list)  # ticks between transitions
    # Absolute tick time of each INDEX pulse: the stream's sample cursor (sum of
    # intervals plus any dead time since the last one) plus the opcode's N28.
    index_ticks: list[int] = dataclasses.field(default_factory=list)
    markers: list[BodyMarker] = dataclasses.field(default_factory=list)
    data_bytes: int = 0  # bytes the firmware counts for END (in-loop flux bytes)
    checksum: int = 0  # additive checksum of those bytes, & 0xFFFFFFFF
    terminated: bool = False  # saw the trailing NUL
    sample_clock_hz: int = 72_000_000  # from SESSION_START when present
    # Ticks of trailing dead time: SPACE filler GW emits while no flux arrives,
    # not yet attached to an interval when the stream ended. A capture with no
    # transitions at all is ALL dead time, which is how a silent RDATA shows up.
    trailing_ticks: int = 0
    end: StreamEnd | None = None
    # Byte spans [start, stop) inside the parsed region that are NOT flux data
    # (INDEX, dead-time SPACE, markers), in order. Everything else before
    # ``stop_offset`` is flux data, which is how flux_data_only() rebuilds the
    # exact bytes END counted without a per-byte append in the hot loop.
    skips: list[tuple[int, int]] = dataclasses.field(default_factory=list)
    stop_offset: int = 0  # where the walk stopped: the NUL, a cut record, or EOF

    @property
    def verified(self) -> bool:
        """True when our parse agrees with the firmware's END accounting."""
        return (
            self.end is not None
            and self.end.flux_count == len(self.intervals)
            and self.end.byte_count == self.data_bytes
            and self.end.checksum == self.checksum
        )

    @property
    def duration_s(self) -> float:
        return sum(self.intervals) / self.sample_clock_hz

    @property
    def span_s(self) -> float:
        """Stream time including trailing dead time (silence counts too)."""
        return (sum(self.intervals) + self.trailing_ticks) / self.sample_clock_hz


def _n28_at(b: bytes, i: int) -> int:
    return (
        (b[i] >> 1)
        | ((b[i + 1] & 0xFE) << 6)
        | ((b[i + 2] & 0xFE) << 13)
        | ((b[i + 3] & 0xFE) << 20)
    )


def parse_body(blob: bytes, *, progress: Progress = NULL_PROGRESS) -> ParsedStream:
    """Walk a TWRF flux body token by token. Raises ``ValueError`` on an unknown opcode.

    ``progress`` gets one "parsing flux stream" task in bytes. This loop runs
    once per stream byte (tens of millions per track), so the progress update
    is hoisted out of it: the scan runs in slices of ``_PROGRESS_EVERY``
    bytes (an inner ``while i < limit``) and the bar moves once per slice.
    The inner loop's own stop conditions set ``stop`` and break; the outer
    loop then breaks too.

    Ambiguity inherited from GW (TWS-1 §5.2): a dead-time SPACE followed by a
    genuine 249-tick interval is byte-identical to an in-loop long interval.
    Both decode to the same total time; only the END accounting could disagree.
    """
    out = ParsedStream()
    iv = out.intervals
    skips = out.skips
    i, n, pending, t, csum, nbytes = 0, len(blob), 0, 0, 0, 0
    markers = {m.value: m for m in WireMarker}
    # Dead-time SPACEs are common on a real capture; tally, don't log each.
    n_dead = 0
    stop = False
    log.debug("parsing %d-byte flux body", n)
    with progress.task("parsing flux stream", total=n, unit="bytes") as bar:
        while i < n and not stop:
            limit = min(n, i + _PROGRESS_EVERY)
            while i < limit:
                c = blob[i]
                if c == 0:
                    log.debug("end-of-stream NUL at byte %d of %d; stopping", i, n)
                    out.terminated = True
                    stop = True
                    break
                if c < 250:
                    v = pending + c
                    iv.append(v)
                    t += v
                    pending = 0
                    csum += c
                    nbytes += 1
                    i += 1
                elif c < 255:
                    if i + 1 >= n:
                        log.debug("2-byte interval %#04x cut at byte %d of %d; stopping", c, i, n)
                        stop = True
                        break
                    # The second byte is 1..255: a 0xFF here is data, not an escape.
                    v = pending + 250 + (c - 250) * 255 + blob[i + 1] - 1
                    iv.append(v)
                    t += v
                    pending = 0
                    csum += c + blob[i + 1]
                    nbytes += 2
                    i += 2
                else:
                    if i + 1 >= n:
                        log.debug("0xFF opcode escape cut at byte %d of %d; stopping", i, n)
                        stop = True
                        break
                    op = blob[i + 1]
                    if op in (_FLUXOP_INDEX, _FLUXOP_SPACE) and i + 6 > n:
                        log.debug("flux opcode %d cut at byte %d of %d; stopping", op, i, n)
                        # Cut mid-opcode: an aborted capture ends wherever USB stopped.
                        stop = True
                        break
                    if op == _FLUXOP_INDEX:
                        # N28 counts from GW's sample cursor, which dead-time
                        # SPACEs advance too (floppy.c: index.rdata_cnt - prev).
                        out.index_ticks.append(t + pending + _n28_at(blob, i + 2))
                        skips.append((i, i + 6))
                        i += 6
                    elif op == _FLUXOP_SPACE:
                        val = _n28_at(blob, i + 2)
                        if i + 6 < n and blob[i + 6] == _LONG_TAIL:  # in-loop long interval
                            v = pending + val + _LONG_TAIL
                            iv.append(v)
                            t += v
                            pending = 0
                            csum += sum(blob[i : i + 7])
                            nbytes += 7
                            i += 7
                        else:  # dead time, carried into the next interval
                            n_dead += 1
                            pending += val
                            skips.append((i, i + 6))
                            i += 6
                    elif op in markers:
                        if i + 2 >= n:
                            log.debug("marker %#04x cut before its length at byte %d", op, i)
                            stop = True
                            break
                        plen = blob[i + 2]
                        payload = bytes(blob[i + 3 : i + 3 + plen])
                        kind = markers[op]
                        out.markers.append(BodyMarker(kind, payload, len(iv), i))
                        skips.append((i, min(n, i + 3 + plen)))
                        log.debug(
                            "marker %s (%d-byte payload) at byte %d, interval %d",
                            kind.name,
                            plen,
                            i,
                            len(iv),
                        )
                        if kind is WireMarker.SESSION_START and len(payload) >= 6:
                            out.sample_clock_hz = struct.unpack_from("<I", payload, 2)[0]
                            log.debug("SESSION_START: sample clock %d Hz", out.sample_clock_hz)
                        elif kind is WireMarker.END and len(payload) >= 13:
                            out.end = StreamEnd(*struct.unpack("<BIII", payload[:13]))
                            log.debug("END marker: %r", out.end)
                        i += 3 + plen
                    else:
                        log.debug("unknown stream opcode %#04x at byte %d; refusing", op, i)
                        raise ValueError(f"unknown stream opcode {op:#04x} at byte {i}")
            bar.update(min(i, n))
        # A NUL or a cut opcode ends the parse early; whatever follows is not
        # stream, so the bar is done either way.
        bar.update(n)
    out.data_bytes = nbytes
    out.trailing_ticks = pending
    out.checksum = csum & 0xFFFFFFFF
    out.stop_offset = min(i, n)
    log.debug(
        "parsed body: %d intervals, %d index pulses, %d markers, %d data bytes, "
        "checksum %#010x, %d dead-time SPACEs, %d trailing ticks, terminated=%s",
        len(iv),
        len(out.index_ticks),
        len(out.markers),
        nbytes,
        out.checksum,
        n_dead,
        pending,
        out.terminated,
    )
    return out


# ---------------------------------------------------------------------------
# Marker / flux-data helpers (all built on parse_body)
# ---------------------------------------------------------------------------


def _decode_payload(kind: MarkerKind, payload: bytes) -> dict[str, int | str]:
    try:
        if kind is MarkerKind.SEGMENT and len(payload) >= 8:
            ticks, index = struct.unpack_from("<II", payload)
            return {"ticks": ticks, "index": index}
        if kind is MarkerKind.EVENT and len(payload) >= 1:
            return {"code": payload[0]}
        if kind is MarkerKind.END and len(payload) >= 13:
            reason, flux_count, byte_count, checksum = struct.unpack_from("<BIII", payload)
            return {
                "reason": reason,
                "flux_count": flux_count,
                "byte_count": byte_count,
                "checksum": checksum,
            }
        if kind is MarkerKind.SESSION_START and len(payload) >= 11:
            rate, clock, tpt, direction, pass_id = struct.unpack_from("<HIHBH", payload)
            return {
                "rate": rate,
                "clock": clock,
                "tpt": tpt,
                "direction": direction,
                "pass_id": pass_id,
            }
    except struct.error:
        pass
    if kind is not MarkerKind.HEARTBEAT:  # HEARTBEAT is empty by design
        log.debug(
            "marker %s payload of %d bytes too short/unknown; keeping raw_len only",
            kind.name,
            len(payload),
        )
    return {"raw_len": len(payload)}


def _markers_of(parsed: ParsedStream) -> list[Marker]:
    out = []
    for bm in parsed.markers:
        kind = _WIRE_TO_KIND[bm.code]
        out.append(Marker(kind=kind, fields=_decode_payload(kind, bm.payload), offset=bm.offset))
    return out


def _data_of(flux: bytes, parsed: ParsedStream) -> bytes:
    """The flux data bytes: the parsed region minus its non-data spans."""
    pieces = []
    at = 0
    for start, stop in parsed.skips:
        pieces.append(flux[at:start])
        at = stop
    pieces.append(flux[at : parsed.stop_offset])
    return b"".join(pieces)


def iter_markers(flux: bytes) -> Iterator[Marker]:
    """Yield each marker in a flux body, decoded, with its byte offset."""
    yield from _markers_of(parse_body(flux))


def flux_data_only(flux: bytes) -> bytes:
    """The flux data bytes END counts: every interval's bytes, in order.

    That is each one-byte (1), two-byte (2) and long (``FF 02 N28 F9``, 7)
    interval; INDEX, dead-time SPACE, markers and the terminator are dropped.
    ``len()`` of the result is END's ``byte_count`` and :func:`flux_checksum`
    of it is END's ``checksum`` (TWS-1 §7.2).
    """
    return _data_of(flux, parse_body(flux))


def flux_checksum(data: bytes) -> int:
    """Additive checksum over flux data bytes (matches the END accounting)."""
    return sum(data) & 0xFFFFFFFF


# ---------------------------------------------------------------------------
# Container
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class RawFluxCapture:
    header: CaptureHeader
    flux: bytes  # verbatim on-wire GW flux (with marker opcodes)
    # One parse_body() result, reused by markers()/segments()/end_marker()/
    # verify(): walking a 60 MB body takes seconds, and is_truncated + verify
    # would otherwise walk it twice. Keyed on the identity of ``flux`` so that
    # assigning a new body invalidates it.
    _parsed: tuple[bytes, ParsedStream] | None = dataclasses.field(
        default=None, init=False, repr=False, compare=False
    )

    @classmethod
    def from_stream(cls, hdr: CaptureHeader, chunks: Iterable[bytes]) -> RawFluxCapture:
        buf = bytearray()
        for chunk in chunks:
            buf.extend(chunk)
        return cls(header=hdr, flux=bytes(buf))

    def parsed(self) -> ParsedStream:
        """The body walked once with :func:`parse_body` (cached)."""
        if self._parsed is None or self._parsed[0] is not self.flux:
            log.debug("parsing %d-byte TWRF body", len(self.flux))
            self._parsed = (self.flux, parse_body(self.flux))
        return self._parsed[1]

    def markers(self) -> Iterator[Marker]:
        return iter(_markers_of(self.parsed()))

    def segments(self) -> list[Marker]:
        return [m for m in self.markers() if m.kind is MarkerKind.SEGMENT]

    def end_marker(self) -> Marker | None:
        last: Marker | None = None
        for m in self.markers():
            if m.kind is MarkerKind.END:
                last = m
        return last

    @property
    def is_truncated(self) -> bool:
        """True if the run has no valid END (e.g. USB loss) — still decodes."""
        return self.end_marker() is None

    def verify(self) -> bool:
        """Check the last END's flux_count, byte_count and checksum (TWS-1 §7.2)."""
        ps = self.parsed()
        if ps.end is None:
            log.debug("no decodable END marker; capture is truncated, cannot verify")
            return False
        if not ps.verified:
            log.debug(
                "END accounting mismatch: flux_count %d vs %d, byte_count %d vs %d, "
                "checksum %#010x vs %#010x",
                ps.end.flux_count,
                len(ps.intervals),
                ps.end.byte_count,
                ps.data_bytes,
                ps.end.checksum,
                ps.checksum,
            )
        return ps.verified

    # --- persistence ---

    def _header_dict(self) -> dict:
        return header_to_dict(self.header)

    def save(self, path: str | Path) -> None:
        log.debug("writing RawFluxCapture (%d flux bytes) to %s", len(self.flux), path)
        with Path(path).open("wb") as f:
            write_preamble(f, self.header)
            f.write(self.flux)

    @classmethod
    def load(cls, path: str | Path) -> RawFluxCapture:
        log.debug("loading RawFluxCapture from %s", path)
        with Path(path).open("rb") as f:
            hdr, _ = _read_header(f, str(path))
            flux = f.read()
        return cls(header=hdr, flux=flux)


def header_to_dict(hdr: CaptureHeader) -> dict:
    d = dataclasses.asdict(hdr)
    d["direction"] = hdr.direction.value
    d["tape_format"] = int(hdr.tape_format)
    return d


def write_preamble(f: BinaryIO, hdr: CaptureHeader) -> int:
    """Write magic + version + header; return the byte offset where flux starts.

    Lets a capture stream straight to disk: the header is known before the pass
    starts, then flux chunks are appended as the device sends them.
    """
    hdr_json = json.dumps(header_to_dict(hdr), separators=(",", ":")).encode("utf-8")
    log.debug("writing TWRF v%d preamble, %d-byte header", FORMAT_VERSION, len(hdr_json))
    f.write(_PREAMBLE.pack(MAGIC, FORMAT_VERSION, len(hdr_json)))
    f.write(hdr_json)
    return _PREAMBLE.size + len(hdr_json)


# Every member of a version 2 header (TWS-1 section 4.2) and the JSON types
# it may hold. The writer (``header_to_dict`` of a whole ``CaptureHeader``)
# always writes all of them, so the reader requires all of them: a missing
# one means the header was not written by a conforming writer, and guessing
# a default (QIC-80 for a missing tape_format, say) would decode the flux
# under an assumption nobody recorded. ``None`` in a tuple means the member
# may be JSON null ("the drive did not report"); present-but-null is fine,
# absent is not. ``bool`` is listed only where it is meant: JSON ``true`` is
# a Python ``bool``, which is an ``int`` subclass, so integer members are
# checked with ``type(x) is int`` rather than ``isinstance``.
_HEADER_MEMBERS: dict[str, tuple[type | None, ...]] = {
    "rate_kbps": (int,),
    "sample_clock_hz": (int,),
    "track": (int,),
    "direction": (str,),
    "pass_id": (int,),
    "utc": (str,),
    "tape_format": (int,),
    "segments_per_track": (int,),
    "tracks": (int,),
    "sectors_per_segment": (int,),
    "device_serial": (str,),
    "physical_reverse": (bool,),
    "drive_status": (int, None),
    "drive_config": (int, None),
    "drive_rom": (int, None),
    "drive_vendor_id": (int, None),
    "tape_status": (int, None),
    "tw_commit": (str, None),
    "firmware_commit": (str, None),
    "firmware_dirty": (bool, None),
}


def _check_member(value: object, allowed: tuple[type | None, ...]) -> bool:
    """Whether ``value`` has one of the JSON types in ``allowed`` (exact types)."""
    return any(value is None if kind is None else type(value) is kind for kind in allowed)


def _read_header(f: BinaryIO, name: str) -> tuple[CaptureHeader, int]:
    """Parse and check the preamble and JSON header; return it and the flux offset.

    Raises plain ``ValueError`` for a file that is not TWRF at all (no magic:
    TWS-1 8.2 rule 1), and :class:`MalformedFileError` -- also a
    ``ValueError``, so the CLIs report it in one line -- for a TWRF file that
    is cut short in its preamble or header, has an unreadable version, or
    whose header lacks a member or holds one of the wrong type or range
    (rules 2, 3 and 6). Unknown members are ignored (rule 4).
    """

    def malformed(problem: str) -> MalformedFileError:
        log.debug("%s: %s; refusing", name, problem)
        return MalformedFileError(name, "TWRF", problem)

    pre = f.read(_PREAMBLE.size)
    if pre[:4] != MAGIC:
        log.debug("%s: preamble starts %r, not magic %r; refusing", name, pre[:4], MAGIC)
        raise ValueError(
            f"{name}: not a TWRF capture (it does not start with the TWRF magic); "
            "only TWRF captures written by `tw dump` can be converted"
        )
    if len(pre) < _PREAMBLE.size:
        raise TruncatedFileError(name, "TWRF", "preamble", _PREAMBLE.size, len(pre))
    _, version, hlen = _PREAMBLE.unpack(pre)
    if version not in READABLE_VERSIONS:
        raise malformed(
            f"it is version {version}, and only version {FORMAT_VERSION} is read; "
            "dump the tape again with the current `tw`"
        )
    raw = f.read(hlen)
    if len(raw) < hlen:
        raise TruncatedFileError(
            name, "TWRF", "JSON header", _PREAMBLE.size + hlen, _PREAMBLE.size + len(raw)
        )
    try:
        # JSONDecodeError and UnicodeDecodeError are both ValueErrors.
        d = json.loads(raw)
    except ValueError as exc:
        raise malformed(f"the header is not valid UTF-8 JSON ({exc})") from exc
    if not isinstance(d, dict):
        raise malformed(f"the header is a JSON {type(d).__name__}, not an object")
    missing = [member for member in _HEADER_MEMBERS if member not in d]
    if missing:
        raise malformed(f"the header lacks the required member(s) {', '.join(missing)}")
    for member, allowed in _HEADER_MEMBERS.items():
        if not _check_member(d[member], allowed):
            raise malformed(f"the header's {member} is {d[member]!r}, of the wrong JSON type")
    try:
        d["direction"] = Direction(d["direction"])
    except ValueError as exc:
        raise malformed(f"the header's direction is {d['direction']!r}") from exc
    try:
        d["tape_format"] = TapeFormat(d["tape_format"])
    except ValueError as exc:
        raise malformed(f"the header's tape_format is {d['tape_format']!r}, not 0..4") from exc
    unknown = sorted(set(d) - set(_HEADER_MEMBERS))
    if unknown:
        log.debug("%s: ignoring unknown header fields %s", name, unknown)
    fields = {member: d[member] for member in _HEADER_MEMBERS}
    return CaptureHeader(**fields), _PREAMBLE.size + hlen


def read_header(path: str | Path) -> tuple[CaptureHeader, int]:
    """Just the header (and where the flux starts) -- cheap on a 60 MB capture."""
    log.debug("reading TWRF header from %s", path)
    with Path(path).open("rb") as f:
        return _read_header(f, str(path))

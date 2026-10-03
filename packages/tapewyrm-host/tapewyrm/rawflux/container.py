"""RawFluxCapture — the IO-layer output artifact (DESIGN.md §7.1, §6A.6, §13.4).

On-disk format (deliberately dead simple and lossless):

    magic   "TWRF" (4 bytes)
    version u16 little-endian
    hlen    u32 little-endian  (length of the JSON header that follows)
    header  JSON (the CaptureHeader fields)
    flux    the verbatim GW flux byte stream, markers inside as opcode escapes

No re-encoding: the flux bytes are stored exactly as they came off the device.

Marker framing
--------------
Markers ride the GW opcode-escape channel (DESIGN.md §7.2) so they can never be
misread as flux. We model that here with a self-contained, byte-stuffed framing
so the container round-trips and is fully fixture-testable offline:

    ESC (0xFF) , marker_code (one of protocol.Marker, 0xF0..0xF4) , len:u8 , payload
    a literal 0xFF flux byte is escaped as  0xFF 0xFF

TODO(bench), DESIGN.md §13.6 item 1: the real GW firmware uses its own opcode
escape values and a long-flux continuation scheme. Replace ``ESC`` and the
stuffing rule below with GW's actual encoding once read from greaseweazle-firmware;
the marker *codes* and payload layouts (the contract) stay as generated in
``link/protocol.py``. Only this localized framing is bench-dependent.

Marker payload layouts (little-endian), shared with the firmware skeleton:
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
from pathlib import Path
from typing import BinaryIO

from tapewyrm.link import protocol
from tapewyrm.types import CaptureHeader, Direction, Marker, MarkerKind, TapeFormat

log = logging.getLogger(__name__)

MAGIC = b"TWRF"
# v2 (2026-10-01): the header gains the drive's raw QIC-117 report bytes
# (status, configuration -- hence the bit rate --, ROM, vendor ID, tape status)
# and the tw/firmware commits. All new fields are optional, so v1 files load
# with them as None; the on-disk layout is otherwise unchanged.
FORMAT_VERSION = 2
READABLE_VERSIONS = (1, 2)
_PREAMBLE = struct.Struct("<4sHI")  # magic, version, header length
ESC = 0xFF  # TODO(bench): reconcile with GW opcode-escape byte (§13.6 item 1)

# protocol.Marker (on-wire 0xF0..) <-> types.MarkerKind (semantic 0..4)
_WIRE_TO_KIND = {
    protocol.Marker.SESSION_START: MarkerKind.SESSION_START,
    protocol.Marker.SEGMENT: MarkerKind.SEGMENT,
    protocol.Marker.EVENT: MarkerKind.EVENT,
    protocol.Marker.END: MarkerKind.END,
    protocol.Marker.HEARTBEAT: MarkerKind.HEARTBEAT,
}
_KIND_TO_WIRE = {v: k for k, v in _WIRE_TO_KIND.items()}


# ---------------------------------------------------------------------------
# Marker framing helpers (also used to synthesize fixtures in tests)
# ---------------------------------------------------------------------------


def frame_marker(kind: MarkerKind, payload: bytes = b"") -> bytes:
    """Encode one marker as an opcode-escape frame for embedding in a flux run."""
    if len(payload) > 0xFF:
        log.debug("marker %s payload is %d bytes > 255; refusing", kind.name, len(payload))
        raise ValueError("marker payload too long for u8 length field")
    return bytes([ESC, int(_KIND_TO_WIRE[kind]), len(payload)]) + payload


def stuff_flux(raw: bytes) -> bytes:
    """Escape literal ESC bytes in a run of raw flux (0xFF -> 0xFF 0xFF)."""
    return raw.replace(bytes([ESC]), bytes([ESC, ESC]))


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


def iter_markers(flux: bytes) -> Iterator[Marker]:
    """Walk a flux run, yielding each embedded marker (skipping flux/escaped bytes)."""
    i = 0
    n = len(flux)
    unknown_escapes = 0  # counted, not logged per hit: this loop walks every byte
    while i < n:
        if flux[i] != ESC:
            i += 1
            continue
        if i + 1 >= n:
            log.debug("flux ends on a bare ESC at offset %d; stopping marker walk", i)
            break
        nxt = flux[i + 1]
        if nxt == ESC:  # escaped literal 0xFF flux byte
            i += 2
            continue
        if nxt in _WIRE_TO_KIND:
            kind = _WIRE_TO_KIND[protocol.Marker(nxt)]
            if i + 2 >= n:
                log.debug("marker %s at offset %d truncated before length; stopping", kind.name, i)
                break
            plen = flux[i + 2]
            payload = flux[i + 3 : i + 3 + plen]
            yield Marker(kind=kind, fields=_decode_payload(kind, payload), offset=i)
            i += 3 + plen
        else:
            # Malformed/unknown escape; be lenient and resync past the ESC.
            unknown_escapes += 1
            i += 1
    if unknown_escapes:
        log.debug("skipped %d unknown/malformed escapes while walking markers", unknown_escapes)


def flux_data_only(flux: bytes) -> bytes:
    """Return just the flux data bytes, with markers stripped and ESC unstuffed."""
    out = bytearray()
    i = 0
    n = len(flux)
    unknown_escapes = 0  # counted, not logged per hit: this loop walks every byte
    while i < n:
        if flux[i] != ESC:
            out.append(flux[i])
            i += 1
            continue
        if i + 1 < n and flux[i + 1] == ESC:
            out.append(ESC)
            i += 2
            continue
        if i + 1 < n and flux[i + 1] in _WIRE_TO_KIND:
            plen = flux[i + 2] if i + 2 < n else 0
            i += 3 + plen
            continue
        unknown_escapes += 1
        i += 1
    if unknown_escapes:
        log.debug("dropped %d unknown/malformed escapes from flux data", unknown_escapes)
    return bytes(out)


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

    @classmethod
    def from_stream(cls, hdr: CaptureHeader, chunks: Iterable[bytes]) -> RawFluxCapture:
        buf = bytearray()
        for chunk in chunks:
            buf.extend(chunk)
        return cls(header=hdr, flux=bytes(buf))

    def markers(self) -> Iterator[Marker]:
        return iter_markers(self.flux)

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
        """Check the END accounting (byte count + checksum) against the data."""
        end = self.end_marker()
        if end is None:
            log.debug("no END marker; capture is truncated, cannot verify")
            return False
        data = flux_data_only(self.flux)
        ok_bytes = end.fields.get("byte_count") == len(data)
        ok_sum = end.fields.get("checksum") == flux_checksum(data)
        if not (ok_bytes and ok_sum):
            log.debug(
                "END accounting mismatch: byte_count %s vs %d, checksum %s vs %d",
                end.fields.get("byte_count"),
                len(data),
                end.fields.get("checksum"),
                flux_checksum(data),
            )
        return bool(ok_bytes and ok_sum)

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


def _read_header(f: BinaryIO, name: str) -> tuple[CaptureHeader, int]:
    pre = f.read(_PREAMBLE.size)
    if len(pre) < _PREAMBLE.size or pre[:4] != MAGIC:
        log.debug(
            "%s: preamble %r (%d bytes) lacks magic %r; refusing", name, pre[:4], len(pre), MAGIC
        )
        raise ValueError(f"not a RawFluxCapture file (bad magic): {name}")
    _, version, hlen = _PREAMBLE.unpack(pre)
    if version not in READABLE_VERSIONS:
        log.debug("%s: version %d not in %s; refusing", name, version, READABLE_VERSIONS)
        raise ValueError(f"unsupported RawFluxCapture version {version}: {name}")
    d = json.loads(f.read(hlen))
    d["direction"] = Direction(d["direction"])
    d["tape_format"] = TapeFormat(d["tape_format"])
    known = {fl.name for fl in dataclasses.fields(CaptureHeader)}
    unknown = sorted(set(d) - known)
    if unknown:
        log.debug("%s: ignoring unknown header fields %s", name, unknown)
    return CaptureHeader(**{k: v for k, v in d.items() if k in known}), _PREAMBLE.size + hlen


def read_header(path: str | Path) -> tuple[CaptureHeader, int]:
    """Just the header (and where the flux starts) -- cheap on a 60 MB capture."""
    log.debug("reading TWRF header from %s", path)
    with Path(path).open("rb") as f:
        return _read_header(f, str(path))

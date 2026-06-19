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
import struct
from collections.abc import Iterable, Iterator
from pathlib import Path

from tapewyrm.link import protocol
from tapewyrm.types import CaptureHeader, Direction, Marker, MarkerKind, TapeFormat

MAGIC = b"TWRF"
FORMAT_VERSION = 1
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
    return {"raw_len": len(payload)}


def iter_markers(flux: bytes) -> Iterator[Marker]:
    """Walk a flux run, yielding each embedded marker (skipping flux/escaped bytes)."""
    i = 0
    n = len(flux)
    while i < n:
        if flux[i] != ESC:
            i += 1
            continue
        if i + 1 >= n:
            break
        nxt = flux[i + 1]
        if nxt == ESC:  # escaped literal 0xFF flux byte
            i += 2
            continue
        if nxt in _WIRE_TO_KIND:
            kind = _WIRE_TO_KIND[protocol.Marker(nxt)]
            if i + 2 >= n:
                break
            plen = flux[i + 2]
            payload = flux[i + 3 : i + 3 + plen]
            yield Marker(kind=kind, fields=_decode_payload(kind, payload), offset=i)
            i += 3 + plen
        else:
            # Malformed/unknown escape; be lenient and resync past the ESC.
            i += 1


def flux_data_only(flux: bytes) -> bytes:
    """Return just the flux data bytes, with markers stripped and ESC unstuffed."""
    out = bytearray()
    i = 0
    n = len(flux)
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
        i += 1
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
            return False
        data = flux_data_only(self.flux)
        ok_bytes = end.fields.get("byte_count") == len(data)
        ok_sum = end.fields.get("checksum") == flux_checksum(data)
        return bool(ok_bytes and ok_sum)

    # --- persistence ---

    def _header_dict(self) -> dict:
        d = dataclasses.asdict(self.header)
        d["direction"] = self.header.direction.value
        d["tape_format"] = int(self.header.tape_format)
        return d

    def save(self, path: str | Path) -> None:
        hdr_json = json.dumps(self._header_dict(), separators=(",", ":")).encode("utf-8")
        with Path(path).open("wb") as f:
            f.write(MAGIC)
            f.write(struct.pack("<H", FORMAT_VERSION))
            f.write(struct.pack("<I", len(hdr_json)))
            f.write(hdr_json)
            f.write(self.flux)

    @classmethod
    def load(cls, path: str | Path) -> RawFluxCapture:
        with Path(path).open("rb") as f:
            blob = f.read()
        if blob[:4] != MAGIC:
            raise ValueError(f"not a RawFluxCapture file (bad magic): {path}")
        (version,) = struct.unpack_from("<H", blob, 4)
        if version != FORMAT_VERSION:
            raise ValueError(f"unsupported RawFluxCapture version {version}")
        (hlen,) = struct.unpack_from("<I", blob, 6)
        hdr_json = blob[10 : 10 + hlen]
        flux = blob[10 + hlen :]
        d = json.loads(hdr_json)
        d["direction"] = Direction(d["direction"])
        d["tape_format"] = TapeFormat(d["tape_format"])
        return cls(header=CaptureHeader(**d), flux=flux)

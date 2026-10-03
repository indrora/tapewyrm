"""Parse a raw Tapewyrm capture stream: Greaseweazle flux encoding + our markers.

This is the exact inverse of the firmware encoder -- ``rdata_encode_flux()`` in
firmware/src/floppy.c (Greaseweazle's, by Keir Fraser, public domain) plus the
marker writers in firmware/src/qic/qic.c. Validated on the first real capture:
the parsed transition count, flux byte count and checksum matched the
firmware's END marker exactly (2,597,295 / 3,000,000 / 0x1cd3d3f1).

Encoding:

=====================  =====================================================
``1..249``             one interval of that many sample ticks
``250..254, b``        ``250 + (b0-250)*255 + b - 1`` ticks (up to 1524)
``FF 02 N28, 249``     long interval: N28 + 249 ticks (in-loop SPACE)
``FF 02 N28``          dead time (no flux for a while): added to the next one
``FF 01 N28``          INDEX, N28 ticks after the previous transition
``FF F0..F4 len ...``  Tapewyrm marker (SESSION_START/SEGMENT/EVENT/END/...)
``00``                 end of stream
=====================  =====================================================

N28 packs a 28-bit value into 4 bytes, 7 bits each, low bit always 1.

Ambiguity note: an in-loop long interval is ``SPACE`` immediately followed by
the byte 249; a dead-time ``SPACE`` followed by a genuine 249-tick flux looks
identical. Both decode to the same total time, so intervals are unaffected --
only the END byte-count/checksum cross-check could disagree in that corner.
"""

from __future__ import annotations

import logging
import struct
from dataclasses import dataclass, field

from tapewyrm.link.protocol import Marker

log = logging.getLogger(__name__)

_FLUXOP_INDEX = 1
_FLUXOP_SPACE = 2


@dataclass(frozen=True)
class StreamEnd:
    """The firmware's END marker: why the run stopped + its accounting."""

    reason: int  # protocol.EndReason
    flux_count: int
    byte_count: int
    checksum: int


@dataclass
class ParsedStream:
    intervals: list[int] = field(default_factory=list)  # ticks between transitions
    index_ticks: list[int] = field(default_factory=list)  # absolute tick time of INDEX
    markers: list[tuple[Marker, bytes, int]] = field(
        default_factory=list
    )  # (kind, payload, interval#)
    data_bytes: int = 0  # bytes the firmware counts for END (in-loop flux bytes)
    checksum: int = 0  # additive checksum of those bytes, & 0xFFFFFFFF
    terminated: bool = False  # saw the trailing NUL
    sample_clock_hz: int = 72_000_000  # from SESSION_START when present
    # Ticks of trailing dead time: SPACE filler GW emits while no flux arrives,
    # not yet attached to an interval when the stream ended. A capture with no
    # transitions at all is ALL dead time, which is how a silent RDATA shows up.
    trailing_ticks: int = 0
    end: StreamEnd | None = None

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


def _n28(b: bytes, i: int) -> int:
    return (
        (b[i] >> 1)
        | ((b[i + 1] & 0xFE) << 6)
        | ((b[i + 2] & 0xFE) << 13)
        | ((b[i + 3] & 0xFE) << 20)
    )


def parse(blob: bytes) -> ParsedStream:
    """Decode a capture stream. Raises ``ValueError`` on an unknown opcode."""
    out = ParsedStream()
    iv = out.intervals
    i, n, pending, t, csum, nbytes = 0, len(blob), 0, 0, 0, 0
    markers = {m.value: m for m in Marker}
    # Dead-time SPACEs are common on a real capture; tally, don't log each.
    n_dead = 0
    log.debug("parsing %d-byte capture stream", n)
    while i < n:
        c = blob[i]
        if c == 0:
            log.debug("end-of-stream NUL at byte %d of %d; stopping", i, n)
            out.terminated = True
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
                break
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
                break
            op = blob[i + 1]
            if op in (_FLUXOP_INDEX, _FLUXOP_SPACE) and i + 6 > n:
                log.debug("flux opcode %d cut at byte %d of %d; stopping", op, i, n)
                break  # cut mid-opcode: an aborted capture ends wherever USB stopped
            if op == _FLUXOP_INDEX:
                out.index_ticks.append(t + _n28(blob, i + 2))
                i += 6
            elif op == _FLUXOP_SPACE:
                val = _n28(blob, i + 2)
                if i + 6 < n and blob[i + 6] == 249:  # in-loop long interval
                    v = pending + val + 249
                    iv.append(v)
                    t += v
                    pending = 0
                    csum += sum(blob[i : i + 7])
                    nbytes += 7
                    i += 7
                else:  # dead time, carried into the next interval
                    n_dead += 1
                    pending += val
                    i += 6
            elif op in markers:
                plen = blob[i + 2]
                payload = bytes(blob[i + 3 : i + 3 + plen])
                kind = markers[op]
                out.markers.append((kind, payload, len(iv)))
                log.debug(
                    "marker %s (%d-byte payload) at byte %d, interval %d",
                    kind.name,
                    plen,
                    i,
                    len(iv),
                )
                if kind is Marker.SESSION_START and plen >= 6:
                    out.sample_clock_hz = struct.unpack_from("<I", payload, 2)[0]
                    log.debug("SESSION_START: sample clock %d Hz", out.sample_clock_hz)
                elif kind is Marker.END and plen >= 13:
                    out.end = StreamEnd(*struct.unpack("<BIII", payload[:13]))
                    log.debug("END marker: %r", out.end)
                i += 3 + plen
            else:
                log.debug("unknown stream opcode %#04x at byte %d; refusing", op, i)
                raise ValueError(f"unknown stream opcode {op:#04x} at byte {i}")
    out.data_bytes = nbytes
    out.trailing_ticks = pending
    out.checksum = csum & 0xFFFFFFFF
    log.debug(
        "parsed stream: %d intervals, %d index pulses, %d markers, %d data bytes, "
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

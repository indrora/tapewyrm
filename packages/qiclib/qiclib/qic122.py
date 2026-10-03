"""QIC-122 (Stac LZS) decompression and QIC-113 compression extents.

QIC-122 Rev B (docs/qic122b.pdf) defines the compressed stream; QIC-113 Rev G
section 9 (docs/qic113g.pdf) defines how QIC-80 volumes pack it into segments.
Written from those two documents; no mature, widely used QIC-122 decoder
exists to depend on (only small, unvetted LZS repos).

QIC-122 stream, bits taken MSB-first from each byte::

    <Compressed_Stream> := [<Compressed_String>] <End_Marker>
    <Compressed_String> := 0 <Raw_Byte> | 1 <Offset> <Length>
    <Offset>            := 1 <7 bits> | 0 <11 bits>          (back-reference)
    <End_Marker>        := 1 1 0000000                        (7-bit offset 0)
    <Length>            := 00=2 | 01=3 | 10=4 | 11 00=5 | 11 01=6 | 11 10=7 |
                           11 11 then 4-bit groups: each 1111 adds 15 and
                           continues; the first other value v ends it: 8+v+15k

Back-references index a 2048-byte history buffer of output. Each QIC-113
Compression Frame is an independent stream (the history starts empty).

QIC-113 segment layout (non-spanning volume, the only kind seen so far)::

    0, 8   Uncompressed Volume Byte Offset (sum of all earlier segments' output)
    8, ... Compression Frames, back to back:
             0, 2  Frame Size n (hi bit set = n bytes stored raw, not compressed)
             2, n  data
           Fewer than 18 bytes left after a frame = null fill, end of extent.
"""

from __future__ import annotations

import logging
import struct
from dataclasses import dataclass

log = logging.getLogger(__name__)

HISTORY = 2048
_RAW_FRAME = 0x8000
_TAIL_FILL = 18  # QIC-113 9.1.4: a frame ending within the last 18 bytes ends the extent


class Qic122Error(ValueError):
    """A malformed QIC-122 stream or QIC-113 extent."""


def decompress(data: bytes) -> bytes:
    """Decompress one QIC-122 stream (up to its end marker)."""
    # A '0'/'1' string is the fastest pure-Python bit reader for this size of
    # input (a frame is at most ~32 KB): int(bits[i:j], 2) does the slicing in C.
    bits = "".join(f"{b:08b}" for b in data)
    n = len(bits)
    out = bytearray()
    i = 0
    while True:
        if i >= n:
            log.debug(
                "qic122: bit %d of %d reached with no end marker (%d bytes out); raising",
                i,
                n,
                len(out),
            )
            raise Qic122Error("stream ended without an end marker")
        if bits[i] == "0":  # raw byte
            if i + 9 > n:
                log.debug(
                    "qic122: raw byte at bit %d needs 9 bits, only %d left; raising", i, n - i
                )
                raise Qic122Error("truncated raw byte")
            out.append(int(bits[i + 1 : i + 9], 2))
            i += 9
            continue
        # back-reference: offset
        if i + 2 > n:
            log.debug("qic122: string token at bit %d with only %d bits left; raising", i, n - i)
            raise Qic122Error("truncated string token")
        # A token cut off by the end of the frame slices short: int('', 2)
        # raises a bare ValueError, and a partial slice parses but leaves i
        # past n. Both mean a truncated token and must surface as Qic122Error,
        # the one exception callers (twvl.extract) treat as "segment lost".
        # try costs nothing on the success path (zero-cost exceptions, 3.11+).
        token_at = i
        try:
            if bits[i + 1] == "1":
                offset = int(bits[i + 2 : i + 9], 2)
                i += 9
                if offset == 0:
                    if i > n:
                        log.debug("qic122: end marker at bit %d overruns %d bits; raising", i, n)
                        raise Qic122Error("truncated end marker")
                    # Once per frame, not per token: cheap enough to log.
                    log.debug("qic122: end marker at bit %d of %d; %d bytes out", i, n, len(out))
                    return bytes(out)  # end marker
            else:
                offset = int(bits[i + 2 : i + 13], 2)
                i += 13
            # length
            code = bits[i : i + 2]
            i += 2
            if code != "11":
                length = 2 + int(code, 2)
            else:
                code = bits[i : i + 2]
                i += 2
                if code != "11":
                    length = 5 + int(code, 2)
                else:
                    length = 8
                    while True:
                        nib = int(bits[i : i + 4], 2)
                        i += 4
                        length += nib
                        if nib != 15:
                            break
        except ValueError:
            log.debug("qic122: string token at bit %d runs off %d bits; raising", token_at, n)
            raise Qic122Error(f"truncated string token at bit {token_at}") from None
        if i > n:
            log.debug(
                "qic122: string token at bit %d ends at %d > %d bits; raising", token_at, i, n
            )
            raise Qic122Error(f"truncated string token at bit {token_at}")
        if offset > len(out) or offset > HISTORY:
            log.debug(
                "qic122: offset %d > history (%d bytes out, max %d) at bit %d; raising",
                offset,
                len(out),
                HISTORY,
                i,
            )
            raise Qic122Error(f"offset {offset} reaches before the start of the history")
        # Byte at a time: the source may overlap the bytes being written
        # (offset < length repeats a pattern, e.g. offset 1 = a run).
        for _ in range(length):
            out.append(out[-offset])


@dataclass(frozen=True)
class Extent:
    """One segment's Compression Extent, decompressed."""

    uncompressed_offset: int  # where this data sits in the volume's byte stream
    data: bytes
    frames: int


def decode_extent(segment_data: bytes, *, offset_bytes: int = 8) -> Extent:
    """Decompress a non-spanning QIC-113 Compression Extent (one segment).

    ``offset_bytes`` is the width of the extent's leading uncompressed-offset
    field. QIC-113 Rev G makes it a quadword (8); some older software wrote a
    doubleword (4) -- the "MTN" tapes do, see profiles/volume/mtn.toml. Read with
    the wrong width, the first frame size lands inside the offset and nearly
    every segment fails to decode. The volume profile carries the width.
    """
    if offset_bytes not in (4, 8):
        log.debug("extent: offset width %d not 4 or 8; raising", offset_bytes)
        raise ValueError(f"extent offset width must be 4 or 8 bytes, not {offset_bytes}")
    if len(segment_data) < offset_bytes + 2:
        log.debug(
            "extent: segment data is %d bytes (< %d); raising", len(segment_data), offset_bytes + 2
        )
        raise Qic122Error("segment too short for an extent")
    (uoff,) = struct.unpack_from("<Q" if offset_bytes == 8 else "<I", segment_data, 0)
    log.debug(
        "extent: decoding %d bytes at uncompressed offset %d (%d-byte offset field)",
        len(segment_data),
        uoff,
        offset_bytes,
    )
    pos, end = offset_bytes, len(segment_data)
    out = bytearray()
    frames = 0
    while end - pos >= _TAIL_FILL:
        (size,) = struct.unpack_from("<H", segment_data, pos)
        n = size & ~_RAW_FRAME
        if n == 0:
            log.debug("extent: null fill at %d after %d frames; stopping", pos, frames)
            break  # null fill
        body = segment_data[pos + 2 : pos + 2 + n]
        if len(body) != n:
            log.debug("extent: frame at %d wants %d bytes, %d remain; raising", pos, n, len(body))
            raise Qic122Error(f"frame of {n} bytes overruns the segment at {pos}")
        out += body if size & _RAW_FRAME else decompress(body)
        frames += 1
        pos += 2 + n
    log.debug("extent: %d frames -> %d bytes", frames, len(out))
    return Extent(uncompressed_offset=uoff, data=bytes(out), frames=frames)

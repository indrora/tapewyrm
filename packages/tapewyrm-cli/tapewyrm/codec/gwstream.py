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
``FF 01 N28``          INDEX, N28 ticks after the sample cursor (see below)
``FF F0..F4 len ...``  Tapewyrm marker (SESSION_START/SEGMENT/EVENT/END/...)
``00``                 end of stream
=====================  =====================================================

N28 packs a 28-bit value into 4 bytes, 7 bits each, low bit always 1.

The sample cursor is the end of the last decoded interval plus any dead-time
SPACE since it (GW's ``prev``; floppy.c computes ``index.rdata_cnt - prev``),
so ``ParsedStream.index_ticks`` adds pending dead time before the N28.

Ambiguity note: an in-loop long interval is ``SPACE`` immediately followed by
the byte 249; a dead-time ``SPACE`` followed by a genuine 249-tick flux looks
identical. Both decode to the same total time, so intervals are unaffected --
only the END byte-count/checksum cross-check could disagree in that corner.

One parser: the tokenizer itself lives in ``tapewyrm_archive.twrf.parse_body``,
because this stream IS the TWRF flux body (TWS-1 §5) and the archive package's
own marker/verify helpers must read it the same way; tapewyrm-cli depends on
tapewyrm-archive, never the reverse (STYLE.md §2). ``parse`` here is that same
function, so ``tw convert``, ``tw dump`` and ``RawFluxCapture.verify`` cannot
disagree about a stream. Moving the loop cost nothing measurable on ``tw
convert`` (``decode_capture`` on captures/3m-unknown-1/track-00.twrf).
"""

from __future__ import annotations

from tapewyrm_archive.twrf import BodyMarker, ParsedStream, StreamEnd
from tapewyrm_archive.twrf import parse_body as parse

# ``ParsedStream.markers`` holds ``BodyMarker(code, payload, interval, offset)``
# tuples; ``code`` is the archive's ``WireMarker``, an IntEnum equal by value to
# ``tapewyrm.link.protocol.Marker`` (tests/test_protocol.py asserts the tables match).
__all__ = ["BodyMarker", "ParsedStream", "StreamEnd", "parse"]

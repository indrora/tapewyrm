"""Spot the true end of a capture stream as it arrives, chunk by chunk.

Why this exists
---------------
A capture ends when the device writes GW's end-of-stream ``00`` byte (after our
END marker, when there is one). The host used to infer the end from *silence*,
taking one empty read (the 2 s serial timeout) as "drained". That is wrong
whenever the stream merely pauses: a drive whose read channel chatters sparse
noise keeps the firmware's idle detector alive (any transition resets it) yet
produces too few bytes to fill a USB packet for seconds. The host then sent its
next command into a stream the device was still pumping, read flux bytes back
as the response header ("response echo 0x98 != request 0x09"), and every
command after that was out of step. Seen on a Colorado Jumbo 250, 2026-11-08.

Stock Greaseweazle's host does the same: it reads until the last byte it got is
``00``. We cannot do it that cheaply, because our typed markers (``FF F0..F4
len payload``) carry raw u32 fields, so a ``00`` can sit *inside* a marker
payload, and a chunk boundary can land just after one. So this module walks the
token grammar, keeping only the state needed to know where the next token
starts, and reports a ``00`` only when it is a token on its own.

The grammar is the one ``tapewyrm_archive.twrf.parse_body`` decodes (see the
table in ``tapewyrm.codec.gwstream``). Only the token *lengths* matter here:

=====================  ===========================================
``00``                 end of stream (what we are looking for)
``01..F9``             1 byte
``FA..FE, b``          2 bytes
``FF 01|02 N28``       6 bytes (INDEX / SPACE; a SPACE's trailing
                       ``F9`` long-interval tail is a token of its
                       own as far as lengths go)
``FF F0..F4 len ...``  3 + len bytes (a Tapewyrm marker)
=====================  ===========================================

Speed: a pass streams ~0.5 MB/s, so the scan must not run Python per byte. A
compiled regex finds the next byte that is NOT a 1-byte interval (``00`` or
``FA..FF``) at C speed; only those few bytes are handled in Python.
"""

from __future__ import annotations

import logging
import re

from tapewyrm.link.protocol import Marker

log = logging.getLogger(__name__)

# GW flux opcodes after the FF escape (cdc_acm_protocol.h), as in twrf.py.
_FLUXOP_INDEX = 1
_FLUXOP_SPACE = 2
_N28_TOKEN_LEN = 6  # FF op + 4 N28 bytes
_MARKER_CODES = frozenset(int(m) for m in Marker)

# Bytes that start something other than a 1-byte interval.
_INTERESTING = re.compile(rb"[\x00\xfa-\xff]")


class StreamEndScanner:
    """Feed capture chunks in order; ``feed`` says when the stream has ended.

    The scanner carries across chunk boundaries the only two things a split
    token can leave behind: how many bytes of the current token are still to
    come (``_skip``), and an incomplete token *header* (``_partial``: an ``FF``
    whose opcode has not arrived yet, or ``FF <marker>`` still missing its
    length byte), since those bytes decide the token's length.
    """

    def __init__(self) -> None:
        self._skip = 0
        self._partial = b""
        self.ended = False
        # Bytes fed before the terminator, i.e. the stream's length without
        # the 00 (only meaningful once ``ended``); kept for debug logs.
        self.end_offset: int | None = None
        self._fed = 0

    def feed(self, chunk: bytes) -> bool:
        """Scan ``chunk``; return True once the end-of-stream byte has been seen.

        Bytes after the terminator are ignored (they are the next response, or
        junk, and not this module's business). Feeding after the end is a no-op.
        """
        if self.ended or not chunk:
            return self.ended
        base = self._fed
        self._fed += len(chunk)
        data = self._partial + chunk
        # Offset of data[0] in the whole stream, for end_offset.
        origin = base - len(self._partial)
        self._partial = b""
        i = 0
        n = len(data)
        if self._skip:
            if self._skip >= n:
                self._skip -= n
                return False
            i = self._skip
            self._skip = 0
        while True:
            match = _INTERESTING.search(data, i)
            if match is None:
                return False
            i = match.start()
            c = data[i]
            if c == 0:
                self.ended = True
                self.end_offset = origin + i
                log.debug("end-of-stream 00 at byte %d", self.end_offset)
                return True
            if c < 0xFF:
                token = 2  # FA..FE: a 2-byte interval
            else:
                if i + 1 >= n:
                    self._partial = data[i:]  # FF with its opcode still to come
                    return False
                op = data[i + 1]
                if op in (_FLUXOP_INDEX, _FLUXOP_SPACE):
                    token = _N28_TOKEN_LEN
                elif op in _MARKER_CODES:
                    if i + 2 >= n:
                        self._partial = data[i:]  # FF code, length byte to come
                        return False
                    token = 3 + data[i + 2]
                else:
                    # Not in the grammar: the firmware and host disagree, or
                    # this is junk. parse_body stops at the same place; here
                    # step over the escape so a later 00 is still found.
                    log.debug("unknown FF opcode %#04x at byte %d", op, origin + i)
                    token = 2
            if i + token > n:
                self._skip = i + token - n
                return False
            i += token

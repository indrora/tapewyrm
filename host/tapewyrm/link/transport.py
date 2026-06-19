"""Transport: pyserial wrapper + frame codec for the wire protocol (DESIGN.md §13.3).

This layer is *semantically dumb*: it knows nothing about QIC commands, reports,
or tape geometry. It only knows how to push request frames at the device, pull
response frames back, and stream the capture path. The framing is the §13.3
wire protocol:

    request frame  : {opcode:u8, len:u16-LE, payload}
    response frame : {opcode:u8, len:u16-LE, payload}
    capture path   : a continuous byte stream (GW flux bytes + opcode markers)

Two concrete transports live here:

* ``SerialTransport`` — the real pyserial-backed link to a GW v4.1 CDC-ACM port.
* ``FakeTransport`` — an in-memory, scriptable stand-in for tests; you queue
  response frames / stream chunks and assert on the request frames sent.

Both satisfy the ``Transport`` protocol so ``DeviceLink`` can be driven against
either without change.
"""

from __future__ import annotations

import struct
from collections import deque
from collections.abc import Iterator
from typing import Protocol, runtime_checkable

# Wire framing constants (§13.3). The header is opcode:u8 + len:u16-LE.
_FRAME_HEADER = struct.Struct("<BH")
FRAME_HEADER_LEN = _FRAME_HEADER.size  # 3


class TransportError(Exception):
    """Low-level transport failure (port/IO/framing). DeviceLink maps these to LinkError."""


class TransportClosed(TransportError):
    """Operation attempted on a transport that is not open."""


def encode_frame(opcode: int, payload: bytes = b"") -> bytes:
    """Encode one request/response frame: {opcode:u8, len:u16-LE, payload}."""
    if not 0 <= opcode <= 0xFF:
        raise ValueError(f"opcode out of range: {opcode}")
    if len(payload) > 0xFFFF:
        raise ValueError("frame payload too long for u16 length field")
    return _FRAME_HEADER.pack(opcode, len(payload)) + payload


def decode_frame_header(header: bytes) -> tuple[int, int]:
    """Decode a 3-byte frame header into (opcode, payload_len)."""
    if len(header) != FRAME_HEADER_LEN:
        raise TransportError(f"short frame header: {len(header)} bytes")
    opcode, length = _FRAME_HEADER.unpack(header)
    return opcode, length


@runtime_checkable
class Transport(Protocol):
    """The semantically-dumb byte/frame pipe DeviceLink talks to."""

    @property
    def is_open(self) -> bool: ...

    def open(self) -> None: ...

    def close(self) -> None: ...

    def send_frame(self, opcode: int, payload: bytes = b"") -> None:
        """Write one request frame to the device."""

    def recv_frame(self) -> tuple[int, bytes]:
        """Read one response frame, returning (opcode, payload)."""

    def send_control(self, opcode: int, payload: bytes = b"") -> None:
        """Write an out-of-band control frame (e.g. ABORT) mid-capture.

        Semantically identical framing to ``send_frame``; kept separate so the
        intent (a frame that is *not* part of the request/response sequence) is
        explicit and so a real transport can route it past any read buffering.
        """

    def read_stream(self, max_bytes: int = 65536) -> bytes:
        """Read raw bytes from the capture stream (may be shorter than max_bytes).

        Returns ``b""`` when the stream is exhausted / the session is done.
        """


# ---------------------------------------------------------------------------
# Real serial transport
# ---------------------------------------------------------------------------


class SerialTransport:
    """pyserial-backed transport to a Greaseweazle v4.1 CDC-ACM serial port.

    The GW enumerates as USB CDC-ACM; one process owns the port (DESIGN.md §6A.2).
    Baud is nominal for CDC-ACM (the link is USB, not a UART) — we set a high
    value to match GW's own host tooling.
    """

    DEFAULT_BAUD = 3_000_000  # GW debug serial logging runs at 3 Mbaud (§12.1)

    def __init__(self, port: str, baud: int = DEFAULT_BAUD, timeout_s: float = 2.0) -> None:
        self.port = port
        self.baud = baud
        self.timeout_s = timeout_s
        self._serial: object | None = None

    @property
    def is_open(self) -> bool:
        ser = self._serial
        return ser is not None and bool(getattr(ser, "is_open", False))

    def open(self) -> None:
        if self.is_open:
            return
        try:
            import serial  # pyserial; imported lazily so the module loads w/o hardware
        except ImportError as exc:  # pragma: no cover - dep is declared, defensive only
            raise TransportError("pyserial is required for SerialTransport") from exc
        try:
            self._serial = serial.Serial(
                self.port, self.baud, timeout=self.timeout_s, write_timeout=self.timeout_s
            )
        except Exception as exc:  # serial.SerialException et al.
            raise TransportError(f"could not open serial port {self.port!r}: {exc}") from exc

    def close(self) -> None:
        ser = self._serial
        if ser is not None:
            try:
                ser.close()  # type: ignore[attr-defined]
            except Exception:  # pragma: no cover - best-effort teardown
                pass
        self._serial = None

    def _require(self) -> object:
        ser = self._serial
        if ser is None or not self.is_open:
            raise TransportClosed("serial transport is not open")
        return ser

    def send_frame(self, opcode: int, payload: bytes = b"") -> None:
        ser = self._require()
        try:
            ser.write(encode_frame(opcode, payload))  # type: ignore[attr-defined]
            ser.flush()  # type: ignore[attr-defined]
        except Exception as exc:
            raise TransportError(f"frame write failed: {exc}") from exc

    def send_control(self, opcode: int, payload: bytes = b"") -> None:
        # Same wire framing; a real device routes ABORT out-of-band (§5.2/§13.3).
        self.send_frame(opcode, payload)

    def _read_exact(self, n: int) -> bytes:
        ser = self._require()
        buf = bytearray()
        while len(buf) < n:
            try:
                chunk = ser.read(n - len(buf))  # type: ignore[attr-defined]
            except Exception as exc:
                raise TransportError(f"read failed: {exc}") from exc
            if not chunk:
                raise TransportError(f"short read: wanted {n}, got {len(buf)} (timeout?)")
            buf.extend(chunk)
        return bytes(buf)

    def recv_frame(self) -> tuple[int, bytes]:
        opcode, length = decode_frame_header(self._read_exact(FRAME_HEADER_LEN))
        payload = self._read_exact(length) if length else b""
        return opcode, payload

    def read_stream(self, max_bytes: int = 65536) -> bytes:
        ser = self._require()
        try:
            return bytes(ser.read(max_bytes))  # type: ignore[attr-defined]
        except Exception as exc:
            raise TransportError(f"stream read failed: {exc}") from exc


# ---------------------------------------------------------------------------
# In-memory scriptable transport (tests)
# ---------------------------------------------------------------------------


class FakeTransport:
    """Scriptable, in-memory transport for hardware-free tests.

    Usage::

        t = FakeTransport()
        t.queue_response(Txn.INFO, info_payload)      # canned response frames
        t.queue_stream_chunk(flux_bytes)              # capture stream chunks
        link = DeviceLink(t)
        ...
        assert t.sent_frames[0] == (Txn.INFO, b"")    # inspect what was sent

    Each ``recv_frame`` pops the next queued response in order. ``read_stream``
    drains queued stream chunks then returns ``b""`` (stream exhausted).
    """

    def __init__(self) -> None:
        self._open = False
        self.sent_frames: list[tuple[int, bytes]] = []
        self.sent_control: list[tuple[int, bytes]] = []
        self._responses: deque[tuple[int, bytes]] = deque()
        self._stream: deque[bytes] = deque()

    # --- scripting API ---

    def queue_response(self, opcode: int, payload: bytes = b"") -> None:
        self._responses.append((int(opcode), payload))

    def queue_stream_chunk(self, data: bytes) -> None:
        self._stream.append(data)

    def queue_stream(self, *chunks: bytes) -> None:
        for c in chunks:
            self._stream.append(c)

    # --- Transport protocol ---

    @property
    def is_open(self) -> bool:
        return self._open

    def open(self) -> None:
        self._open = True

    def close(self) -> None:
        self._open = False

    def _require(self) -> None:
        if not self._open:
            raise TransportClosed("fake transport is not open")

    def send_frame(self, opcode: int, payload: bytes = b"") -> None:
        self._require()
        # Validate framing round-trips just as the real transport would.
        encode_frame(opcode, payload)
        self.sent_frames.append((int(opcode), bytes(payload)))

    def send_control(self, opcode: int, payload: bytes = b"") -> None:
        self._require()
        encode_frame(opcode, payload)
        self.sent_control.append((int(opcode), bytes(payload)))

    def recv_frame(self) -> tuple[int, bytes]:
        self._require()
        if not self._responses:
            raise TransportError("no queued response frame")
        return self._responses.popleft()

    def read_stream(self, max_bytes: int = 65536) -> bytes:
        self._require()
        if not self._stream:
            return b""
        chunk = self._stream.popleft()
        if len(chunk) <= max_bytes:
            return chunk
        # Return the first max_bytes; push the remainder back for the next read.
        head, tail = chunk[:max_bytes], chunk[max_bytes:]
        self._stream.appendleft(tail)
        return head

    def iter_stream(self) -> Iterator[bytes]:
        """Convenience drain of all remaining stream chunks (test helper)."""
        while True:
            data = self.read_stream()
            if not data:
                return
            yield data

"""DeviceLink — typed RPC over the transaction protocol + capability gate.

This is the host's only door to the device (DESIGN.md §6A.2, §13.3). It speaks
the §13.3 transaction set and nothing more: **no QIC or QIC-80 semantics live
here**. ``command_txn`` is the verbatim seam — the host passes a command
*number*, never its meaning; the device checks ACK/Final and a bad ACK/Final is
surfaced here as an error.

Transactions implemented:

    INFO         -> DeviceInfo               (capability gate runs in open())
    SET_TIMING   -> ok
    SELECT       -> ok
    COMMAND_TXN  -> {ack, bits, final}       (bits returned as raw bytes)
    WAIT_READY   -> {status}
    CAPTURE      -> CaptureStream            (continuous flux+marker stream)
    ABORT        -> out-of-band control

Typed errors form a small tree::

    LinkError
      ├─ LinkTimeout        (a wait/transaction timed out)
      ├─ LinkVersionError   (capability gate: bad proto version / missing caps)
      └─ LinkClosed         (operation on a closed link)
"""

from __future__ import annotations

import json
import struct
from collections.abc import Iterator
from contextlib import AbstractContextManager
from types import TracebackType

from tapewyrm.link.protocol import CAPS, PROTO_VERSION, Txn
from tapewyrm.link.transport import (
    SerialTransport,
    Transport,
    TransportClosed,
    TransportError,
)
from tapewyrm.types import DeviceInfo, SelectHint, StopCond, TimingParams

# Required capabilities to refuse stock GW firmware (DESIGN.md §6A.2).
REQUIRED_CAPS = frozenset({"verbs", "capture"})

# COMMAND_TXN response framing: {ack:u8, bits:u16-LE, final:u8} + report bytes.
# The device returns the ACK bit, the (up to 16) report bits, and the Final bit;
# the host treats the data bits as raw little-endian bytes (LSB-first ordering is
# applied by the qic117 layer, which knows the bit order).
_CMD_RESP = struct.Struct("<BHB")


class LinkError(Exception):
    """Base class for all device-link failures (DESIGN.md §6A.9)."""


class LinkTimeout(LinkError):
    """A transaction or wait-ready exceeded its timeout."""


class LinkVersionError(LinkError):
    """Capability gate rejected the device (bad proto version or missing caps)."""


class LinkClosed(LinkError):
    """An operation was attempted on a link that is not open."""


def _u16le(b: int) -> bytes:
    return struct.pack("<H", b)


class DeviceLink:
    """Typed transaction client. No arbitration, no bus access (DESIGN.md §6.1)."""

    def __init__(self, transport: Transport | None = None) -> None:
        # Transport may be injected (tests/FakeTransport) or built in open().
        self._transport = transport
        self._info: DeviceInfo | None = None

    # --- lifecycle ---

    def open(self, port: str | None = None) -> DeviceInfo:
        """Open the link, read INFO, run the capability gate, return DeviceInfo.

        Capability gate (DESIGN.md §6A.2): verify ``proto_ver >= PROTO_VERSION``
        and that the required caps (``verbs``, ``capture``) are present; refuse
        stock GW firmware otherwise with ``LinkVersionError``.
        """
        if self._transport is None:
            if port is None:
                raise LinkError("no transport injected and no port given to open()")
            self._transport = SerialTransport(port)
        try:
            self._transport.open()
        except TransportError as exc:
            raise LinkError(f"failed to open transport: {exc}") from exc

        info = self._info_txn()
        self._gate(info)
        self._info = info
        return info

    def close(self) -> None:
        if self._transport is not None:
            self._transport.close()
        self._info = None

    def __enter__(self) -> DeviceLink:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    @property
    def info(self) -> DeviceInfo | None:
        return self._info

    # --- internals ---

    def _txn(self) -> Transport:
        if self._transport is None or not self._transport.is_open:
            raise LinkClosed("device link is not open")
        return self._transport

    def _request(self, opcode: Txn, payload: bytes = b"") -> tuple[int, bytes]:
        t = self._txn()
        try:
            t.send_frame(int(opcode), payload)
            return t.recv_frame()
        except TransportClosed as exc:
            raise LinkClosed(str(exc)) from exc
        except TransportError as exc:
            raise LinkError(f"transaction {opcode.name} failed: {exc}") from exc

    def _info_txn(self) -> DeviceInfo:
        _opcode, payload = self._request(Txn.INFO)
        return self._parse_info(payload)

    @staticmethod
    def _parse_info(payload: bytes) -> DeviceInfo:
        """Decode the INFO response payload into a DeviceInfo.

        The INFO payload is a small JSON object (device-side serialization is part
        of the firmware skeleton; keeping it JSON keeps the host gate readable and
        forward-compatible).

        TODO(bench), DESIGN.md §13.6 item 2: lock this to the exact firmware INFO
        serialization once the GW command handler integration is settled.
        """
        try:
            d = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise LinkError(f"malformed INFO payload: {exc}") from exc
        caps = frozenset(str(c) for c in d.get("caps", d.get("qic_caps", [])))
        return DeviceInfo(
            model=str(d.get("model", "")),
            mcu=str(d.get("mcu", "")),
            firmware=str(d.get("firmware", d.get("fw", ""))),
            serial=str(d.get("serial", "")),
            usb_high_speed=bool(d.get("usb_high_speed", False)),
            sram_bytes=int(d.get("sram_bytes", d.get("sram", 0))),
            qic_caps=caps,
            proto_ver=int(d.get("proto_ver", 0)),
        )

    @staticmethod
    def _gate(info: DeviceInfo) -> None:
        if info.proto_ver < PROTO_VERSION:
            raise LinkVersionError(
                f"device proto_ver {info.proto_ver} < required {PROTO_VERSION} "
                "(refusing stock/old firmware)"
            )
        missing = REQUIRED_CAPS - info.qic_caps
        if missing:
            raise LinkVersionError(
                f"device missing required capabilities {sorted(missing)}; "
                f"has {sorted(info.qic_caps)} (need at least {sorted(REQUIRED_CAPS)})"
            )
        # CAPS is the full advertised set generated alongside PROTO_VERSION; a
        # device may advertise a superset, but never less than REQUIRED_CAPS.
        _ = CAPS

    # --- control (synchronous) ---

    def set_timing(self, t: TimingParams) -> None:
        """Push the QIC pulse/report cadence (idle-only; §13.3)."""
        payload = json.dumps(
            {
                "pulse_us": t.pulse_us,
                "inter_pulse_us": t.inter_pulse_us,
                "terminate_gap_us": t.terminate_gap_us,
                "report_settle_us": t.report_settle_us,
                "motion_timeout_s": t.motion_timeout_s,
            },
            separators=(",", ":"),
        ).encode("utf-8")
        self._request(Txn.SET_TIMING, payload)

    def select(self, hint: SelectHint) -> None:
        """Push the drive-select hint (idle-only; sticky optional; §13.3)."""
        payload = bytes([hint.unit & 0xFF, 1 if hint.sticky else 0])
        self._request(Txn.SELECT, payload)

    def command_txn(self, n: int, report_bits: int = 0) -> bytes:
        """Emit *n* STEP pulses verbatim; optionally clock ``report_bits`` off TRK0.

        Returns the (possibly empty) report bytes. The device checks the ACK
        (must be TRUE) and the Final bit (FALSE => mid-report error); a bad
        ACK/Final raises ``LinkError`` here (DESIGN.md §13.3).
        """
        if not 0 <= n <= 0xFF:
            raise ValueError(f"command number out of range: {n}")
        if not 0 <= report_bits <= 16:
            raise ValueError(f"report_bits must be 0..16, got {report_bits}")
        _opcode, payload = self._request(Txn.COMMAND_TXN, bytes([n & 0xFF, report_bits & 0xFF]))
        if len(payload) < _CMD_RESP.size:
            raise LinkError(f"short COMMAND_TXN response: {len(payload)} bytes")
        ack, bits, final = _CMD_RESP.unpack_from(payload)
        if report_bits and not ack:
            raise LinkError(
                f"command {n}: ACK was FALSE (hardware failure or reset; DESIGN.md §2.1)"
            )
        if report_bits and not final:
            raise LinkError(f"command {n}: Final bit FALSE (error mid-report; DESIGN.md §2.1)")
        if report_bits == 0:
            return b""
        nbytes = (report_bits + 7) // 8
        return _u16le(bits)[:nbytes]

    def wait_ready(self, timeout_ms: int) -> bool:
        """Poll the ready/INDEX line until ready or timeout (DESIGN.md §13.3)."""
        timeout_s = max(0, (timeout_ms + 999) // 1000)
        _opcode, payload = self._request(Txn.WAIT_READY, _u16le(timeout_s & 0xFFFF))
        if not payload:
            raise LinkError("empty WAIT_READY response")
        # status byte: bit0 = ready (DriveStatus layout); device returns it raw.
        return bool(payload[0] & 0x01)

    # --- capture (streaming) ---

    def capture(self, motion_cmd: int, stop: StopCond) -> CaptureStream:
        """Open a capture session (device takes the lease, issues motion, streams).

        Returns a ``CaptureStream`` context manager whose ``chunks()`` yields raw
        GW flux bytes (markers inside as opcode escapes). ``__exit__`` guarantees
        teardown (abort/stop) even on error.
        """
        if not 0 <= motion_cmd <= 0xFF:
            raise ValueError(f"motion command out of range: {motion_cmd}")
        payload = _encode_stop(motion_cmd, stop)
        t = self._txn()
        try:
            t.send_frame(int(Txn.CAPTURE), payload)
        except TransportClosed as exc:
            raise LinkClosed(str(exc)) from exc
        except TransportError as exc:
            raise LinkError(f"failed to start capture: {exc}") from exc
        return CaptureStream(t)


def _encode_stop(motion_cmd: int, stop: StopCond) -> bytes:
    """CAPTURE request payload: {motion_n:u8, stop:StopCond} (§13.3)."""
    body = json.dumps(
        {
            "byte_budget": stop.byte_budget,
            "max_duration_s": stop.max_duration_s,
            "stop_on_eot": stop.stop_on_eot,
        },
        separators=(",", ":"),
    ).encode("utf-8")
    return bytes([motion_cmd & 0xFF]) + body


class CaptureStream(AbstractContextManager["CaptureStream"]):
    """Handle for a live capture session (DESIGN.md §6A.2).

    ``chunks()`` drains the raw GW flux byte stream (markers inside as opcodes).
    ``abort()`` writes the out-of-band stop control, valid mid-stream. ``__exit__``
    guarantees the device session is torn down (issues abort if still active).
    """

    def __init__(self, transport: Transport) -> None:
        self._transport = transport
        self._closed = False
        self._aborted = False

    def chunks(self) -> Iterator[bytes]:
        """Yield raw flux byte chunks until the stream is exhausted (END / abort)."""
        while not self._closed:
            try:
                data = self._transport.read_stream()
            except TransportClosed as exc:
                raise LinkClosed(str(exc)) from exc
            except TransportError as exc:
                raise LinkError(f"capture stream read failed: {exc}") from exc
            if not data:
                break
            yield data

    def abort(self) -> None:
        """Out-of-band stop, valid mid-stream (routes through Quiesce; §5.2)."""
        if self._aborted or self._closed:
            return
        self._aborted = True
        try:
            self._transport.send_control(int(Txn.ABORT))
        except TransportError as exc:
            raise LinkError(f"abort failed: {exc}") from exc

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        # Guarantee teardown: if the session wasn't cleanly drained, abort it so
        # the device stops the tape (DESIGN.md §5.2 — stop before releasing).
        if not self._closed and not self._aborted:
            try:
                self.abort()
            except LinkError:
                pass  # best-effort; closing the link below is the backstop
        self._closed = True

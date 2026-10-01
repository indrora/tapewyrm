"""DeviceLink — typed RPC over the transaction protocol + capability gate.

This is the host's only door to the device (DESIGN.md §6A.2, §13.3). It speaks
the §13.3 transaction set and nothing more: **no QIC or QIC-80 semantics live
here**. ``command_txn`` is the verbatim seam — the host passes a command
*number*, never its meaning; the device checks ACK/Final and a bad ACK/Final is
surfaced here as an error.

Transactions implemented:

    GW GET_INFO + INFO -> DeviceInfo         (capability gate runs in open())
    SET_TIMING   -> ok
    select()     -> GW SET_BUS_TYPE + SELECT (+ MOTOR)   (GW-native, not a TW verb)
    COMMAND_TXN  -> {flags, bits, nbits}     (bits returned as raw bytes)
    WAIT_READY   -> {status}
    CAPTURE      -> CaptureStream            (continuous flux+marker stream)
    ABORT        -> out-of-band control

Every request is a Greaseweazle command packet and every payload layout below is
copied from the firmware handlers in firmware/src/qic/qic.c (the firmware is the
source of truth). See link/transport.py for the framing.

TODO: these layouts are hand-mirrored from qic.c. They belong in
protocol/protocol.toml so generate.py emits both sides (that drift is exactly
what broke the first bench bring-up).

Typed errors form a small tree::

    LinkError
      ├─ LinkTimeout        (a wait/transaction timed out)
      ├─ LinkVersionError   (capability gate: bad proto version / missing caps)
      └─ LinkClosed         (operation on a closed link)
"""

from __future__ import annotations

import struct
from collections.abc import Iterator
from contextlib import AbstractContextManager
from dataclasses import dataclass
from types import TracebackType

from tapewyrm.link.protocol import CAPS, PROTO_VERSION, Txn
from tapewyrm.link.transport import (
    ACK_NAMES,
    ACK_OKAY,
    SerialTransport,
    Transport,
    TransportClosed,
    TransportError,
)
from tapewyrm.types import DeviceInfo, SelectHint, StopCond, TimingParams

# Required capabilities to refuse stock GW firmware (DESIGN.md §6A.2).
REQUIRED_CAPS = frozenset({"verbs", "capture"})

# TW_CAP_* bits (firmware/inc/protocol.h) -> the names protocol.py's CAPS uses.
_CAP_BITS = {0: "verbs", 1: "capture", 2: "markers"}

# --- GW-native commands (firmware/inc/cdc_acm_protocol.h) ------------------
# The QIC verbs ride alongside GW's own command set; drive select, bus type and
# motor are plain GW commands, and so is the board/firmware identity.
_GW_GET_INFO = 0
_GW_MOTOR = 6
_GW_SELECT = 12
_GW_DESELECT = 13
_GW_SET_BUS_TYPE = 14
_GETINFO_FIRMWARE = 0
# struct gw_info: fw_major, fw_minor, is_main_firmware, max_cmd, sample_freq:u32,
# hw_model, hw_submodel, usb_speed, mcu_id, mcu_mhz:u16, mcu_sram_kb:u16,
# usb_buf_kb:u16 -- 20 bytes, zero-padded to 32 on the wire.
_GW_INFO = struct.Struct("<4BI4B3H")
_GW_INFO_LEN = 32
_BUS_TYPES = {"ibmpc": 1, "shugart": 2}

# (hw_model, hw_submodel) -> board name, mirroring greaseweazle/tools/info.py.
# We report ourselves as "tapewyrm-GW <board>"; the numeric ids stay stock so
# `gw update` still matches our .upd entries and `gw` keeps working.
_BOARD_NAMES = {
    (4, 0): "V4", (4, 1): "V4 Slim", (4, 2): "V4.1",
    (7, 0): "F7 v1", (7, 6): "F7 Slim",
    (1, 0): "F1", (1, 1): "F1 Plus",
}  # fmt: skip

# --- Tapewyrm verb payloads (firmware/src/qic/qic.c) ------------------------
# INFO response: proto_ver:u8, caps:u32 bitmask, sram_bytes:u32, sample_hz:u32.
_QIC_INFO = struct.Struct("<BIII")
# SET_TIMING request: pulse_us, inter_pulse_us, terminate_gap_us, tack_us,
# tbit_us (all u16), report_on_index:u8.
_SET_TIMING = struct.Struct("<5HB")
# COMMAND_TXN request: cmd_n:u8, report_bits:u8.
# COMMAND_TXN response: flags:u8 (b0 ack, b1 final_ok, b2 timed_out), bits:u16,
# nbits:u8. The (up to 16) report bits come back LSB-first in `bits`; the
# qic117 layer applies the profile's bit order.
_CMD_RESP = struct.Struct("<BHB")
_FLAG_ACK, _FLAG_FINAL, _FLAG_TIMEOUT = 0x01, 0x02, 0x04
# WAIT_READY request: timeout_s:u16. Response: status:u8, 0 = ready, 1 = timeout.
_WAIT_RESP_LEN = 1
# SCOPE response head: initial:u8, n_edges:u8, overflow:u8, counts:4*u16; then
# n_edges * {t_us:u32, state:u8}. State bit set = line asserted (bus LOW).
_SCOPE_HEAD = struct.Struct("<3B4H")
_SCOPE_EDGE = struct.Struct("<IB")
_SCOPE_MAX_MS = 10_000
SCOPE_LINES = ("TRK0", "INDEX", "WRPROT", "PIN34")


@dataclass(frozen=True)
class ScopeTrace:
    """Edge log from SCOPE. ``edges`` = ((t_us, state), ...); see SCOPE_LINES."""

    initial: int
    edges: tuple[tuple[int, int], ...]
    overflow: bool
    counts: dict[str, int]
    duration_ms: int

    @staticmethod
    def describe(state: int) -> str:
        """Names of the asserted lines in a state byte, e.g. 'TRK0+INDEX'."""
        names = [n for i, n in enumerate(SCOPE_LINES) if state >> i & 1]
        return "+".join(names) or "-"


# CAPTURE request: motion_n:u8, rate:u16, tpt:u16, direction:u8, pass_id:u16,
# byte_budget:u32 (0 = free-run until aborted).
_CAPTURE = struct.Struct("<BHHBHI")


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
        """Open the link, identify the board, run the capability gate.

        Identity comes from GW's own GET_INFO; the capability gate (DESIGN.md
        §6A.2) from our INFO verb. Stock GW firmware answers INFO with
        BAD_COMMAND, which we turn into ``LinkVersionError``.
        """
        if self._transport is None:
            if port is None:
                raise LinkError("no transport injected and no port given to open()")
            self._transport = SerialTransport(port)
        try:
            self._transport.open()
        except TransportError as exc:
            raise LinkError(f"failed to open transport: {exc}") from exc

        info = self._identify()
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

    def _exchange(self, opcode: int, payload: bytes, resp_len: int) -> tuple[int, bytes]:
        """One request/response; returns (ack, payload) without judging the ack."""
        t = self._txn()
        try:
            t.send_frame(opcode, payload)
            return t.recv_frame(opcode, resp_len)
        except TransportClosed as exc:
            raise LinkClosed(str(exc)) from exc
        except TransportError as exc:
            raise LinkError(f"command 0x{opcode:02x} failed: {exc}") from exc

    def _request(self, opcode: int, payload: bytes = b"", resp_len: int = 0) -> bytes:
        """One request/response that must be ACK_OKAY; returns the payload."""
        ack, body = self._exchange(opcode, payload, resp_len)
        if ack != ACK_OKAY:
            name = Txn(opcode).name if opcode in Txn._value2member_map_ else f"0x{opcode:02x}"
            raise LinkError(f"command {name} rejected: ACK_{ACK_NAMES.get(ack, ack)}")
        return body

    def _identify(self) -> DeviceInfo:
        raw = self._request(_GW_GET_INFO, bytes([_GETINFO_FIRMWARE]), _GW_INFO_LEN)
        (fw_major, fw_minor, _is_main, _max_cmd, _sample_hz, hw_model, hw_sub,
         usb_speed, _mcu_id, mcu_mhz, mcu_sram_kb, _usb_buf_kb) = _GW_INFO.unpack_from(raw)  # fmt: skip
        board = _BOARD_NAMES.get((hw_model, hw_sub), f"model {hw_model}.{hw_sub}")

        # Our verb. Stock firmware -> BAD_COMMAND -> proto_ver 0 -> gate refuses.
        ack, body = self._exchange(int(Txn.INFO), b"", _QIC_INFO.size)
        proto_ver, caps_mask, sram_bytes = 0, 0, 0
        if ack == ACK_OKAY:
            proto_ver, caps_mask, sram_bytes, _ = _QIC_INFO.unpack(body)
        caps = frozenset(name for bit, name in _CAP_BITS.items() if caps_mask >> bit & 1)

        return DeviceInfo(
            model=f"tapewyrm-GW {board}",
            mcu=f"{mcu_mhz} MHz, {mcu_sram_kb} kB SRAM",
            firmware=f"{fw_major}.{fw_minor}",
            serial="",  # GW's serial lives in the USB descriptor, not GET_INFO
            usb_high_speed=usb_speed == 1,
            sram_bytes=sram_bytes,
            qic_caps=caps,
            proto_ver=proto_ver,
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
        """Push the QIC pulse/report cadence (idle-only; §13.3).

        ``report_settle_us`` is the firmware's per-bit TBIT window; the motion
        timeout is host-side only and is not sent.
        """
        payload = _SET_TIMING.pack(
            t.pulse_us,
            t.inter_pulse_us,
            t.terminate_gap_us,
            t.tack_us,
            t.report_settle_us,
            1 if t.report_on_index else 0,
        )
        self._request(int(Txn.SET_TIMING), payload)

    def select(self, hint: SelectHint) -> None:
        """Select the drive with GW's own SET_BUS_TYPE + SELECT (+ MOTOR).

        Not a Tapewyrm verb: the firmware's pulse engine just uses whatever GW
        unit is currently selected. GW drops the select itself if the host goes
        quiet for its watchdog period.
        """
        bus = _BUS_TYPES.get(hint.bus)
        if bus is None:
            raise ValueError(f"unknown bus type {hint.bus!r} (want one of {sorted(_BUS_TYPES)})")
        self._request(_GW_SET_BUS_TYPE, bytes([bus]))
        self._request(_GW_SELECT, bytes([hint.unit & 0xFF]))
        if hint.motor:
            self._request(_GW_MOTOR, bytes([hint.unit & 0xFF, 1]))

    def deselect(self) -> None:
        """Release drive select (GW DESELECT)."""
        self._request(_GW_DESELECT)

    def command_txn(self, n: int, report_bits: int = 0) -> bytes:
        """Emit *n* STEP pulses verbatim; optionally clock ``report_bits`` off TRK0.

        Returns the (possibly empty) report bytes, LSB-first. With a report, the
        device must see ACK (first bit TRUE) and Final (stop bit TRUE); either
        missing raises ``LinkError`` (DESIGN.md §2.1).
        """
        if not 0 <= n <= 0xFF:
            raise ValueError(f"command number out of range: {n}")
        if not 0 <= report_bits <= 16:
            raise ValueError(f"report_bits must be 0..16, got {report_bits}")
        body = self._request(int(Txn.COMMAND_TXN), bytes([n, report_bits]), _CMD_RESP.size)
        flags, bits, nbits = _CMD_RESP.unpack(body)
        if report_bits == 0:
            return b""
        timed_out = " (timed out)" if flags & _FLAG_TIMEOUT else ""
        if not flags & _FLAG_ACK:
            raise LinkError(
                f"command {n}: no ACK bit{timed_out} -- drive not selected/listening, "
                "or reset/hardware failure (DESIGN.md §2.1)"
            )
        if not flags & _FLAG_FINAL:
            raise LinkError(
                f"command {n}: Final bit FALSE after {nbits}/{report_bits} bits{timed_out} "
                "(error mid-report; DESIGN.md §2.1)"
            )
        nbytes = (report_bits + 7) // 8
        return _u16le(bits)[:nbytes]

    def wait_ready(self, timeout_ms: int) -> bool:
        """Firmware WAIT_READY: wait for INDEX-low, the drive's ready cue.

        A selected, ready QIC-117 drive emits "cue" INDEX pulses every ~4 ms
        (Rev J Fig. 1/6; ~2.9 ms on the bench Colorado), so INDEX activity does
        mean ready. ``Qic117Drive`` polls Report Drive Status instead because it
        also wants the status bits; both are valid.
        """
        timeout_s = max(0, (timeout_ms + 999) // 1000)
        body = self._request(int(Txn.WAIT_READY), _u16le(timeout_s & 0xFFFF), _WAIT_RESP_LEN)
        return body[0] == 0  # 0 = ready, 1 = timed out

    # --- bench tools ---

    def scope(self, cmd_n: int = 0, duration_ms: int = 1000) -> ScopeTrace:
        """SCOPE: optional ``cmd_n`` STEP pulses, then edge-log the input lines.

        A poor man's logic analyser for bring-up (see qic.c CMD_QIC_SCOPE):
        sampling starts the instant the last pulse is released, so the drive's
        ACK (or any other reaction) lands inside the window.
        """
        if not 0 <= cmd_n <= 0xFF:
            raise ValueError(f"cmd_n out of range: {cmd_n}")
        duration_ms = min(max(0, duration_ms), _SCOPE_MAX_MS)
        t = self._txn()
        payload = struct.pack("<BH", cmd_n, duration_ms)
        try:
            t.send_frame(int(Txn.SCOPE), payload)
            # The device answers only when the window closes: allow for it.
            ack, head = t.recv_frame(
                int(Txn.SCOPE), _SCOPE_HEAD.size, timeout_s=duration_ms / 1000 + 2.0
            )
            if ack != ACK_OKAY:
                raise LinkError(f"command SCOPE rejected: ACK_{ACK_NAMES.get(ack, ack)}")
            initial, n_edges, overflow, *counts = _SCOPE_HEAD.unpack(head)
            tail = t.read_exact(n_edges * _SCOPE_EDGE.size) if n_edges else b""
        except TransportClosed as exc:
            raise LinkClosed(str(exc)) from exc
        except TransportError as exc:
            raise LinkError(f"command SCOPE failed: {exc}") from exc
        edges = tuple(_SCOPE_EDGE.iter_unpack(tail))
        return ScopeTrace(
            initial=initial,
            edges=edges,
            overflow=bool(overflow),
            counts=dict(zip(SCOPE_LINES, counts, strict=True)),
            duration_ms=duration_ms,
        )

    # --- capture (streaming) ---

    def capture(
        self,
        motion_cmd: int,
        stop: StopCond,
        *,
        rate: int = 0,
        tpt: int = 0,
        direction: int = 0,
        pass_id: int = 0,
    ) -> CaptureStream:
        """Open a capture session (device issues motion, then streams flux).

        Like GW's READ_FLUX, the device answers with a 2-byte {echo, ack} header
        and then the stream follows. ``rate``/``tpt``/``direction``/``pass_id``
        are only recorded into the SESSION_START marker.

        TODO(bench): ``max_duration_s`` / ``stop_on_eot`` have no firmware field
        yet; only ``byte_budget`` is enforced device-side.
        """
        if not 0 <= motion_cmd <= 0xFF:
            raise ValueError(f"motion command out of range: {motion_cmd}")
        payload = _CAPTURE.pack(motion_cmd, rate, tpt, direction, pass_id, stop.byte_budget or 0)
        self._request(int(Txn.CAPTURE), payload)
        return CaptureStream(self._txn())


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

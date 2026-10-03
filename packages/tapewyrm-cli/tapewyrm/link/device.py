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
    deselect(), motor() -> GW DESELECT, GW MOTOR          (GW-native)
    COMMAND_TXN  -> {flags, bits, nbits}     (bits returned as raw bytes)
    WAIT_READY   -> {status}
    SCOPE        -> ScopeTrace               (bench edge log)
    BUILD_INFO   -> FirmwareBuild            (None on stock/older firmware)
    CAPTURE      -> CaptureStream            (continuous flux+marker stream)
    flux_status() -> GW GET_FLUX_STATUS      (after a capture drains)
    ABORT        -> out-of-band control      (a clear-comms baud change)

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

import logging
import struct
import time
from collections.abc import Iterator
from contextlib import AbstractContextManager
from dataclasses import dataclass, replace
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

# Everything here is bench detail, so it logs at DEBUG; the CLI decides what a
# user sees. CaptureStream's read loops are a hot path (one read per USB
# packet): they log only at session boundaries and on failure, never per chunk.
log = logging.getLogger(__name__)

# Required capabilities to refuse stock GW firmware (DESIGN.md §6A.2).
REQUIRED_CAPS = frozenset({"verbs", "capture"})

# TW_CAP_* bits (firmware/inc/protocol.h) -> the names protocol.py's CAPS uses.
_CAP_BITS = {0: "verbs", 1: "capture", 2: "markers"}

# --- GW-native commands (firmware/inc/cdc_acm_protocol.h) ------------------
# The QIC verbs ride alongside GW's own command set; drive select, bus type and
# motor are plain GW commands, and so is the board/firmware identity.
_GW_GET_INFO = 0
_GW_MOTOR = 6
_GW_GET_FLUX_STATUS = 9
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


# BUILD_INFO response: commit:40 bytes ASCII hex (zero-filled if unknown), dirty:u8.
_BUILD_INFO = struct.Struct("<40sB")


@dataclass(frozen=True)
class FirmwareBuild:
    """Which source a firmware image was built from (BUILD_INFO verb)."""

    commit: str | None  # 40-char hex SHA, None if the build had no git
    dirty: bool  # firmware/ or protocol/ had uncommitted changes at build time


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


# Greaseweazle USB identity (firmware/src/usb/config.c). Our firmware keeps it,
# so stock and Tapewyrm images enumerate identically; the INFO gate tells them
# apart after open.
GW_USB_VID = 0x1209  # pid.codes open-source VID
GW_USB_PID = 0x4D69  # Keir Fraser's Greaseweazle PID


def usb_serial_for(port: str) -> str:
    """USB serial-number string of the device behind ``port`` ("" if unknown)."""
    from serial.tools import list_ports

    for p in list_ports.comports():
        if p.device == port:
            return p.serial_number or ""
    log.debug("%s not in the USB port list; serial number unknown", port)
    return ""


def find_port() -> str:
    """Return the serial port of the single attached Greaseweazle.

    Raises ``LinkError`` when none, or more than one, is attached: guessing
    between two GWs could send tape commands to the wrong drive.
    """
    from serial.tools import list_ports  # pyserial; lazy so imports stay light

    ports = [p.device for p in list_ports.comports() if (p.vid, p.pid) == (GW_USB_VID, GW_USB_PID)]
    if not ports:
        log.debug("no port with USB %04x:%04x; refusing to guess", GW_USB_VID, GW_USB_PID)
        raise LinkError("no Greaseweazle found (USB 1209:4d69); pass --port")
    if len(ports) > 1:
        log.debug("%d Greaseweazles found %s; refusing to guess", len(ports), ports)
        raise LinkError(f"several Greaseweazles found {ports}; pass --port to pick one")
    log.debug("found Greaseweazle on %s", ports[0])
    return ports[0]


class DeviceLink:
    """Typed transaction client. No arbitration, no bus access (DESIGN.md §6.1)."""

    def __init__(self, transport: Transport | None = None) -> None:
        # Transport may be injected (tests/FakeTransport) or built in open().
        self._transport = transport
        self._info: DeviceInfo | None = None

    # --- lifecycle ---

    def open(self, port: str | None = None, *, gate: bool = True) -> DeviceInfo:
        """Open the link, identify the board, run the capability gate.

        Identity comes from GW's own GET_INFO; the capability gate (DESIGN.md
        §6A.2) from our INFO verb. Stock GW firmware answers INFO with
        BAD_COMMAND, which we turn into ``LinkVersionError`` -- unless
        ``gate=False`` (``tw info`` wants to *describe* stock firmware, not
        refuse it); then ``proto_ver`` is 0 and ``qic_caps`` is empty.
        """
        if self._transport is None:
            if port is None:
                log.debug("no port given; searching for a Greaseweazle")
            self._transport = SerialTransport(port if port is not None else find_port())
        log.debug("opening device link")
        try:
            self._transport.open()
        except TransportError as exc:
            log.debug("transport open failed: %s", exc)
            raise LinkError(f"failed to open transport: {exc}") from exc

        info = self._identify()
        if isinstance(self._transport, SerialTransport):
            info = replace(
                info,
                port=self._transport.port,
                serial=usb_serial_for(self._transport.port),
            )
        log.debug(
            "identified %s fw %s on %s: proto_ver %d, caps %s, sram %d B, sample clock %d Hz",
            info.model, info.firmware, info.port or "?", info.proto_ver,
            sorted(info.qic_caps), info.sram_bytes, info.sample_clock_hz,
        )  # fmt: skip
        if gate:
            self._gate(info)
        else:
            log.debug("capability gate skipped (gate=False)")
        self._info = info
        return info

    def close(self) -> None:
        if self._transport is not None:
            log.debug("closing device link")
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
            log.debug("link used while not open; refusing")
            raise LinkClosed("device link is not open")
        return self._transport

    def _exchange(self, opcode: int, payload: bytes, resp_len: int) -> tuple[int, bytes]:
        """One request/response; returns (ack, payload) without judging the ack."""
        t = self._txn()
        log.debug("sending 0x%02x [%s], expecting %d B", opcode, payload.hex(), resp_len)
        try:
            t.send_frame(opcode, payload)
            return t.recv_frame(opcode, resp_len)
        except TransportClosed as exc:
            log.debug("0x%02x: transport closed mid-exchange: %s", opcode, exc)
            raise LinkClosed(str(exc)) from exc
        except TransportError as exc:
            log.debug("0x%02x: exchange failed: %s", opcode, exc)
            raise LinkError(f"command 0x{opcode:02x} failed: {exc}") from exc

    def _request(self, opcode: int, payload: bytes = b"", resp_len: int = 0) -> bytes:
        """One request/response that must be ACK_OKAY; returns the payload."""
        ack, body = self._exchange(opcode, payload, resp_len)
        if ack != ACK_OKAY:
            name = Txn(opcode).name if opcode in Txn._value2member_map_ else f"0x{opcode:02x}"
            log.debug("command %s rejected: ACK_%s", name, ACK_NAMES.get(ack, ack))
            raise LinkError(f"command {name} rejected: ACK_{ACK_NAMES.get(ack, ack)}")
        return body

    def _identify(self) -> DeviceInfo:
        log.debug("identifying board (GW GET_INFO, then INFO verb)")
        raw = self._request(_GW_GET_INFO, bytes([_GETINFO_FIRMWARE]), _GW_INFO_LEN)
        (fw_major, fw_minor, _is_main, _max_cmd, _sample_hz, hw_model, hw_sub,
         usb_speed, _mcu_id, mcu_mhz, mcu_sram_kb, _usb_buf_kb) = _GW_INFO.unpack_from(raw)  # fmt: skip
        board = _BOARD_NAMES.get((hw_model, hw_sub), f"model {hw_model}.{hw_sub}")

        # Our verb. Stock firmware -> BAD_COMMAND -> proto_ver 0 -> gate refuses.
        ack, body = self._exchange(int(Txn.INFO), b"", _QIC_INFO.size)
        proto_ver, caps_mask, sram_bytes, sample_hz = 0, 0, 0, 0
        if ack == ACK_OKAY:
            proto_ver, caps_mask, sram_bytes, sample_hz = _QIC_INFO.unpack(body)
        else:
            log.debug("INFO verb answered ACK_%s: stock GW firmware?", ACK_NAMES.get(ack, ack))
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
            sample_clock_hz=sample_hz,
        )

    @staticmethod
    def _gate(info: DeviceInfo) -> None:
        if info.proto_ver < PROTO_VERSION:
            log.debug("gate: proto_ver %d < %d; refusing", info.proto_ver, PROTO_VERSION)
            raise LinkVersionError(
                f"device proto_ver {info.proto_ver} < required {PROTO_VERSION} "
                "(refusing stock/old firmware)"
            )
        missing = REQUIRED_CAPS - info.qic_caps
        if missing:
            log.debug(
                "gate: missing caps %s (has %s); refusing", sorted(missing), sorted(info.qic_caps)
            )
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
        log.debug("set_timing: %s", t)
        self._request(int(Txn.SET_TIMING), payload)

    def select(self, hint: SelectHint) -> None:
        """Select the drive with GW's own SET_BUS_TYPE + SELECT (+ MOTOR).

        Not a Tapewyrm verb: the firmware's pulse engine just uses whatever GW
        unit is currently selected. GW drops the select itself if the host goes
        quiet for its watchdog period.
        """
        bus = _BUS_TYPES.get(hint.bus)
        if bus is None:
            log.debug("select: unknown bus %r; refusing", hint.bus)
            raise ValueError(f"unknown bus type {hint.bus!r} (want one of {sorted(_BUS_TYPES)})")
        log.debug("select: bus %s, unit %d, motor %s", hint.bus, hint.unit, hint.motor)
        self._request(_GW_SET_BUS_TYPE, bytes([bus]))
        self._request(_GW_SELECT, bytes([hint.unit & 0xFF]))
        if hint.motor:
            self._request(_GW_MOTOR, bytes([hint.unit & 0xFF, 1]))

    def deselect(self) -> None:
        """Release drive select (GW DESELECT)."""
        log.debug("deselect")
        self._request(_GW_DESELECT)

    def motor(self, unit: int, on: bool) -> None:
        """Drive one unit's motor-enable line (GW MOTOR), e.g. to switch it off.

        Needs a bus type set first (``select()`` does that); GW answers
        ACK_NO_BUS otherwise, and ACK_BAD_UNIT for a unit the bus lacks (the
        IBM PC bus has units 0 and 1). Turning a motor on makes GW wait its
        motor-spin-up delay before answering.
        """
        log.debug("motor: unit %d %s", unit, "on" if on else "off")
        self._request(_GW_MOTOR, bytes([unit & 0xFF, 1 if on else 0]))

    def command_txn(self, n: int, report_bits: int = 0) -> bytes:
        """Emit *n* STEP pulses verbatim; optionally clock ``report_bits`` off TRK0.

        Returns the (possibly empty) report bytes, LSB-first. With a report, the
        device must see ACK (first bit TRUE) and Final (stop bit TRUE); either
        missing raises ``LinkError`` (DESIGN.md §2.1).
        """
        if not 0 <= n <= 0xFF:
            log.debug("command_txn: n=%d outside 0..255; refusing", n)
            raise ValueError(f"command number out of range: {n}")
        if not 0 <= report_bits <= 16:
            log.debug("command_txn: report_bits=%d outside 0..16; refusing", report_bits)
            raise ValueError(f"report_bits must be 0..16, got {report_bits}")
        body = self._request(int(Txn.COMMAND_TXN), bytes([n, report_bits]), _CMD_RESP.size)
        flags, bits, nbits = _CMD_RESP.unpack(body)
        # One line per QIC-117 command: the bench transcript of what the drive
        # was told and what it said back (bits are raw, LSB-first).
        log.debug("cmd %d: flags 0x%02x, %d/%d bits = 0x%04x", n, flags, nbits, report_bits, bits)
        if report_bits == 0:
            log.debug("cmd %d: no report requested; done", n)
            return b""
        timed_out = " (timed out)" if flags & _FLAG_TIMEOUT else ""
        if not flags & _FLAG_ACK:
            log.debug("cmd %d: ACK bit missing (flags 0x%02x)%s; raising", n, flags, timed_out)
            raise LinkError(
                f"command {n}: no ACK bit{timed_out} -- drive not selected/listening, "
                "or reset/hardware failure (DESIGN.md §2.1)"
            )
        if not flags & _FLAG_FINAL:
            log.debug("cmd %d: Final bit FALSE (flags 0x%02x)%s; raising", n, flags, timed_out)
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
        log.debug("wait_ready: waiting up to %d s (asked %d ms)", timeout_s, timeout_ms)
        body = self._request(int(Txn.WAIT_READY), _u16le(timeout_s & 0xFFFF), _WAIT_RESP_LEN)
        ready = body[0] == 0  # 0 = ready, 1 = timed out
        if not ready:
            log.debug("wait_ready: not ready after %d s", timeout_s)
        return ready

    def flux_status(self) -> int:
        """GW GET_FLUX_STATUS (cmd 9): the ACK of the read that just finished.

        0 = OKAY; e.g. 4 = FLUX_OVERFLOW if USB couldn't keep up. Call after a
        capture stream has drained.
        """
        ack, _ = self._exchange(_GW_GET_FLUX_STATUS, b"", 0)
        if ack != ACK_OKAY:
            log.debug("flux status after capture: ACK_%s", ACK_NAMES.get(ack, ack))
        return ack

    def build_info(self) -> FirmwareBuild | None:
        """The git commit the firmware was built from; None on older images.

        Images predating the BUILD_INFO verb (and stock GW) answer BAD_COMMAND.
        """
        ack, body = self._exchange(int(Txn.BUILD_INFO), b"", _BUILD_INFO.size)
        if ack != ACK_OKAY:
            log.debug(
                "BUILD_INFO answered ACK_%s: image predates the verb", ACK_NAMES.get(ack, ack)
            )
            return None
        raw, dirty = _BUILD_INFO.unpack(body)
        commit = raw.rstrip(b"\x00").decode("ascii", "replace")
        return FirmwareBuild(commit=commit or None, dirty=bool(dirty))

    # --- bench tools ---

    def scope(self, cmd_n: int = 0, duration_ms: int = 1000) -> ScopeTrace:
        """SCOPE: optional ``cmd_n`` STEP pulses, then edge-log the input lines.

        A poor man's logic analyser for bring-up (see qic.c CMD_QIC_SCOPE):
        sampling starts the instant the last pulse is released, so the drive's
        ACK (or any other reaction) lands inside the window.
        """
        if not 0 <= cmd_n <= 0xFF:
            log.debug("scope: cmd_n=%d outside 0..255; refusing", cmd_n)
            raise ValueError(f"cmd_n out of range: {cmd_n}")
        clamped = min(max(0, duration_ms), _SCOPE_MAX_MS)
        if clamped != duration_ms:
            log.debug("scope: duration %d ms clamped to %d ms", duration_ms, clamped)
        duration_ms = clamped
        t = self._txn()
        payload = struct.pack("<BH", cmd_n, duration_ms)
        log.debug("scope: cmd %d, %d ms window", cmd_n, duration_ms)
        try:
            t.send_frame(int(Txn.SCOPE), payload)
            # The device answers only when the window closes: allow for it.
            ack, head = t.recv_frame(
                int(Txn.SCOPE), _SCOPE_HEAD.size, timeout_s=duration_ms / 1000 + 2.0
            )
            if ack != ACK_OKAY:
                log.debug("command SCOPE rejected: ACK_%s", ACK_NAMES.get(ack, ack))
                raise LinkError(f"command SCOPE rejected: ACK_{ACK_NAMES.get(ack, ack)}")
            initial, n_edges, overflow, *counts = _SCOPE_HEAD.unpack(head)
            log.debug(
                "scope: %d edges%s; reading edge log", n_edges, " (overflow)" if overflow else ""
            )
            tail = t.read_exact(n_edges * _SCOPE_EDGE.size) if n_edges else b""
        except TransportClosed as exc:
            log.debug("scope: transport closed: %s", exc)
            raise LinkClosed(str(exc)) from exc
        except TransportError as exc:
            log.debug("scope: exchange failed: %s", exc)
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
            log.debug("capture: motion_cmd=%d outside 0..255; refusing", motion_cmd)
            raise ValueError(f"motion command out of range: {motion_cmd}")
        payload = _CAPTURE.pack(motion_cmd, rate, tpt, direction, pass_id, stop.byte_budget or 0)
        log.debug(
            "capture: motion cmd %d, rate %d, tpt %d, dir %d, pass %d, budget %s",
            motion_cmd, rate, tpt, direction, pass_id, stop.byte_budget or "free-run",
        )  # fmt: skip
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

    def _read(self) -> bytes:
        """One stream read, with transport errors mapped to link errors.

        Shared by :meth:`chunks` and :meth:`chunks_for` so both report a dead
        transport the same way: ``TransportClosed`` (a subclass of
        ``TransportError``, so it must be caught first) becomes ``LinkClosed``,
        anything else ``LinkError``.
        """
        try:
            return self._transport.read_stream()
        except TransportClosed as exc:
            log.debug("capture stream: transport closed: %s", exc)
            raise LinkClosed(str(exc)) from exc
        except TransportError as exc:
            log.debug("capture stream read failed: %s", exc)
            raise LinkError(f"capture stream read failed: {exc}") from exc

    def chunks(self) -> Iterator[bytes]:
        """Yield raw flux byte chunks until the stream is exhausted (END / abort)."""
        while not self._closed:
            data = self._read()
            if not data:
                # The device drained the stream (END + NUL, then silence). Mark
                # the session closed so __exit__ doesn't send ABORT -- which no
                # firmware verb implements -- into an idle command channel.
                log.debug("capture stream drained")
                self._closed = True
                break
            yield data

    def chunks_for(self, seconds: float) -> Iterator[bytes]:
        """Yield flux chunks for ``seconds`` of wall time, then abort the session.

        For probes that run the tape under a plain motion command, which (unlike
        Logical Forward) doesn't end at logical EOT. An empty read here does NOT
        end the session the way it does in :meth:`chunks`: a drive with a dead
        read channel sends nothing while the tape is still moving, and that
        silence is exactly what a probe wants to see. The deadline is checked
        between reads, so the run may overshoot by one read timeout (2 s). The
        abort always runs, even when the caller stops iterating early, and
        stops the tape (the firmware's clear-comms path issues Stop Tape).
        """
        deadline = time.monotonic() + seconds
        log.debug("timed capture: streaming for %.1f s", seconds)
        try:
            while time.monotonic() < deadline:
                data = self._read()
                if data:
                    yield data
        except BaseException:
            # A read failed, or the caller stopped early (GeneratorExit). Abort
            # best-effort: on a dead link the abort fails too, and raising that
            # here would replace the error that explains what went wrong.
            log.debug("timed capture interrupted; aborting best-effort")
            self._abort_best_effort()
            raise
        else:
            # Deadline reached on a healthy link: a failed abort may leave the
            # tape moving, so it is the error to report.
            log.debug("timed capture over (%.1f s requested); aborting", seconds)
            self.abort()
        finally:
            self._closed = True

    def abort(self) -> None:
        """Out-of-band stop, valid mid-stream (routes through Quiesce; §5.2)."""
        if self._aborted or self._closed:
            log.debug(
                "abort: already aborted=%s closed=%s; nothing to do", self._aborted, self._closed
            )
            return
        self._aborted = True
        log.debug("aborting capture session")
        try:
            self._transport.send_control(int(Txn.ABORT))
        except TransportError as exc:
            log.debug("abort failed: %s", exc)
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
            log.debug("capture session still live at teardown; aborting")
            self._abort_best_effort()
        self._closed = True

    def _abort_best_effort(self) -> None:
        """Abort, logging rather than raising if it fails (teardown paths)."""
        try:
            self.abort()
        except LinkError as exc:
            # Best-effort; closing the link is the backstop. But a failed
            # abort can leave the tape moving, so a human should hear of it.
            log.warning("could not abort capture on teardown: %s", exc)

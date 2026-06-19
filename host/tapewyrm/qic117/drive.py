"""Qic117Drive — the semantic dispatch layer over a DeviceLink (DESIGN.md §6A.3).

Turns "seek to load point" into the right transaction sequence and decodes what
comes back. Dispatch is by ``Kind`` (the whole point of tagging the table):

* **report**  -> ACK / clock bits / Final (handled device-side via command_txn).
* **motion** (non-streaming) -> wait-ready on a generous timeout, then read status.
* **streaming** (Logical Forward) -> send and return; **NEVER** wait-ready/status
  after it — a status report would swallow segment 0 (DESIGN.md §2.1/§6A.3).
* **mode / config / select** -> state change, no motion, no report.

The command *content* is verbatim through ``link.command_txn``; only the
follow-up policy lives here. Report bytes are converted to ints honoring
``profile.bit_order``.
"""

from __future__ import annotations

from tapewyrm.link.device import DeviceLink
from tapewyrm.qic117 import commands
from tapewyrm.qic117.commands import Cmd, Kind, encode_arg
from tapewyrm.qic117.status import classify_error
from tapewyrm.types import (
    DriveConfig,
    DriveProfile,
    DriveStatus,
    ErrorCode,
    TapeStatus,
)


class DriveError(Exception):
    """A drive-level failure (carries a decoded ErrorCode when available)."""

    def __init__(self, message: str, error: ErrorCode | None = None) -> None:
        super().__init__(message)
        self.error = error


def bits_to_int(raw: bytes, bit_order: str) -> int:
    """Convert report bytes to an int honoring the drive's bit order.

    The device clocks report bits LSB-first off TRK0 and the link returns them as
    little-endian bytes. ``lsb`` therefore concatenates bytes little-endian (the
    natural case); ``msb`` reverses the bit significance within the value.
    """
    value = int.from_bytes(raw, "little")
    order = bit_order.lower()
    if order == "lsb":
        return value
    if order == "msb":
        nbits = len(raw) * 8
        rev = 0
        for i in range(nbits):
            if (value >> i) & 1:
                rev |= 1 << (nbits - 1 - i)
        return rev
    raise ValueError(f"unknown bit_order {bit_order!r} (expected 'lsb' or 'msb')")


class Qic117Drive:
    """Semantic drive layer (DESIGN.md §6A.3)."""

    def __init__(self, link: DeviceLink, profile: DriveProfile) -> None:
        self.link = link
        self.profile = profile
        self._last_error: ErrorCode | None = None

    @property
    def last_error(self) -> ErrorCode | None:
        return self._last_error

    # --- argument emission ---

    def _send_arg(self, cmd: Cmd, arg: int) -> None:
        """Send the command then its operand as N+2 pulse train(s) (DESIGN.md §13.1)."""
        self.link.command_txn(cmd.code)
        for pulses in encode_arg(cmd, arg):
            self.link.command_txn(pulses)

    # --- dispatch ---

    def command(self, cmd: Cmd, arg: int | None = None) -> DriveStatus | None:
        """Dispatch a command by kind (DESIGN.md §6A.3).

        Non-streaming MOTION returns the post-motion ``DriveStatus``; everything
        else returns ``None``. Logical Forward (streaming) is sent and returns
        immediately with NO wait-ready/status.
        """
        if cmd.takes_arg:
            if arg is None:
                raise ValueError(f"command {cmd.name!r} requires an argument")
            self._send_arg(cmd, arg)
        else:
            self.link.command_txn(cmd.code)

        # Streaming motion (Logical Forward): never wait-ready/status — a status
        # report would swallow segment 0 (DESIGN.md §2.1/§6A.3). In practice
        # capture is armed via link.capture(); command() with LF is the bare verb.
        if cmd.kind is Kind.MOTION and not cmd.is_streaming:
            self.link.wait_ready(self.profile.timing.motion_timeout_s * 1000)
            return self.status()
        return None

    def report(self, cmd: Cmd, nbits: int) -> int:
        """Issue a report command and return its ``nbits`` payload as an int.

        The device clocks the ACK (must be TRUE), ``nbits`` LSB-first, then Final
        (FALSE => error); the link raises on a bad ACK/Final and returns the data
        bytes here, which we convert honoring ``profile.bit_order``.
        """
        raw = self.link.command_txn(cmd.code, report_bits=nbits)
        return bits_to_int(raw, self.profile.bit_order)

    # --- status / error ---

    def status(self) -> DriveStatus:
        """Report Drive Status (cmd 6); read+clear error on new-cartridge/error."""
        b = self.report(commands.REPORT_DRIVE_STATUS, 8)
        st = DriveStatus.decode(b)
        if st.new_cartridge or st.error:
            # Both cleared via Report Error Code (errors latch — DESIGN.md §6A.3).
            w = self.report(commands.REPORT_ERROR_CODE, 16)
            code = w & 0xFF
            self._last_error = ErrorCode.decode(w, fatal=classify_error(code))
        return st

    def error(self) -> ErrorCode:
        """Read+clear the latched error code (cmd 7)."""
        w = self.report(commands.REPORT_ERROR_CODE, 16)
        code = w & 0xFF
        ec = ErrorCode.decode(w, fatal=classify_error(code))
        self._last_error = ec
        return ec

    def config(self) -> DriveConfig:
        """Report Drive Configuration (cmd 8) -> data rate."""
        return DriveConfig.decode(self.report(commands.REPORT_DRIVE_CONFIGURATION, 8))

    def tape_status(self) -> TapeStatus:
        """Report Tape Status (cmd 33) -> format + tape type/length."""
        return TapeStatus.decode(self.report(commands.REPORT_TAPE_STATUS, 8))

    def format_segments(self) -> int:
        """Report Format Segments (cmd 37) -> segments per tape track (16b)."""
        return self.report(commands.REPORT_FORMAT_SEGMENTS, 16)

    # --- lifecycle ---

    def wake(self) -> None:
        """Run the profile's wake sequence (DESIGN.md §6A.3).

        Each step is (command-name, arg|None, delay-ms). Unknown command names in
        a profile raise so a typo is caught at bring-up rather than silently
        skipped.

        TODO(bench), DESIGN.md §9 item 3: the real wake timings/sequence per drive
        family are bench-characterized; the profiles ship nominal placeholders.
        """
        import time

        for name, arg, delay_ms in self.profile.wake_sequence:
            cmd = self._lookup(name)
            self.command(cmd, arg=arg)
            if delay_ms:
                time.sleep(delay_ms / 1000.0)

    def reset(self) -> None:
        """Soft Reset (cmd 1) — single pulse; clears state, drops to known mode."""
        self.link.command_txn(commands.SOFT_RESET.code)
        self._last_error = None

    @staticmethod
    def _lookup(name: str) -> Cmd:
        key = name.upper().replace(" ", "_").replace("-", "_")
        cmd = commands.TABLE.get(key)
        if cmd is None:
            raise DriveError(f"unknown command name in profile: {name!r}")
        return cmd

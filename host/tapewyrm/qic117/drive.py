"""Qic117Drive — the semantic dispatch layer over a DeviceLink (DESIGN.md §6A.3).

Turns "seek to load point" into the right transaction sequence and decodes what
comes back. Dispatch is by ``Kind`` (the whole point of tagging the table):

* **report**  -> ACK / clock bits / Final (handled device-side via command_txn).
* **motion** (non-streaming) -> wait-ready on a generous timeout, then read status.
* **streaming** (Logical Forward) -> send and return; **NEVER** wait-ready/status
  after it — a status report would swallow segment 0 (DESIGN.md §2.1/§6A.3).
* **mode / config / select** -> state change, no motion, no report.
* **writes** (Enter Format Mode, Write Reference Burst) -> refused with
  ``DriveError`` unless the drive was built with ``allow_writes=True``. This
  project recovers tapes; nothing should arm the write path by accident.

The command *content* is verbatim through ``link.command_txn``; only the
follow-up policy lives here. Report bytes are converted to ints honoring
``profile.bit_order``.
"""

from __future__ import annotations

import logging

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

log = logging.getLogger(__name__)


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
    log.debug("bit_order %r is neither 'lsb' nor 'msb'; refusing", bit_order)
    raise ValueError(f"unknown bit_order {bit_order!r} (expected 'lsb' or 'msb')")


class Qic117Drive:
    """Semantic drive layer (DESIGN.md §6A.3)."""

    def __init__(
        self, link: DeviceLink, profile: DriveProfile, *, allow_writes: bool = False
    ) -> None:
        self.link = link
        self.profile = profile
        self.allow_writes = allow_writes
        self._last_error: ErrorCode | None = None
        # True while wait_ready()/jog() poll status: report() then stays quiet so
        # a long motion doesn't log two lines per poll (the loops log start and
        # outcome themselves).
        self._polling = False

    @property
    def last_error(self) -> ErrorCode | None:
        return self._last_error

    # --- argument emission ---

    def _send_arg(self, cmd: Cmd, arg: int) -> None:
        """Send the command then its operand as N+2 pulse train(s) (DESIGN.md §13.1)."""
        trains = encode_arg(cmd, arg)
        log.debug("sending %r (code %d) arg %d as pulse trains %s", cmd.name, cmd.code, arg, trains)
        self.link.command_txn(cmd.code)
        for pulses in trains:
            self.link.command_txn(pulses)

    # --- dispatch ---

    def command(self, cmd: Cmd, arg: int | None = None) -> DriveStatus | None:
        """Dispatch a command by kind (DESIGN.md §6A.3).

        Non-streaming MOTION returns the post-motion ``DriveStatus``; everything
        else returns ``None``. Logical Forward (streaming) is sent and returns
        immediately with NO wait-ready/status.
        """
        if cmd.writes and not self.allow_writes:
            log.debug("%r (code %d) writes and allow_writes is False; refusing", cmd.name, cmd.code)
            raise DriveError(
                f"refusing {cmd.name!r} (code {cmd.code}): it writes to tape and this "
                "drive was not opened with allow_writes=True"
            )
        if cmd.takes_arg:
            if arg is None:
                log.debug(
                    "%r (code %d) takes an argument but got None; refusing", cmd.name, cmd.code
                )
                raise ValueError(f"command {cmd.name!r} requires an argument")
            self._send_arg(cmd, arg)
        else:
            log.debug("sending %r (code %d), kind %s", cmd.name, cmd.code, cmd.kind.name)
            self.link.command_txn(cmd.code)

        # Streaming motion (Logical Forward): never wait-ready/status — a status
        # report would swallow segment 0 (DESIGN.md §2.1/§6A.3). In practice
        # capture is armed via link.capture(); command() with LF is the bare verb.
        if cmd.kind is Kind.MOTION and not cmd.is_streaming:
            return self.wait_ready(self._ready_timeout(cmd))
        log.debug("%r is %s, not non-streaming motion; no wait-ready", cmd.name, cmd.kind.name)
        return None

    def _ready_timeout(self, cmd: Cmd) -> float:
        """How long to wait for Ready after ``cmd`` (QIC-117 Rev J Table 2d).

        The spec value is the worst case over every tape length and speed; e.g.
        Seek Load Point is 670 s, and on the bench it took ~28 s even from BOT
        because the drive re-references the tape. The profile's
        ``motion_timeout_s`` only covers commands the table gives no time for.
        A 1 s floor keeps the 200 ms micro-steps from timing out on USB round
        trips alone (each status poll costs a few ms of pulse train + report).
        """
        if cmd.timeout_s is None:
            log.debug(
                "%r has no spec timeout; using profile motion_timeout_s=%s",
                cmd.name,
                self.profile.timing.motion_timeout_s,
            )
            return float(self.profile.timing.motion_timeout_s)
        return max(cmd.timeout_s, 1.0)

    def jog(self, cmd: Cmd, seconds: float, poll_s: float = 0.25) -> DriveStatus:
        """Run Physical Forward/Reverse for ``seconds``, then Stop Tape.

        Physical motion only reports Ready when it reaches an end of tape, so it
        cannot go through ``command()`` (that would wait up to 650 s). Instead
        we poll status, stop early if the drive goes Ready (it hit BOT/EOT or
        stopped on an error; the error bit itself means nothing until Ready),
        and ALWAYS send Stop Tape on the way out -- including
        on Ctrl-C or a link error -- so the tape is never left running.
        Returns the status after the stop.
        """
        import time

        if cmd.code not in (commands.PHYSICAL_FORWARD.code, commands.PHYSICAL_REVERSE.code):
            log.debug(
                "jog() got %r (code %d), not Physical Forward/Reverse; refusing", cmd.name, cmd.code
            )
            raise ValueError(f"jog() takes Physical Forward/Reverse, not {cmd.name!r}")
        log.debug(
            "jog: sending %r (code %d) for %.2f s, polling every %.2f s",
            cmd.name,
            cmd.code,
            seconds,
            poll_s,
        )
        self.link.command_txn(cmd.code)
        try:
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                time.sleep(poll_s)
                self._polling = True
                try:
                    st = self.status()
                finally:
                    self._polling = False
                if st.ready:  # hit BOT/EOT (or stopped on an error -- only valid when ready)
                    log.debug("jog: drive went Ready early (%s); stopping", st)
                    break
        finally:
            log.debug("jog: sending Stop Tape")
            stopped = self.command(commands.STOP_TAPE)
        assert stopped is not None  # Stop Tape is non-streaming motion
        return stopped

    def wait_ready(self, timeout_s: float, poll_s: float = 0.1) -> DriveStatus:
        """Poll Report Drive Status until the ready bit is set (ftape-style).

        QIC-117 has no dedicated ready line: the drive keeps answering status
        reports while it moves, and bit 0 goes TRUE when the operation is done.
        Raises ``DriveError`` on timeout, carrying the last status seen.
        """
        import time

        log.debug("waiting up to %s s for Ready (poll every %s s)", timeout_s, poll_s)
        start = time.monotonic()
        deadline = start + timeout_s
        polls = 0
        while True:
            self._polling = True
            try:
                st = self.status()
            finally:
                self._polling = False
            polls += 1
            if st.ready:
                log.debug("Ready after %.2f s, %d polls: %s", time.monotonic() - start, polls, st)
                return st
            if time.monotonic() >= deadline:
                log.debug("not Ready after %s s, %d polls (last %s); raising", timeout_s, polls, st)
                raise DriveError(f"drive not ready after {timeout_s} s (last status {st})")
            time.sleep(poll_s)

    def report(self, cmd: Cmd, nbits: int) -> int:
        """Issue a report command and return its ``nbits`` payload as an int.

        The device clocks the ACK (must be TRUE), ``nbits`` LSB-first, then Final
        (FALSE => error); the link raises on a bad ACK/Final and returns the data
        bytes here, which we convert honoring ``profile.bit_order``.
        """
        if not self._polling:
            log.debug("report %r (code %d), %d bits", cmd.name, cmd.code, nbits)
        raw = self.link.command_txn(cmd.code, report_bits=nbits)
        value = bits_to_int(raw, self.profile.bit_order)
        if not self._polling:
            log.debug("report %r -> raw %s = 0x%x", cmd.name, raw.hex(), value)
        return value

    # --- status / error ---

    def status(self) -> DriveStatus:
        """Report Drive Status (cmd 6); read+clear error on new-cartridge/error.

        Only while Ready: Rev J says the Error Detected bit "is not valid unless
        the Drive Ready bit is asserted", the error code is "undefined unless
        Error Detected and Drive Ready", and Report Error Code clears the latch
        only "after the drive indicates ready". Mid-motion we just return the
        status and let the next Ready poll clear it.
        """
        b = self.report(commands.REPORT_DRIVE_STATUS, 8)
        st = DriveStatus.decode(b)
        if st.ready and (st.new_cartridge or st.error):
            # Both cleared via Report Error Code (errors latch — DESIGN.md §6A.3).
            log.debug(
                "status ready with new_cartridge=%s error=%s; reading+clearing error code",
                st.new_cartridge,
                st.error,
            )
            w = self.report(commands.REPORT_ERROR_CODE, 16)
            code = w & 0xFF
            self._last_error = ErrorCode.decode(w, fatal=classify_error(code))
            log.debug("latched error: %s", self._last_error)
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
        from dataclasses import replace

        # Push the profile's pulse/report timing first: until now nothing ever
        # called set_timing(), so the firmware always ran its compiled-in
        # defaults and profile timings were dead configuration.
        log.debug(
            "wake: pushing profile %r timing (report_strategy=%s)",
            self.profile.name,
            self.profile.report_strategy,
        )
        self.link.set_timing(
            replace(
                self.profile.timing,
                report_on_index=self.profile.report_strategy == "index_edge",
            )
        )
        for name, arg, delay_ms in self.profile.wake_sequence:
            cmd = self._lookup(name)
            log.debug("wake step: %r arg=%s, then %d ms delay", cmd.name, arg, delay_ms)
            self.command(cmd, arg=arg)
            if delay_ms:
                time.sleep(delay_ms / 1000.0)
        # Read status last: it also reads+clears any latched error / new-cartridge
        # state (power-on leaves error 26 latched), without which the drive
        # rejects many later commands (QIC-117 Rev J §3).
        log.debug("wake: reading status to clear latched error/new-cartridge")
        self.status()

    def reset(self) -> None:
        """Soft Reset (cmd 1) — single pulse; clears state, drops to known mode."""
        log.debug("sending %r (code %d)", commands.SOFT_RESET.name, commands.SOFT_RESET.code)
        self.link.command_txn(commands.SOFT_RESET.code)
        self._last_error = None

    @staticmethod
    def _lookup(name: str) -> Cmd:
        key = name.upper().replace(" ", "_").replace("-", "_")
        cmd = commands.TABLE.get(key)
        if cmd is None:
            log.debug("profile command name %r (key %r) not in table; refusing", name, key)
            raise DriveError(f"unknown command name in profile: {name!r}")
        return cmd

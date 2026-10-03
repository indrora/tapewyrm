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
from collections.abc import Sequence

from tapewyrm_archive.qic117 import DriveConfig, DriveStatus, TapeStatus

from tapewyrm.link.device import (
    DeviceLink,
    LinkClosed,
    LinkError,
    LinkTimeout,
    LinkVersionError,
)
from tapewyrm.qic117 import commands
from tapewyrm.qic117.commands import Cmd, Kind, encode_arg
from tapewyrm.qic117.status import classify_error
from tapewyrm.types import DriveProfile, ErrorCode

log = logging.getLogger(__name__)


class DriveError(Exception):
    """A drive-level failure (carries a decoded ErrorCode when available)."""

    def __init__(self, message: str, error: ErrorCode | None = None) -> None:
        super().__init__(message)
        self.error = error


class WakeUnsupported(DriveError):
    """A wake step needs something this link or board cannot do (e.g. motor lines)."""


#: Wake steps that drive a cable line instead of sending a QIC-117 command
#: (keys as ``_step_key`` spells them). ``DELAY`` does nothing but its own
#: ``delay_ms``; ``MOTOR_ON`` asserts an IBM PC bus unit's drive-select and
#: motor-enable lines (``arg`` = unit, default 0). See ``Qic117Drive._line_step``.
LINE_STEPS = frozenset({"DELAY", "MOTOR_ON"})


def _step_key(name: str) -> str:
    """Normalize a profile step name: "motor on" -> "MOTOR_ON" (as ``_lookup`` does)."""
    return name.strip().upper().replace(" ", "_").replace("-", "_")


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
        # The IBM PC bus unit whose motor a "motor on" wake step switched on,
        # or None; release_lines() switches it back off.
        self.motor_unit: int | None = None

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
        """Run the profile's wake sequence, then read status (DESIGN.md §6A.3).

        Each step is (command-name, arg|None, delay-ms). Unknown command names in
        a profile raise so a typo is caught at bring-up rather than silently
        skipped. Besides QIC-117 commands a step may be a *line* step (see
        ``LINE_STEPS``), which drives a cable line instead of sending pulses.

        TODO(bench), DESIGN.md §9 item 3: the real wake timings/sequence per drive
        family are bench-characterized; the profiles ship nominal placeholders.
        """
        self.run_wake_steps()
        # Read status last: it also reads+clears any latched error / new-cartridge
        # state (power-on leaves error 26 latched), without which the drive
        # rejects many later commands (QIC-117 Rev J §3).
        log.debug("wake: reading status to clear latched error/new-cartridge")
        self.status()

    def run_wake_steps(self) -> None:
        """Push the profile's timing and run its wake steps, without reading status.

        ``wake()`` is this plus a status read; ``auto_wake`` calls it directly so
        it can run ftape's own "did the drive answer?" test afterwards.
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
            if _step_key(name) in LINE_STEPS:
                log.debug("wake line step: %r arg=%s, then %d ms delay", name, arg, delay_ms)
                self._line_step(_step_key(name), arg)
            else:
                cmd = self._lookup(name)
                log.debug("wake step: %r arg=%s, then %d ms delay", cmd.name, arg, delay_ms)
                self.command(cmd, arg=arg)
            if delay_ms:
                time.sleep(delay_ms / 1000.0)

    def _line_step(self, key: str, arg: int | None) -> None:
        """Run one ``LINE_STEPS`` step (a cable line, not a QIC-117 command).

        ``MOTOR_ON`` is ftape's Insight ("Motor-on") wake: such drives are
        enabled by their motor-enable line rather than by a command. ftape sets
        the unit's motor bit in the controller's Digital Output Register; on a
        PC floppy controller that unit's drive-select output follows it, so the
        drive sees DS and MOTEN asserted together. GW's IBM PC bus gives the
        same pair: SELECT asserts the unit's DS line, MOTOR its MOTEN line
        (unit 0 = cable pins 14 + 10, unit 1 = pins 12 + 16).

        TODO(bench): no Insight-wake drive (Irwin 80SX, Insight 80, early Iomega
        250) has been tried; the DS-follows-motor reading of ftape's comment
        ("enable is done by motor-on") is ours, not ftape's.
        """
        if key == "DELAY":
            log.debug("wake: delay step (only its delay_ms)")
            return
        # MOTOR_ON: everything below needs GW's select + motor commands.
        unit = 0 if arg is None else arg
        if not (
            callable(getattr(self.link, "select", None))
            and callable(getattr(self.link, "motor", None))
        ):
            log.debug("wake: link %r has no select()/motor(); refusing motor on", self.link)
            raise WakeUnsupported("this link cannot drive the drive-select/motor-enable lines")
        from tapewyrm.types import SelectHint

        log.debug("wake: motor on: IBM PC bus, unit %d, select + motor-enable", unit)
        # Remember the unit before asking: if GW turns the motor on and then
        # something fails, release_lines() must still switch it off.
        self.motor_unit = unit
        try:
            self.link.select(SelectHint(unit=unit, bus="ibmpc", motor=True))
        except _LINK_FATAL:
            raise
        except LinkError as exc:
            log.debug("wake: board refused motor on for unit %d (%s); unsupported", unit, exc)
            raise WakeUnsupported(f"board refused select + motor on unit {unit}: {exc}") from exc

    def release_lines(self) -> None:
        """Undo the line steps: motor off and drive deselected (ftape's undo).

        ftape turns the motor back off when a Motor-on wake gets no answer, and
        again when it puts the drive to sleep (``ftape_put_drive_to_sleep``). A
        no-op when no wake step turned a motor on.
        """
        if self.motor_unit is None:
            log.debug("release_lines: no motor on; nothing to undo")
            return
        unit, self.motor_unit = self.motor_unit, None
        log.debug("release_lines: motor off on unit %d, then deselect", unit)
        try:
            self.link.motor(unit, False)
        except _LINK_FATAL:
            raise
        except LinkError as exc:
            # NO_BUS / BAD_UNIT: the motor was never switched on.
            log.debug("release_lines: motor off refused (%s); carrying on to deselect", exc)
        self.link.deselect()

    def reset(self) -> None:
        """Soft Reset (cmd 1) — single pulse; clears state, drops to known mode."""
        log.debug("sending %r (code %d)", commands.SOFT_RESET.name, commands.SOFT_RESET.code)
        self.link.command_txn(commands.SOFT_RESET.code)
        self._last_error = None

    @staticmethod
    def _lookup(name: str) -> Cmd:
        key = _step_key(name)
        cmd = commands.TABLE.get(key)
        if cmd is None:
            log.debug("profile command name %r (key %r) not in table; refusing", name, key)
            raise DriveError(f"unknown command name in profile: {name!r}")
        return cmd


# ---------------------------------------------------------------------------
# `--profile auto`: ftape's wake-up methods, in ftape's order
# ---------------------------------------------------------------------------
#
# Precedent: ftape, the Linux floppy-tape driver, as shipped in Linux 2.6.19
# (its last release before removal; the code dates from ftape 3.x/4.x,
# Bas Laarhoven and Claus-Justus Heine, 1993-1997). Read from
# https://raw.githubusercontent.com/torvalds/linux/v2.6.19/drivers/char/ftape/...
#
# * lowlevel/ftape-ctl.c, ftape_activate_drive(): with the drive type unknown
#   it loops ``for (method=no_wake_up; method < NR_ITEMS(methods); ++method)``
#   over WAKEUP_METHODS (include/linux/ftape-vendors.h): None, Colorado,
#   Mountain, Motor-on (wake_up_insight). The first method whose
#   ftape_wakeup_drive() succeeds wins; if none does, "no tape drive found".
#   Nothing is sent between attempts: no deselect, no reset, no delay.
# * lowlevel/ftape-io.c, ftape_wakeup_drive(method):
#     - no_wake_up:       nothing.
#     - wake_up_colorado: QIC_PHANTOM_SELECT (46), then ftape_parameter(0),
#                         i.e. a 0+2 = 2-pulse train ("0 /* ft_drive_sel ?? */").
#     - wake_up_mountain: QIC_SOFT_SELECT (23), 1 ms sleep ("NEEDED"), then
#                         ftape_parameter(18), i.e. 20 pulses.
#     - wake_up_insight:  100 ms sleep, then fdc_motor(1) (motor bit for the
#                         unit in the FDC Digital Output Register; fdc-io.c
#                         sleeps 10 ms after) -- "enable is done by motor-on".
#   then, for every method, ftape_report_raw_drive_status(): success means the
#   drive answered. If that fails after a Motor-on wake, the motor goes back
#   off (fdc_motor(0)); that is the only undo ftape does between attempts.
# * ftape-io.c, ftape_report_raw_drive_status(): Report Drive Status (6),
#   tried up to 4 times (once + 3 retries) while the report fails; a status
#   of 0xff is rejected as "impossible drive status".
# * ftape-io.c, ftape_put_drive_to_sleep(): at release, Colorado sends Phantom
#   Deselect (47), Mountain Soft Deselect (24), Motor-on turns the motor off.
#   ftape never sends these to a drive whose method it did not find.
#
# tw mirrors that: profile.AUTO_ORDER lists one profile per ftape method in
# ftape's order, auto_wake() runs each profile's wake steps, applies ftape's
# answer test, and undoes only the motor. Differences, all deliberate:
#   - tw's colorado profile adds Enter Primary Mode (30) after the select
#     (bench-verified on the Jumbo 350 / 1400; ftape issues it later).
#   - ftape waits up to 300 ms for a report's ACK; tw's firmware waits TACK
#     (``TimingParams.tack_us``).
#   - ftape's phantom/soft-select argument and motor unit follow its device
#     node (/dev/qft0 = unit 0); tw uses unit 0 (``tw drive select --unit``
#     re-addresses Phantom Select).

# Link failures that mean the *board* is gone or wrong, not that the drive is
# silent. auto_wake must not paper over these by moving on to the next profile.
_LINK_FATAL = (LinkClosed, LinkTimeout, LinkVersionError)

#: ftape_report_raw_drive_status: one try plus up to three retries.
_STATUS_TRIES = 4


def _describe_wake(profile: DriveProfile) -> str:
    """One readable line for a wake sequence, e.g. "phantom select 0, enter primary mode"."""
    steps = []
    for name, arg, ms in profile.wake_sequence:
        if _step_key(name) == "DELAY":
            steps.append(f"wait {ms} ms")
        else:
            steps.append(name if arg is None else f"{name} {arg}")
    return ", ".join(steps) or "no wake steps"


def _unsupported_reason(link: DeviceLink, profile: DriveProfile) -> str | None:
    """Why ``link`` cannot run ``profile``'s wake at all, or None if it can."""
    needs_motor = any(_step_key(name) == "MOTOR_ON" for name, _a, _ms in profile.wake_sequence)
    if needs_motor and not (
        callable(getattr(link, "select", None)) and callable(getattr(link, "motor", None))
    ):
        log.debug("auto: %s needs motor lines; link %r has no select()/motor()", profile.name, link)
        return "it needs the motor-enable line and this link cannot drive it"
    return None


def _ftape_answer(drive: Qic117Drive) -> str | None:
    """ftape's "did the drive answer?" test; None if it did, else why not.

    ftape_report_raw_drive_status(): Report Drive Status, retried while the
    report fails (4 tries in all), and a status byte of 0xff is impossible
    (a floating TRK0 line reads as all ones). Board failures propagate.
    """
    why = "no answer"
    for attempt in range(1, _STATUS_TRIES + 1):
        try:
            raw = drive.report(commands.REPORT_DRIVE_STATUS, 8)
        except _LINK_FATAL:
            raise
        except LinkError as exc:
            log.debug("auto: status try %d/%d failed: %s", attempt, _STATUS_TRIES, exc)
            why = str(exc)
            continue
        if raw & 0xFF == 0xFF:
            log.debug("auto: status 0xff on try %d; ftape rejects it, so no retry", attempt)
            return "impossible drive status 0xff"
        log.debug("auto: status 0x%02x on try %d", raw, attempt)
        return None
    log.debug("auto: no status after %d tries", _STATUS_TRIES)
    return why


def auto_wake(link: DeviceLink, candidates: Sequence[DriveProfile]) -> Qic117Drive:
    """Wake the drive with the first candidate profile it answers (``--profile auto``).

    ftape's drive detection (see the comment block above): for each candidate,
    in order, run its wake steps and ask for Report Drive Status, up to 4
    times. The first candidate the drive answers wins and its ``Qic117Drive``
    is returned (status read once more through ``status()``, which also clears
    the power-on error latch, as ``wake()`` would), so later re-wakes in the
    session use the same profile.

    Between attempts tw undoes only what ftape undoes: a motor turned on by a
    "motor on" step goes back off (and the select with it). A Phantom or Soft
    Select is left alone, exactly as ftape leaves it.

    A candidate this link cannot run (a "motor on" step on a link without
    motor control, or a board that refuses the motor command) is skipped with
    an INFO line saying why. Board failures (closed link, transport timeout,
    wrong firmware) propagate at once. If no candidate answers, raises
    ``DriveError`` naming what was tried.

    TODO(bench): only the Colorado method has met hardware (Jumbo 350, 1400).
    None/Mountain/Motor-on, and the order as a whole, have run only against
    the fake boards in tests/test_drive.py and tests/test_cli_drive.py.
    """
    tried: list[str] = []
    for profile in candidates:
        reason = _unsupported_reason(link, profile)
        if reason is not None:
            log.info("auto: skipping %s: %s", profile.name, reason)
            tried.append(f"{profile.name} (skipped)")
            continue
        log.info("auto: trying %s: %s", profile.name, _describe_wake(profile))
        drive = Qic117Drive(link, profile)
        try:
            drive.run_wake_steps()
            why = _ftape_answer(drive)
        except _LINK_FATAL:
            log.debug(
                "auto: link failure while trying %s; not a silent drive, raising", profile.name
            )
            raise
        except WakeUnsupported as exc:
            log.info("auto: skipping %s: %s", profile.name, exc)
            tried.append(f"{profile.name} (skipped)")
            drive.release_lines()
            continue
        except LinkError as exc:
            # A wake *command* failed (not the status test): treat it as no answer.
            why = str(exc)
        if why is None:
            log.info("auto: drive answered %s; using profile %r", profile.name, profile.name)
            drive.status()  # as wake() ends: clear the power-on / new-cartridge latch
            return drive
        log.info("auto: %s: no answer (%s)", profile.name, why)
        tried.append(profile.name)
        if drive.motor_unit is not None:
            log.info("auto: %s: motor off and deselect (ftape's undo)", profile.name)
            drive.release_lines()
        else:
            log.debug("auto: %s: ftape undoes nothing after this wake; moving on", profile.name)
    log.debug("auto: no candidate answered (tried %s); raising", tried)
    raise DriveError(
        f"no drive answered auto-detection (tried: {', '.join(tried) or 'nothing'}). "
        "Check power and cabling, or pass --profile NAME (see `tw drive --help`)"
    )

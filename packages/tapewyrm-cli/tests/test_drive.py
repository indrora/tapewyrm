"""Qic117Drive over a mock DeviceLink (DESIGN.md §6A.3).

The mock replays a scripted transaction log: each ``command_txn`` call is logged
and the next queued report value (if any) is returned. This lets us assert on the
exact sequence of bus operations the drive emits — including the
no-status-after-LOGICAL_FORWARD rule (DESIGN.md §2.1/§6A.3).
"""

import logging
import struct
from collections import deque

import pytest

from tapewyrm.qic117 import commands
from tapewyrm.qic117.drive import DriveError, Qic117Drive, bits_to_int
from tapewyrm.types import DriveProfile


class MockLink:
    """Scriptable stand-in for DeviceLink (records calls, replays reports)."""

    def __init__(self):
        self.calls: list[tuple] = []
        self._reports: deque[bytes] = deque()
        self._ready: deque[bool] = deque()
        self.info = None

    # scripting
    def queue_report(self, value: int, nbytes: int) -> None:
        self._reports.append(value.to_bytes(nbytes, "little"))

    def queue_ready(self, ready: bool) -> None:
        self._ready.append(ready)

    # DeviceLink surface used by the drive
    def command_txn(self, n: int, report_bits: int = 0) -> bytes:
        self.calls.append(("command_txn", n, report_bits))
        if report_bits:
            if not self._reports:
                raise AssertionError(f"no queued report for command {n} ({report_bits} bits)")
            return self._reports.popleft()
        return b""

    def set_timing(self, t) -> None:
        self.calls.append(("set_timing", t))

    def wait_ready(self, timeout_ms: int) -> bool:
        self.calls.append(("wait_ready", timeout_ms))
        return self._ready.popleft() if self._ready else True

    def capture(self, motion_cmd: int, stop):
        self.calls.append(("capture", motion_cmd))
        raise AssertionError("drive layer should not open capture directly")

    # convenience views
    def command_codes(self) -> list[int]:
        return [c[1] for c in self.calls if c[0] == "command_txn"]

    def opnames(self) -> list[str]:
        return [c[0] for c in self.calls]


def _drive(profile: DriveProfile | None = None) -> tuple[Qic117Drive, MockLink]:
    link = MockLink()
    return Qic117Drive(link, profile or DriveProfile.default()), link


# ---------------------------------------------------------------------------
# bits_to_int / bit order
# ---------------------------------------------------------------------------


def test_bits_to_int_lsb():
    assert bits_to_int(bytes([0xA5]), "lsb") == 0xA5
    assert bits_to_int(struct.pack("<H", 0xBEEF), "lsb") == 0xBEEF


def test_bits_to_int_msb_reverses_bits():
    # 0b0000_0001 (lsb) reversed over 8 bits -> 0b1000_0000.
    assert bits_to_int(bytes([0x01]), "msb") == 0x80


# ---------------------------------------------------------------------------
# Dispatch by kind
# ---------------------------------------------------------------------------


def test_report_dispatch_clocks_bits():
    drive, link = _drive()
    link.queue_report(0x41, 1)
    val = drive.report(commands.REPORT_DRIVE_STATUS, 8)
    assert val == 0x41
    assert link.calls == [("command_txn", 6, 8)]


def test_non_streaming_motion_polls_status_until_ready(monkeypatch):
    # QIC-117 has no ready line: the drive layer polls Report Drive Status (ftape-style).
    monkeypatch.setattr("time.sleep", lambda s: None)
    drive, link = _drive()
    link.queue_report(0b0000_0100, 1)  # cartridge, NOT ready (still moving)
    link.queue_report(0b0100_0101, 1)  # ready + cartridge + at_bot, no error
    st = drive.command(commands.SEEK_LOAD_POINT)
    assert st is not None and st.ready and st.at_bot
    # Motion pulse 14, then status reports until ready. No firmware WAIT_READY.
    assert link.command_codes() == [14, 6, 6]
    assert "wait_ready" not in link.opnames()


def test_wait_ready_times_out(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    drive, link = _drive()
    for _ in range(3):
        link.queue_report(0b0000_0100, 1)  # never ready
    with pytest.raises(DriveError):
        drive.wait_ready(timeout_s=0)


def test_seek_head_to_track_sends_arg_as_n_plus_2():
    drive, link = _drive()
    link.queue_report(0b0000_0001, 1)  # ready
    drive.command(commands.SEEK_HEAD_TO_TRACK, arg=5)
    # command pulse 13, then arg pulse train 5+2=7, then the ready poll (status 6).
    assert link.command_codes() == [13, 7, 6]


def test_seek_head_to_track_requires_arg():
    drive, _link = _drive()
    with pytest.raises(ValueError):
        drive.command(commands.SEEK_HEAD_TO_TRACK)


# ---------------------------------------------------------------------------
# The no-status-after-LOGICAL_FORWARD rule
# ---------------------------------------------------------------------------


def test_no_status_after_logical_forward():
    drive, link = _drive()
    out = drive.command(commands.LOGICAL_FORWARD)
    assert out is None
    # Only the single LF pulse train; NO wait_ready and NO status report.
    assert link.command_codes() == [10]
    assert "wait_ready" not in link.opnames()


# ---------------------------------------------------------------------------
# status -> error read+clear
# ---------------------------------------------------------------------------


def test_status_reads_and_clears_error():
    drive, link = _drive()
    link.queue_report(0b0000_0011, 1)  # ready + error
    link.queue_report((10 << 0) | (13 << 8), 2)  # error code 10 (broken tape), cmd 13
    st = drive.status()
    assert st.error
    # status read 8 bits, then error code read 16 bits.
    assert link.calls == [("command_txn", 6, 8), ("command_txn", 7, 16)]
    assert drive.last_error is not None
    assert drive.last_error.code == 10
    assert drive.last_error.associated_command == 13
    assert drive.last_error.fatal is True  # broken tape is fatal


def test_status_reads_error_on_new_cartridge():
    drive, link = _drive()
    link.queue_report(0b0001_0001, 1)  # ready + new_cartridge
    link.queue_report(26 | (0 << 8), 2)  # 26 = Power On Reset Occurred (benign)
    st = drive.status()
    assert st.new_cartridge
    assert link.command_codes() == [6, 7]
    assert drive.last_error is not None
    assert drive.last_error.code == 26
    assert drive.last_error.fatal is False


def test_status_no_error_no_clear():
    drive, link = _drive()
    link.queue_report(0b0000_0101, 1)  # ready + cartridge, no error/new
    st = drive.status()
    assert not st.error and not st.new_cartridge
    assert link.command_codes() == [6]  # only the status read


# ---------------------------------------------------------------------------
# config / tape_status / format_segments
# ---------------------------------------------------------------------------


def test_config_decodes_rate():
    drive, link = _drive()
    link.queue_report(0b1001_0000, 1)  # bits 3-4 = 0b10 -> 500 kbps; bit7 qic80
    cfg = drive.config()
    assert cfg.rate_kbps == 500
    assert cfg.qic80_mode


def test_tape_status_decodes_format():
    drive, link = _drive()
    link.queue_report(0x02, 1)  # format bits 0-3 = 2 -> QIC80
    tape = drive.tape_status()
    assert tape.format.name == "QIC80"


def test_format_segments_16_bits():
    drive, link = _drive()
    link.queue_report(207, 2)
    assert drive.format_segments() == 207


# ---------------------------------------------------------------------------
# wake / reset
# ---------------------------------------------------------------------------


def test_wake_runs_profile_sequence(monkeypatch):
    # Profile with a 2-step wake; ensure both commands are sent (no real sleep).
    prof = DriveProfile(
        name="t",
        wake_sequence=(("soft reset", None, 0), ("enter primary mode", None, 0)),
        timing=DriveProfile.default().timing,
    )
    drive, link = _drive(prof)
    link.queue_report(0b0000_0101, 1)  # final status read: ready + cartridge
    drive.wake()
    # soft reset = 1, enter primary mode = 30, then the error-clearing status (6).
    assert link.command_codes() == [1, 30, 6]


def test_wake_phantom_select_sends_unit_arg():
    # Bench-confirmed Colorado wake: Phantom Select 46 + N+2 unit train (unit 0 -> 2).
    prof = DriveProfile(
        name="t",
        wake_sequence=(("phantom select", 0, 0),),
        timing=DriveProfile.default().timing,
    )
    drive, link = _drive(prof)
    link.queue_report(0b0000_0101, 1)
    drive.wake()
    assert link.command_codes() == [46, 2, 6]


def test_wake_unknown_command_raises():
    prof = DriveProfile(
        name="t",
        wake_sequence=(("nonexistent cmd", None, 0),),
        timing=DriveProfile.default().timing,
    )
    drive, _link = _drive(prof)
    with pytest.raises(DriveError):
        drive.wake()


def test_reset_sends_soft_reset():
    drive, link = _drive()
    drive.reset()
    assert link.command_codes() == [1]
    assert drive.last_error is None


# ---------------------------------------------------------------------------
# write guard
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["ENTER_FORMAT_MODE", "WRITE_REFERENCE_BURST"])
def test_write_commands_refused_by_default(name):
    drive, link = _drive()
    with pytest.raises(DriveError, match="writes to tape"):
        drive.command(commands.TABLE[name])
    assert link.calls == []  # nothing reached the bus


def test_write_commands_allowed_when_opted_in():
    link = MockLink()
    drive = Qic117Drive(link, DriveProfile.default(), allow_writes=True)
    drive.command(commands.TABLE["ENTER_FORMAT_MODE"])
    assert link.command_codes() == [15]


def test_refused_set_is_writes_plus_diagnostic_modes():
    # 15/16 put flux on tape; 28/29 are manufacturer-dependent (Rev J p.17).
    assert {c.code for c in commands.BY_CODE.values() if c.writes} == {15, 16, 28, 29}


# ---------------------------------------------------------------------------
# jog (Physical Forward/Reverse for N seconds)
# ---------------------------------------------------------------------------


def test_jog_stops_early_at_end_of_tape(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    drive, link = _drive()
    link.queue_report(0b0100_0101, 1)  # poll: ready + at_bot -> we hit the end
    link.queue_report(0b0100_0101, 1)  # status after Stop
    st = drive.jog(commands.PHYSICAL_REVERSE, seconds=60)
    assert st.at_bot
    assert link.command_codes() == [11, 6, 18, 6]


def test_jog_always_sends_stop_even_when_polling_fails(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    drive, link = _drive()
    # No queued reports: the first status poll raises mid-motion.
    with pytest.raises(AssertionError):
        drive.jog(commands.PHYSICAL_FORWARD, seconds=60)
    assert link.command_codes()[:2] == [12, 6]
    assert 18 in link.command_codes()  # Stop Tape went out regardless


def test_jog_rejects_non_physical_motion():
    drive, _link = _drive()
    with pytest.raises(ValueError):
        drive.jog(commands.SEEK_LOAD_POINT, seconds=1)


# ---------------------------------------------------------------------------
# Rev J audit (docs/qic117j.pdf)
# ---------------------------------------------------------------------------


def test_status_ignores_error_bit_while_not_ready():
    # Error Detected "is not valid unless the Drive Ready bit is asserted", and
    # Report Error Code only clears it "after the drive indicates ready".
    drive, link = _drive()
    link.queue_report(0b0000_0110, 1)  # error + cartridge, NOT ready (moving)
    st = drive.status()
    assert st.error and not st.ready
    assert link.command_codes() == [6]  # no Report Error Code
    assert drive.last_error is None


def test_jog_does_not_stop_on_error_bit_while_moving(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    drive, link = _drive()
    link.queue_report(0b0000_0110, 1)  # moving, (meaningless) error bit
    link.queue_report(0b0100_0101, 1)  # ready at BOT -> stop polling
    link.queue_report(0b0100_0101, 1)  # status after Stop
    drive.jog(commands.PHYSICAL_REVERSE, seconds=60)
    assert link.command_codes() == [11, 6, 6, 18, 6]


@pytest.mark.parametrize("strategy,on_index", [("fixed_settle", False), ("index_edge", True)])
def test_wake_pushes_profile_timing_first(strategy, on_index):
    prof = DriveProfile(
        name="t",
        wake_sequence=(("enter primary mode", None, 0),),
        timing=DriveProfile.default().timing,
        report_strategy=strategy,
    )
    drive, link = _drive(prof)
    link.queue_report(0b0000_0101, 1)
    drive.wake()
    assert link.calls[0][0] == "set_timing"
    assert link.calls[0][1].report_on_index is on_index
    assert link.calls[0][1].pulse_us == prof.timing.pulse_us


def test_non_interruptible_flags_match_table_2a():
    # (n) in Rev J Table 2a: 3, 4, 14, 16, 18, 25, 26, 34, 35, 36. Nothing else.
    flagged = {c.code for c in commands.BY_CODE.values() if c.non_intr}
    assert flagged == {3, 4, 14, 16, 18, 25, 26, 34, 35, 36}


# ---------------------------------------------------------------------------
# --profile auto: auto_wake runs ftape's wake-up methods in ftape's order
# ---------------------------------------------------------------------------


class BusLink(MockLink):
    """A floppy-tape cable with at most one drive on it, and no motor control.

    ``kind`` says how the drive wakes, one per ftape method:

    * ``"listening"`` -- always answers (ftape "None").
    * ``"phantom"``   -- after Phantom Select 46 + the ``unit``+2 train; 47 or
      Soft Reset (1) releases it (ftape "Colorado").
    * ``"soft"``      -- after Soft Select 23 + its 20-pulse train; 24 or 1
      releases it (ftape "Mountain").
    * ``"motor"``     -- while its unit's select + motor lines are on (ftape
      "Motor-on"; needs ``MotorBusLink``).
    * ``None``        -- no drive at all.

    Unselected, a report gets no ACK (LinkError), as on the real bus.
    ``events()`` lists command pulse counts and line operations in order.
    """

    def __init__(self, kind: str | None = None, unit: int = 0, status: int = 0x25):
        super().__init__()
        self.kind, self.unit, self.status_byte = kind, unit, status
        self.selected = kind == "listening"
        self.motor_unit: int | None = None
        self._after: int | None = None  # 46 or 23 waiting for its argument train

    def command_txn(self, n: int, report_bits: int = 0) -> bytes:
        from tapewyrm.link.device import LinkError

        self.calls.append(("command_txn", n, report_bits))
        if self._after is not None:
            after, self._after = self._after, None
            if after == 46 and self.kind == "phantom" and n == self.unit + 2:
                self.selected = True
            if after == 23 and self.kind == "soft" and n == 20:
                self.selected = True
            return b""
        if n in (46, 23):
            self._after = n
        elif n in (1, 47, 24) and self.kind in ("phantom", "soft"):
            self.selected = False
        if not report_bits:
            return b""
        listening = self.selected or (self.kind == "motor" and self.motor_unit == self.unit)
        if not listening:
            raise LinkError(f"command {n}: no ACK bit -- drive not selected/listening")
        return bytes([self.status_byte])

    def events(self) -> list:
        out: list = []
        for call in self.calls:
            if call[0] == "command_txn":
                out.append(call[1])
            elif call[0] != "set_timing":
                out.append(call)
        return out


class MotorBusLink(BusLink):
    """``BusLink`` plus GW's select / motor / deselect, as ``DeviceLink`` has."""

    def select(self, hint) -> None:
        self.calls.append(("select", hint.bus, hint.unit, hint.motor))
        if hint.motor:
            self.motor_unit = hint.unit

    def motor(self, unit: int, on: bool) -> None:
        self.calls.append(("motor", unit, on))
        self.motor_unit = unit if on else None

    def deselect(self) -> None:
        self.calls.append(("deselect",))


NO_ANSWER = [6, 6, 6, 6]  # ftape_report_raw_drive_status: 4 tries, then give up
INSIGHT_ON = ("select", "ibmpc", 0, True)
INSIGHT_OFF = [("motor", 0, False), ("deselect",)]


@pytest.fixture
def ftape_candidates(monkeypatch):
    """The real AUTO_ORDER profiles, with wake delays made instant."""
    import time

    from tapewyrm.qic117.profile import auto_candidates

    monkeypatch.setattr(time, "sleep", lambda _s: None)
    return auto_candidates()


@pytest.fixture
def tw_info_logs(monkeypatch, caplog):
    # The CLI's setup_logging turns propagation off for "tapewyrm"; re-enable
    # it so caplog (on the root logger) sees the auto-detect INFO lines.
    monkeypatch.setattr(logging.getLogger("tapewyrm"), "propagate", True)
    caplog.set_level(logging.INFO, logger="tapewyrm")
    return caplog


def _info(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]


def test_auto_order_is_ftapes_wakeup_methods_in_ftapes_order(ftape_candidates):
    # ftape-ctl.c ftape_activate_drive() walks WAKEUP_METHODS: None, Colorado,
    # Mountain, Motor-on. Changing this list means departing from ftape.
    from tapewyrm.qic117.profile import AUTO_ORDER

    assert AUTO_ORDER == ("default", "colorado", "mountain", "insight")
    wakes = {p.name: [(n, a) for n, a, _ms in p.wake_sequence] for p in ftape_candidates}
    assert wakes == {
        "default": [],
        "colorado": [("phantom select", 0), ("enter primary mode", None)],
        "mountain": [("soft select", 18)],
        "insight": [("delay", None), ("motor on", 0)],
    }


def test_auto_with_no_drive_tries_every_method_in_order_and_undoes_only_the_motor(
    ftape_candidates, tw_info_logs
):
    link = MotorBusLink(kind=None)
    with pytest.raises(DriveError, match="tried: default, colorado, mountain, insight"):
        from tapewyrm.qic117.drive import auto_wake

        auto_wake(link, ftape_candidates)
    # As ftape: no Phantom Deselect (47) or Soft Deselect (24) between
    # attempts; only the Motor-on wake's motor goes back off.
    assert link.events() == [
        *NO_ANSWER,  # None
        46, 2, 30, *NO_ANSWER,  # Colorado (+ tw's Enter Primary Mode)
        23, 20, *NO_ANSWER,  # Mountain
        INSIGHT_ON, *NO_ANSWER, *INSIGHT_OFF,  # Motor-on
    ]  # fmt: skip
    messages = _info(tw_info_logs)
    assert "auto: trying default: no wake steps" in messages
    assert "auto: trying colorado: phantom select 0, enter primary mode" in messages
    assert "auto: trying mountain: soft select 18" in messages
    assert "auto: trying insight: wait 100 ms, motor on 0" in messages
    assert "auto: insight: motor off and deselect (ftape's undo)" in messages
    assert sum(m.startswith(("auto: default: no answer", "auto: colorado: no answer",
                             "auto: mountain: no answer", "auto: insight: no answer"))
               for m in messages) == 4  # fmt: skip


@pytest.mark.parametrize(
    ("kind", "winner", "events"),
    [
        ("listening", "default", [6, 6]),
        ("phantom", "colorado", [*NO_ANSWER, 46, 2, 30, 6, 6]),
        ("soft", "mountain", [*NO_ANSWER, 46, 2, 30, *NO_ANSWER, 23, 20, 6, 6]),
        (
            "motor",
            "insight",
            [*NO_ANSWER, 46, 2, 30, *NO_ANSWER, 23, 20, *NO_ANSWER, INSIGHT_ON, 6, 6],
        ),
    ],
)
def test_auto_stops_at_the_first_method_the_drive_answers(
    ftape_candidates, tw_info_logs, kind, winner, events
):
    from tapewyrm.qic117.drive import auto_wake

    link = MotorBusLink(kind=kind)
    drive = auto_wake(link, ftape_candidates)
    assert drive.profile.name == winner and drive.link is link
    # Answer test (6), then status() once more to clear the latch; nothing
    # after the winner, and the winner's select/motor is left in place.
    assert link.events() == events
    assert f"auto: drive answered {winner}; using profile {winner!r}" in _info(tw_info_logs)


def test_auto_turns_the_motor_off_before_the_next_method(monkeypatch):
    import time

    from tapewyrm.qic117.drive import auto_wake
    from tapewyrm.qic117.profile import load_profile

    monkeypatch.setattr(time, "sleep", lambda _s: None)
    link = MotorBusLink(kind="phantom")
    drive = auto_wake(link, [load_profile("insight"), load_profile("colorado")])
    assert drive.profile.name == "colorado"
    assert link.events() == [INSIGHT_ON, *NO_ANSWER, *INSIGHT_OFF, 46, 2, 30, 6, 6]
    assert link.motor_unit is None


def test_auto_retries_status_like_ftape():
    # ftape_report_raw_drive_status retries a failed report 3 times.
    from tapewyrm.link.device import LinkError
    from tapewyrm.qic117.drive import auto_wake

    class SlowToAnswer(BusLink):
        misses = 2

        def command_txn(self, n, report_bits=0):
            if report_bits and self.misses:
                self.misses -= 1
                self.calls.append(("command_txn", n, report_bits))
                raise LinkError("no ACK bit")
            return super().command_txn(n, report_bits)

    link = SlowToAnswer(kind="listening")
    drive = auto_wake(link, [DriveProfile.default()])
    assert drive.profile.name == "default"
    assert link.events() == [6, 6, 6, 6]  # 2 misses, the answer, then status()


def test_auto_rejects_status_ff_without_retrying():
    # A floating TRK0 reads as all ones; ftape calls 0xff "impossible".
    from tapewyrm.qic117.drive import auto_wake

    link = BusLink(kind="listening", status=0xFF)
    with pytest.raises(DriveError, match="tried: default"):
        auto_wake(link, [DriveProfile.default()])
    assert link.events() == [6]


def test_auto_skips_motor_on_when_the_link_has_no_motor_control(ftape_candidates, tw_info_logs):
    from tapewyrm.qic117.drive import auto_wake

    link = BusLink(kind=None)  # no select()/motor()
    with pytest.raises(
        DriveError, match=r"tried: default, colorado, mountain, insight \(skipped\)"
    ):
        auto_wake(link, ftape_candidates)
    assert link.events() == [*NO_ANSWER, 46, 2, 30, *NO_ANSWER, 23, 20, *NO_ANSWER]
    assert (
        "auto: skipping insight: it needs the motor-enable line and this link cannot drive it"
        in _info(tw_info_logs)
    )


def test_auto_skips_motor_on_when_the_board_refuses_it(ftape_candidates, tw_info_logs):
    from tapewyrm.link.device import LinkError
    from tapewyrm.qic117.drive import auto_wake

    class NoIbmPcBus(MotorBusLink):
        def select(self, hint):
            self.calls.append(("select", hint.bus, hint.unit, hint.motor))
            raise LinkError("command 0x0c rejected: ACK_BAD_UNIT")

        def motor(self, unit, on):
            self.calls.append(("motor", unit, on))
            raise LinkError("command 0x06 rejected: ACK_NO_BUS")

    link = NoIbmPcBus(kind=None)
    with pytest.raises(DriveError, match=r"insight \(skipped\)"):
        auto_wake(link, ftape_candidates)
    assert link.events()[-3:] == [INSIGHT_ON, *INSIGHT_OFF]  # undo still attempted
    assert any(m.startswith("auto: skipping insight: board refused") for m in _info(tw_info_logs))


def test_auto_wake_does_not_skip_past_a_dead_link(ftape_candidates):
    from tapewyrm.link.device import LinkClosed
    from tapewyrm.qic117.drive import auto_wake

    class DeadLink(MockLink):
        def command_txn(self, n, report_bits=0):
            self.calls.append(("command_txn", n, report_bits))
            raise LinkClosed("device link is not open")

    link = DeadLink()
    with pytest.raises(LinkClosed):
        auto_wake(link, ftape_candidates)
    assert link.command_codes() == [6]  # gave up at once, tried nothing else


def test_motor_on_wake_step_selects_and_releases(monkeypatch):
    import time

    from tapewyrm.qic117.profile import load_profile

    monkeypatch.setattr(time, "sleep", lambda _s: None)
    link = MotorBusLink(kind="motor")
    drive = Qic117Drive(link, load_profile("insight"))
    drive.wake()
    assert link.events() == [INSIGHT_ON, 6] and drive.motor_unit == 0
    drive.release_lines()
    assert link.events()[-2:] == INSIGHT_OFF and drive.motor_unit is None

"""Qic117Drive over a mock DeviceLink (DESIGN.md §6A.3).

The mock replays a scripted transaction log: each ``command_txn`` call is logged
and the next queued report value (if any) is returned. This lets us assert on the
exact sequence of bus operations the drive emits — including the
no-status-after-LOGICAL_FORWARD rule (DESIGN.md §2.1/§6A.3).
"""

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
    # QIC-117 has no ready line: the drive polls Report Drive Status (ftape-style).
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

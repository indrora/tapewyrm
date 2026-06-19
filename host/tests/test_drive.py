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


def test_non_streaming_motion_waits_ready_and_reads_status():
    drive, link = _drive()
    link.queue_ready(True)
    link.queue_report(0b0100_0101, 1)  # ready + cartridge + at_bot, no error
    st = drive.command(commands.SEEK_LOAD_POINT)
    assert st is not None and st.ready
    # Sequence: command pulse (14, 0), wait_ready, status report (6, 8).
    assert link.opnames() == ["command_txn", "wait_ready", "command_txn"]
    assert link.command_codes() == [14, 6]


def test_seek_head_to_track_sends_arg_as_n_plus_2():
    drive, link = _drive()
    link.queue_ready(True)
    link.queue_report(0b0000_0001, 1)  # ready
    drive.command(commands.SEEK_HEAD_TO_TRACK, arg=5)
    # command pulse 13, then arg pulse train 5+2=7, then wait_ready, then status.
    assert link.command_codes()[:2] == [13, 7]
    assert "wait_ready" in link.opnames()


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
    link.queue_report((1 << 0) | (0 << 8), 2)  # reset-occurred (benign)
    st = drive.status()
    assert st.new_cartridge
    assert link.command_codes() == [6, 7]
    assert drive.last_error is not None
    assert drive.last_error.code == 1
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
    drive.wake()
    # soft reset = 1, enter primary mode = 30.
    assert link.command_codes() == [1, 30]


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

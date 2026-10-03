"""tw dump winds to each track's starting end before Logical Forward."""

import pytest
from tapewyrm_archive.qic117 import DriveStatus
from tapewyrm_archive.types import CaptureHeader, Direction, TapeFormat

from tapewyrm.qic117 import commands
from tapewyrm.tape.dump import DumpStopped, tracks_for_tape, wind_to_track_start

# Report Drive Status bits (Rev J Table 2c): 0 ready, 2 cartridge, 5 referenced,
# 6 at BOT, 7 at EOT, 1 error.
AT_BOT = DriveStatus.decode(0b0110_0101)
AT_EOT = DriveStatus.decode(0b1010_0101)
MID_TAPE = DriveStatus.decode(0b0010_0101)


class StubDrive:
    def __init__(self, result: DriveStatus) -> None:
        self.result = result
        self.sent: list[str] = []
        self.last_error = None

    def command(self, cmd, arg=None):
        self.sent.append(cmd.name)
        return self.result


@pytest.mark.parametrize(
    ("track", "motion", "result"),
    [
        (0, commands.PHYSICAL_REVERSE, AT_BOT),  # even: runs toward EOT, starts at BOT
        (1, commands.PHYSICAL_FORWARD, AT_EOT),  # odd: runs toward BOT, starts at EOT
        (12, commands.PHYSICAL_REVERSE, AT_BOT),
    ],
)
def test_winds_toward_the_end_the_track_starts_at(track, motion, result):
    d = StubDrive(result)
    assert wind_to_track_start(d, track) is result
    assert d.sent == [motion.name]


@pytest.mark.parametrize(("track", "result"), [(0, MID_TAPE), (1, AT_BOT)])
def test_refuses_to_read_when_not_at_the_starting_end(track, result):
    with pytest.raises(DumpStopped):
        wind_to_track_start(StubDrive(result), track)


# ---------------------------------------------------------------------------
# `tw dump OUTDIR` with no TRACKS: every track of the reported format
# ---------------------------------------------------------------------------


def _identity(fmt: TapeFormat, tape_status: int | None) -> CaptureHeader:
    return CaptureHeader(
        rate_kbps=500, sample_clock_hz=72_000_000, track=0, direction=Direction.FORWARD,
        pass_id=1, utc="", tape_format=fmt, tape_status=tape_status,
    )  # fmt: skip


@pytest.mark.parametrize(
    ("fmt", "tape_status", "count"),
    [
        (TapeFormat.QIC80, 0x02, 28),  # narrow QIC-80: tracks 0..27
        (TapeFormat.QIC80, 0x82, 36),  # bit 7: wide (0.315 in) cartridge
        (TapeFormat.QIC40, 0x01, 20),
        (TapeFormat.QIC3020, None, 40),  # no tape status: assume narrow
    ],
)
def test_default_tracks_are_every_track_of_the_reported_format(fmt, tape_status, count):
    assert tracks_for_tape(_identity(fmt, tape_status)) == list(range(count))


def test_default_tracks_refuse_an_unknown_format():
    """No guessing: an unreported format means the user must name TRACKS."""
    with pytest.raises(DumpStopped, match="TRACKS|name the tracks"):
        tracks_for_tape(_identity(TapeFormat.UNKNOWN, None))

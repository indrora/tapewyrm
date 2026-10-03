"""tw dump winds to each track's starting end before Logical Forward."""

import pytest

from tapewyrm.qic117 import commands
from tapewyrm.tape.dump import DumpStopped, wind_to_track_start
from tapewyrm.types import DriveStatus

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

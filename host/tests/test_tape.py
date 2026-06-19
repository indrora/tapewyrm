"""TapeTransport over a mock drive/link (DESIGN.md §6A.4).

Covers walk_all ordering, capture_pass building a valid RawFluxCapture from a
synthesized device flux stream (SESSION_START / SEGMENT / END markers via
frame_marker), serpentine direction recording, and the broken-tape guard.
"""

import struct
from contextlib import AbstractContextManager

import pytest

from tapewyrm.rawflux import RawFluxCapture, frame_marker
from tapewyrm.rawflux.container import flux_checksum, flux_data_only, stuff_flux
from tapewyrm.tape.geometry import Geometry
from tapewyrm.tape.transport import TapeError, TapeTransport
from tapewyrm.types import (
    Direction,
    DriveConfig,
    DriveStatus,
    ErrorCode,
    MarkerKind,
    TapeFormat,
    TapeStatus,
)

# ---------------------------------------------------------------------------
# Synthesize a device flux stream the way the firmware would emit it
# ---------------------------------------------------------------------------


def _synth_flux(tpt: int, direction: int, n_segments: int = 3) -> bytes:
    raw_flux = b"\x01\x02\x03"  # a little raw flux between markers
    out = bytearray()
    out += frame_marker(
        MarkerKind.SESSION_START,
        struct.pack("<HIHBH", 500, 72_000_000, tpt, direction, 0),
    )
    for i in range(n_segments):
        out += stuff_flux(raw_flux)
        out += frame_marker(MarkerKind.SEGMENT, struct.pack("<II", 100 + i, i))
    data = flux_data_only(bytes(out) + stuff_flux(raw_flux))
    out += stuff_flux(raw_flux)
    out += frame_marker(
        MarkerKind.END, struct.pack("<BIII", 0, n_segments, len(data), flux_checksum(data))
    )
    return bytes(out)


class FakeCaptureStream(AbstractContextManager):
    def __init__(self, flux: bytes):
        self._flux = flux
        self.exited = False

    def chunks(self):
        # Deliver in a couple of chunks to exercise concatenation.
        mid = len(self._flux) // 2
        yield self._flux[:mid]
        yield self._flux[mid:]

    def abort(self):
        pass

    def __exit__(self, *exc):
        self.exited = True
        return None


class FakeLink:
    def __init__(self):
        self.info = None
        self.captures: list[int] = []
        self._flux_for_track = {}

    def set_flux(self, tpt: int, flux: bytes):
        self._flux_for_track[tpt] = flux

    def capture(self, motion_cmd: int, stop):
        self.captures.append(motion_cmd)
        return FakeCaptureStream(self._current_flux)


class FakeDrive:
    """Mock Qic117Drive: records commands, serves canned status/config/geometry."""

    def __init__(self, link: FakeLink, geom: Geometry, fmt=TapeFormat.QIC80):
        self.link = link
        self._geom = geom
        self._fmt = fmt
        self.commands_run: list[tuple] = []
        self.last_error: ErrorCode | None = None
        self._status_queue: list[DriveStatus] = []

    # surface used by TapeTransport
    def wake(self):
        self.commands_run.append(("wake",))

    def config(self) -> DriveConfig:
        return DriveConfig.decode(0b1001_0000)  # 500 kbps, qic80

    def tape_status(self) -> TapeStatus:
        return TapeStatus(format=self._fmt, tape_type=0, wide=False)

    def format_segments(self) -> int:
        return self._geom.segments_per_track

    def command(self, cmd, arg=None):
        self.commands_run.append((cmd.name, arg))
        # set the flux the FakeLink will serve for the upcoming capture
        if cmd.name == "seek head to track":
            self.link._current_flux = self.link._flux_for_track.get(arg, b"")
        return None

    def status(self) -> DriveStatus:
        if self._status_queue:
            return self._status_queue.pop(0)
        return DriveStatus.decode(0b0000_0101)  # ready + cartridge, no error

    def queue_status(self, st: DriveStatus):
        self._status_queue.append(st)


def _transport(geom=None, fmt=TapeFormat.QIC80):
    geom = geom or Geometry(tracks=4, segments_per_track=10)
    link = FakeLink()
    drive = FakeDrive(link, geom, fmt)
    tt = TapeTransport(drive)
    # pre-seed identify-derived state
    tt._cfg = drive.config()
    tt._tape = drive.tape_status()
    tt._geom = geom
    # seed flux per track
    for t in range(geom.tracks):
        link.set_flux(t, _synth_flux(t, 0 if t % 2 == 0 else 1))
    return tt, drive, link


# ---------------------------------------------------------------------------
# identify
# ---------------------------------------------------------------------------


def test_identify_builds_geometry():
    geom = Geometry(tracks=4, segments_per_track=10)
    link = FakeLink()
    drive = FakeDrive(link, geom)
    tt = TapeTransport(drive)
    cfg, tape, g = tt.identify()
    assert cfg.rate_kbps == 500
    assert tape.format is TapeFormat.QIC80
    assert g.tracks == 28  # QIC-80 0.250in default
    assert g.segments_per_track == 10  # from format_segments()
    assert ("wake",) in drive.commands_run


# ---------------------------------------------------------------------------
# capture_pass
# ---------------------------------------------------------------------------


def test_capture_pass_builds_valid_rawfluxcapture():
    tt, drive, link = _transport()
    cap = tt.capture_pass(track=0, pass_id=0)
    assert isinstance(cap, RawFluxCapture)
    assert cap.header.track == 0
    assert cap.header.direction is Direction.FORWARD
    assert cap.header.rate_kbps == 500
    # markers parse and the END verifies
    assert len(cap.segments()) == 3
    assert not cap.is_truncated
    assert cap.verify() is True
    # capture was opened on the LOGICAL_FORWARD code (10), via link.capture
    assert link.captures == [10]


def test_capture_pass_seeks_first_then_captures():
    tt, drive, link = _transport()
    tt.capture_pass(track=2, pass_id=1)
    seek = [c for c in drive.commands_run if c[0] == "seek head to track"]
    assert seek == [("seek head to track", 2)]


def test_capture_pass_records_serpentine_direction():
    tt, drive, link = _transport()
    even = tt.capture_pass(track=0, pass_id=0)
    odd = tt.capture_pass(track=1, pass_id=0)
    assert even.header.direction is Direction.FORWARD
    assert odd.header.direction is Direction.REVERSE
    # flux is never reversed by the transport — it just records direction.
    assert odd.header.physical_reverse is False


# ---------------------------------------------------------------------------
# walk_all ordering
# ---------------------------------------------------------------------------


def test_walk_all_ordering_single_pass():
    tt, drive, link = _transport(Geometry(tracks=4, segments_per_track=10))
    caps = list(tt.walk_all(passes=1))
    assert [c.header.track for c in caps] == [0, 1, 2, 3]
    assert [c.header.pass_id for c in caps] == [0, 0, 0, 0]


def test_walk_all_ordering_multi_pass():
    tt, drive, link = _transport(Geometry(tracks=2, segments_per_track=10))
    caps = list(tt.walk_all(passes=2))
    assert [(c.header.track, c.header.pass_id) for c in caps] == [
        (0, 0),
        (0, 1),
        (1, 0),
        (1, 1),
    ]


# ---------------------------------------------------------------------------
# broken-tape guard
# ---------------------------------------------------------------------------


def test_broken_tape_guard_raises():
    tt, drive, link = _transport(Geometry(tracks=2, segments_per_track=10))
    # First guard status: a fatal broken-tape error.
    drive.queue_status(DriveStatus.decode(0b0000_0011))  # ready + error
    drive.last_error = ErrorCode.decode(10, fatal=True)  # broken tape (fatal)
    with pytest.raises(TapeError):
        list(tt.walk_all(passes=1))
    # A stop_tape command should have been issued before raising.
    assert any(c[0] == "stop tape" for c in drive.commands_run)


def test_benign_error_does_not_abort():
    tt, drive, link = _transport(Geometry(tracks=1, segments_per_track=10))
    drive.queue_status(DriveStatus.decode(0b0000_0011))  # ready + error
    drive.last_error = ErrorCode.decode(1, fatal=False)  # reset-occurred (benign)
    caps = list(tt.walk_all(passes=1))
    assert len(caps) == 1  # not aborted

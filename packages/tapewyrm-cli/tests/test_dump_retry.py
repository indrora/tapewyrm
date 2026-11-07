"""tw dump --retries and --no-clobber: re-reading a failed track, resuming a dump.

The policy (``read_track``) runs over scripted passes that write a marker into
their file, so a test can see which capture survived. The wiring
(``dump_tracks``) runs over a fake drive and link that play back synthetic
flux streams, so a test can see the drive wound again between passes and the
sidecar line that comes out.
"""

from __future__ import annotations

import json
import struct
from collections import deque
from dataclasses import asdict, replace
from pathlib import Path

import pytest
from tapewyrm_archive.qic117 import DriveStatus
from tapewyrm_archive.twrf import read_header
from tapewyrm_archive.types import CaptureHeader, Direction, TapeFormat

from tapewyrm.link.device import LinkClosed, LinkError
from tapewyrm.link.protocol import EndReason, Marker
from tapewyrm.qic117 import commands
from tapewyrm.qic117.drive import DriveError
from tapewyrm.tape import dump as dump_mod
from tapewyrm.tape.dump import (
    RETRY_SUFFIX,
    DumpStopped,
    RejectedPass,
    TrackResult,
    dump_tracks,
    pass_failure,
    pass_rank,
    previous_results,
    read_track,
)

GOOD = TrackResult(
    track=0, path="", bytes=1, seconds=1.0, end_reason="EOT", verified=True,
    tape_seconds=148.0, index_pulses=207, missing_est=0, status_after=0x65, error_after=None,
)  # fmt: skip


# ---------------------------------------------------------------------------
# The measures: what counts as failed, and which of two passes is better
# ---------------------------------------------------------------------------


def test_a_clean_pass_is_not_failed():
    assert pass_failure(GOOD, 0, best_index=0) is None
    assert pass_failure(GOOD, 0, best_index=207) is None


def test_the_stop_rules_and_any_missed_segment_count_as_failed():
    """The retry rule is check_pass plus "missing_est > 0" (the stop rule allows 5 %)."""
    assert "not EOT" in pass_failure(replace(GOOD, end_reason="WATCHDOG"), 0, 0)
    assert "verify" in pass_failure(replace(GOOD, verified=False), 0, 0)
    assert "overflow" in pass_failure(GOOD, 1, 0)
    assert "only 150 segments" in pass_failure(replace(GOOD, index_pulses=150), 0, 207)
    assert "1 segments missed" in pass_failure(replace(GOOD, missing_est=1), 0, 0)


def test_pass_rank_orders_the_measures_most_telling_first():
    """EOT, then verified, then flux status, then segments found, then fewest missed."""
    assert (
        pass_rank(replace(GOOD, end_reason="WATCHDOG"), 0)
        < pass_rank(replace(GOOD, verified=False), 0)
        < pass_rank(GOOD, 1)
        < pass_rank(replace(GOOD, index_pulses=200), 0)
        < pass_rank(replace(GOOD, missing_est=1), 0)
        < pass_rank(GOOD, 0)
    )


def test_segments_found_outrank_segments_missed():
    """207 found with 3 skipped inside holds more of the track than 200 with no gap."""
    assert pass_rank(replace(GOOD, index_pulses=207, missing_est=3), 0) > pass_rank(
        replace(GOOD, index_pulses=200), 0
    )


# ---------------------------------------------------------------------------
# read_track over scripted passes
# ---------------------------------------------------------------------------


class Script:
    """A ``capture_pass`` that plays back outcomes: a result to return, or an error to raise.

    Every pass first writes ``attempt-K`` into its file, as a real pass leaves
    at least a preamble behind even when it dies, so the surviving file names
    the pass it came from.
    """

    def __init__(self, *outcomes: TrackResult | Exception) -> None:
        self.outcomes = deque(outcomes)
        self.calls: list[tuple[Path, int]] = []

    def __call__(self, dest: Path, attempt: int) -> tuple[TrackResult, int]:
        self.calls.append((dest, attempt))
        dest.write_bytes(f"attempt-{attempt}".encode())
        outcome = self.outcomes.popleft()
        if isinstance(outcome, Exception):
            raise outcome
        return replace(outcome, path=str(dest)), 0


@pytest.fixture
def path(tmp_path: Path) -> Path:
    return tmp_path / "track-00.twrf"


def _leftovers(path: Path) -> list[str]:
    return sorted(p.name for p in path.parent.iterdir())


def test_a_failed_pass_is_re_read_and_the_clean_pass_kept(path):
    script = Script(replace(GOOD, end_reason="WATCHDOG"), GOOD)
    res, _ = read_track(script, 0, path, retries=1)
    assert (res.attempts, res.kept) == (2, 2)
    assert res.rejected == [RejectedPass(1, "pass ended with WATCHDOG, not EOT")]
    assert res.path == str(path) and path.read_bytes() == b"attempt-2"
    assert _leftovers(path) == ["track-00.twrf"]  # the scratch file was moved into place
    # The re-read was captured beside the kept file, never over it.
    assert [(dest.name, attempt) for dest, attempt in script.calls] == [
        ("track-00.twrf", 1),
        ("track-00.twrf" + RETRY_SUFFIX, 2),
    ]


def test_a_clean_first_pass_is_not_re_read(path):
    script = Script(GOOD, GOOD)
    res, _ = read_track(script, 0, path, retries=3)
    assert (res.attempts, res.kept, res.rejected) == (1, 1, [])
    assert len(script.calls) == 1


def test_gives_up_after_n_plus_one_failed_passes(path):
    """retries=2 makes three passes, keeps the best, and hands it back still failed."""
    script = Script(
        replace(GOOD, missing_est=3),
        replace(GOOD, missing_est=2),
        replace(GOOD, missing_est=1),
        GOOD,
    )
    res, ack = read_track(script, 0, path, retries=2)
    assert len(script.calls) == 3  # the fourth, clean pass is never made
    assert (res.attempts, res.kept, res.missing_est) == (3, 3, 1)
    assert [r.attempt for r in res.rejected] == [1, 2]
    assert pass_failure(res, ack, 0) is not None  # dump_tracks applies the stop rules to it
    assert path.read_bytes() == b"attempt-3" and _leftovers(path) == ["track-00.twrf"]


def test_no_retries_is_a_single_pass_whatever_it_looks_like(path):
    script = Script(replace(GOOD, verified=False), GOOD)
    res, _ = read_track(script, 0, path, retries=0)
    assert (res.attempts, res.verified, len(script.calls)) == (1, False, 1)
    assert path.read_bytes() == b"attempt-1"


def test_negative_retries_are_refused(path):
    with pytest.raises(ValueError, match="0 or more"):
        read_track(Script(GOOD), 0, path, retries=-1)


def test_an_earlier_pass_that_found_more_of_the_track_is_kept(path):
    """Keep-best: the last pass is clean but holds less, so the earlier one stays."""
    script = Script(replace(GOOD, index_pulses=207, missing_est=3), replace(GOOD, index_pulses=200))
    res, _ = read_track(script, 0, path, retries=1)
    assert (res.attempts, res.kept, res.index_pulses, res.missing_est) == (2, 1, 207, 3)
    assert res.rejected == [
        RejectedPass(
            2, "clean, but ranked below attempt 1 (200 segments found, 0 missed, vs 207 and 3)"
        )
    ]
    assert path.read_bytes() == b"attempt-1" and _leftovers(path) == ["track-00.twrf"]


def test_a_kept_pass_that_is_still_failed_keeps_the_loop_going(path):
    """A clean pass that lost to the kept failed one does not end the re-reads."""
    script = Script(
        replace(GOOD, index_pulses=207, missing_est=3), replace(GOOD, index_pulses=200), GOOD
    )
    res, _ = read_track(script, 0, path, retries=2)
    assert (res.attempts, res.kept) == (3, 3)
    assert [r.attempt for r in res.rejected] == [1, 2]
    assert path.read_bytes() == b"attempt-3"


def test_a_tie_goes_to_the_last_pass(path):
    script = Script(replace(GOOD, missing_est=1), replace(GOOD, missing_est=1))
    res, _ = read_track(script, 0, path, retries=1)
    assert (res.attempts, res.kept) == (2, 2)
    assert [r.attempt for r in res.rejected] == [1]
    assert path.read_bytes() == b"attempt-2"


def test_a_pass_that_raised_is_rejected_with_its_error_and_re_read(path):
    script = Script(LinkError("usb hiccup"), GOOD)
    res, _ = read_track(script, 0, path, retries=1)
    assert (res.attempts, res.kept) == (2, 2)
    assert res.rejected == [RejectedPass(1, "capture raised LinkError: usb hiccup")]
    assert path.read_bytes() == b"attempt-2" and _leftovers(path) == ["track-00.twrf"]


def test_when_every_pass_raised_the_last_error_propagates_and_its_partial_stays(path):
    """Nothing was measured, so the last partial file is all the track has to show."""
    script = Script(LinkError("one"), DriveError("two"))
    with pytest.raises(DriveError, match="two"):
        read_track(script, 0, path, retries=1)
    assert len(script.calls) == 2
    assert path.read_bytes() == b"attempt-2" and _leftovers(path) == ["track-00.twrf"]


def test_a_raised_pass_never_displaces_a_measured_one(path):
    script = Script(replace(GOOD, missing_est=1), LinkError("usb hiccup"))
    res, _ = read_track(script, 0, path, retries=1)
    assert (res.attempts, res.kept) == (2, 1)
    assert res.rejected == [RejectedPass(2, "capture raised LinkError: usb hiccup")]
    assert path.read_bytes() == b"attempt-1" and _leftovers(path) == ["track-00.twrf"]


def test_a_board_that_is_gone_is_not_retried(path):
    script = Script(LinkClosed("gone"), GOOD)
    with pytest.raises(LinkClosed):
        read_track(script, 0, path, retries=3)
    assert len(script.calls) == 1


# ---------------------------------------------------------------------------
# dump_tracks over a fake drive and link
# ---------------------------------------------------------------------------

CLK = 72_000_000
PERIOD = 51_000_000  # ~710 ms of ticks between INDEX pulses: one segment on the 350
# Report Drive Status bits (Rev J Table 2c): 0 ready, 2 cartridge, 5 referenced,
# 6 at BOT, 7 at EOT.
AT_BOT = DriveStatus.decode(0b0110_0101)
AT_EOT = DriveStatus.decode(0b1010_0101)


def _n28(x: int) -> bytes:
    return bytes(
        [
            (1 | (x << 1)) & 0xFF,
            (1 | (x >> 6)) & 0xFF,
            (1 | (x >> 13)) & 0xFF,
            (1 | (x >> 20)) & 0xFF,
        ]
    )


def _marker(kind: Marker, payload: bytes) -> bytes:
    return bytes([0xFF, kind.value, len(payload)]) + payload


def _stream(gaps: list[int], reason: EndReason = EndReason.EOT) -> bytes:
    """A flux stream with one INDEX pulse after each gap (ticks), sealed by END."""
    flux = b""
    body = b""
    for gap in gaps:
        interval = b"\xff\x02" + _n28(gap - 249) + bytes([249])  # one long interval
        flux += interval
        body += interval + b"\xff\x01" + _n28(0)  # INDEX right at that transition
    start = _marker(Marker.SESSION_START, struct.pack("<HIHBH", 500, CLK, 0, 0, 1))
    end = struct.pack("<BIII", reason, len(gaps), len(flux), sum(flux) & 0xFFFFFFFF)
    return start + body + _marker(Marker.END, end) + b"\x00"


class FakeCapture:
    def __init__(self, blob: bytes) -> None:
        self.blob = blob

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def chunks(self):
        yield self.blob[:16]
        yield self.blob[16:]


class FakeLink:
    info = None

    def __init__(self, streams: list[bytes]) -> None:
        self.streams = deque(streams)
        self.captures: list[dict] = []

    def capture(self, motion_cmd, stop, **kw):
        self.captures.append(kw)
        return FakeCapture(self.streams.popleft())

    def flux_status(self) -> int:
        return 0


class FakeDrive:
    def __init__(self, link: FakeLink) -> None:
        self.link = link
        self.sent: list[str] = []
        self.last_error = None

    def status(self) -> DriveStatus:
        return AT_BOT

    def command(self, cmd, arg=None) -> DriveStatus:
        self.sent.append(cmd.name)
        return AT_EOT if cmd is commands.PHYSICAL_FORWARD else AT_BOT

    def wait_ready(self, timeout_s: float) -> DriveStatus:
        return AT_BOT


@pytest.fixture
def identity(monkeypatch):
    template = CaptureHeader(
        rate_kbps=500, sample_clock_hz=CLK, track=0, direction=Direction.FORWARD,
        pass_id=1, utc="", tape_format=TapeFormat.QIC80,
    )  # fmt: skip
    monkeypatch.setattr(dump_mod, "drive_identity", lambda drive: template)


def _sidecar(out_dir: Path) -> list[dict]:
    return [json.loads(line) for line in (out_dir / "dump.jsonl").read_text().splitlines()]


def test_dump_tracks_re_reads_a_track_through_the_drive_layer_and_logs_every_pass(
    tmp_path, identity
):
    link = FakeLink([_stream([PERIOD] * 8, EndReason.WATCHDOG), _stream([PERIOD] * 8)])
    drive = FakeDrive(link)
    results = dump_tracks(drive, tmp_path, [0], retries=1)
    assert [(r.attempts, r.kept, r.end_reason, r.index_pulses) for r in results] == [
        (2, 2, "EOT", 8)
    ]
    (line,) = _sidecar(tmp_path)
    assert (line["attempts"], line["kept"]) == (2, 2)
    assert line["rejected"] == [{"attempt": 1, "reason": "pass ended with WATCHDOG, not EOT"}]
    assert line["path"] == str(tmp_path / "track-00.twrf")
    # Each pass wound to the track's start and seeked the head again, by drive commands.
    assert drive.sent == [commands.PHYSICAL_REVERSE.name, commands.SEEK_HEAD_TO_TRACK.name] * 2
    # The attempt number is the capture's pass_id, in the request and in the kept header.
    assert [c["pass_id"] for c in link.captures] == [1, 2]
    hdr, _ = read_header(tmp_path / "track-00.twrf")
    assert hdr.pass_id == 2
    assert sorted(p.name for p in tmp_path.iterdir()) == ["dump.jsonl", "track-00.twrf"]


def test_dump_tracks_stops_on_the_kept_pass_once_the_retries_are_spent(tmp_path, identity):
    link = FakeLink([_stream([PERIOD] * 8, EndReason.WATCHDOG)] * 2)
    with pytest.raises(DumpStopped, match="track 0: pass ended with WATCHDOG, not EOT"):
        dump_tracks(FakeDrive(link), tmp_path, [0], retries=1)
    (line,) = _sidecar(tmp_path)  # the line for the kept pass is still written
    assert (line["attempts"], line["kept"], len(link.captures)) == (2, 2, 2)


def test_dump_tracks_carries_on_with_a_few_missed_segments_once_the_retries_are_spent(
    tmp_path, identity
):
    """One missed segment in 24 is under the 5 % stop rule: with no retry left, carry on."""
    holed = [PERIOD] * 12 + [2 * PERIOD] + [PERIOD] * 11
    link = FakeLink([_stream(holed), _stream(holed)])
    results = dump_tracks(FakeDrive(link), tmp_path, [0], retries=1)
    assert [(r.attempts, r.missing_est, r.index_pulses) for r in results] == [(2, 1, 24)]
    assert len(link.captures) == 2


# ---------------------------------------------------------------------------
# --no-clobber: an earlier run's pass is skipped if clean, else competes
# ---------------------------------------------------------------------------


def test_an_earlier_runs_pass_is_replaced_only_by_a_better_one(path):
    """Seeded with the earlier pass, numbering continues and keep-best applies to it."""
    path.write_bytes(b"earlier")
    earlier = replace(
        GOOD,
        missing_est=2,
        attempts=2,
        kept=2,
        rejected=[RejectedPass(1, "pass ended with WATCHDOG, not EOT")],
    )
    script = Script(replace(GOOD, index_pulses=190))  # clean, but holds less of the track
    res, _ = read_track(script, 0, path, retries=0, existing=earlier)
    assert (res.attempts, res.kept, res.missing_est) == (3, 2, 2)
    assert [r.attempt for r in res.rejected] == [1, 3]
    assert path.read_bytes() == b"earlier" and _leftovers(path) == ["track-00.twrf"]
    assert script.calls == [(path.with_name("track-00.twrf" + RETRY_SUFFIX), 3)]


def test_an_earlier_runs_pass_gives_way_to_a_clean_one(path):
    path.write_bytes(b"earlier")
    earlier = replace(GOOD, missing_est=2, attempts=2, kept=2, rejected=[RejectedPass(1, "x")])
    res, _ = read_track(Script(GOOD), 0, path, retries=0, existing=earlier)
    assert (res.attempts, res.kept) == (3, 3)
    assert res.rejected == [
        RejectedPass(1, "x"),
        RejectedPass(2, "~2 segments missed inside the pass (207 found)"),
    ]
    assert path.read_bytes() == b"attempt-3" and _leftovers(path) == ["track-00.twrf"]


def _write_sidecar(out_dir: Path, lines: list[dict]) -> None:
    (out_dir / "dump.jsonl").write_text("\n".join(json.dumps(line) for line in lines) + "\n")


def test_previous_results_take_the_latest_line_per_track_with_a_whole_file(tmp_path):
    before_retries = {
        k: v for k, v in asdict(GOOD).items() if k not in ("attempts", "kept", "rejected")
    }
    _write_sidecar(
        tmp_path,
        [
            {**before_retries, "track": 0, "path": "elsewhere/track-00.twrf", "bytes": 4},
            asdict(replace(GOOD, track=1, bytes=4, end_reason="WATCHDOG")),
            asdict(
                replace(GOOD, track=1, bytes=4, attempts=2, kept=2, rejected=[RejectedPass(1, "r")])
            ),  # the re-read's line supersedes the one before
            asdict(replace(GOOD, track=2, bytes=4)),  # its file is missing
            asdict(replace(GOOD, track=3, bytes=400)),  # its file is short
        ],
    )
    for track in (0, 1, 3):
        (tmp_path / f"track-{track:02d}.twrf").write_bytes(b"flux")
    found = previous_results(tmp_path)
    assert sorted(found) == [0, 1]
    assert (found[0].attempts, found[0].kept, found[0].rejected) == (1, 1, [])
    assert (found[1].attempts, found[1].end_reason, found[1].rejected) == (
        2,
        "EOT",
        [RejectedPass(1, "r")],
    )


def test_previous_results_ignore_an_unreadable_line(tmp_path):
    (tmp_path / "dump.jsonl").write_text(
        '{"track": 0}\nnot json\n' + json.dumps(asdict(replace(GOOD, track=1, bytes=4))) + "\n"
    )
    (tmp_path / "track-01.twrf").write_bytes(b"flux")
    assert sorted(previous_results(tmp_path)) == [1]


def test_previous_results_without_a_sidecar(tmp_path):
    assert previous_results(tmp_path) == {}


def test_dump_tracks_no_clobber_skips_a_clean_track_and_re_reads_a_dirty_one(tmp_path, identity):
    """An earlier run: track 0 clean, track 1 ended with WATCHDOG, both files whole."""
    for track in (0, 1):
        (tmp_path / f"track-{track:02d}.twrf").write_bytes(b"x" * 64)  # only its size is checked
    _write_sidecar(
        tmp_path,
        [
            asdict(replace(GOOD, track=0, index_pulses=8, bytes=10)),
            asdict(replace(GOOD, track=1, index_pulses=8, bytes=10, end_reason="WATCHDOG")),
        ],
    )
    link = FakeLink([_stream([PERIOD] * 8)])  # one capture: track 1's re-read
    drive = FakeDrive(link)
    results = dump_tracks(drive, tmp_path, [0, 1], no_clobber=True)
    assert [(r.track, r.attempts, r.kept, r.end_reason) for r in results] == [
        (0, 1, 1, "EOT"),
        (1, 2, 2, "EOT"),
    ]
    # Track 0 was never touched; track 1 (odd) was wound to EOT and read as its attempt 2.
    assert drive.sent == [commands.PHYSICAL_FORWARD.name, commands.SEEK_HEAD_TO_TRACK.name]
    assert [(c["tpt"], c["pass_id"]) for c in link.captures] == [(1, 2)]
    hdr, _ = read_header(tmp_path / "track-01.twrf")
    assert hdr.pass_id == 2
    lines = _sidecar(tmp_path)
    assert len(lines) == 3 and lines[2]["track"] == 1  # one new line, for the re-read only
    assert (lines[2]["attempts"], lines[2]["kept"]) == (2, 2)
    assert lines[2]["rejected"] == [{"attempt": 1, "reason": "pass ended with WATCHDOG, not EOT"}]


def test_dump_tracks_no_clobber_reads_a_track_whose_file_is_gone(tmp_path, identity):
    _write_sidecar(tmp_path, [asdict(replace(GOOD, track=0, index_pulses=8, bytes=10))])
    link = FakeLink([_stream([PERIOD] * 8)])
    results = dump_tracks(FakeDrive(link), tmp_path, [0], no_clobber=True)
    assert [(r.track, r.attempts, r.kept) for r in results] == [(0, 1, 1)]
    assert [c["pass_id"] for c in link.captures] == [1]

"""tw dump's decode-free pass checks: INDEX counting, gap estimate, stop rules."""

from dataclasses import replace

from tapewyrm.codec import gwstream
from tapewyrm.tape.dump import TrackResult, check_pass, missing_segments, segment_pulses

PERIOD = 51_000_000  # ~710 ms of 72 MHz ticks, one segment on the 350


def _n28(v: int) -> bytes:
    """GW's 28-bit field: 7 bits per byte, low bit set (the inverse of _n28)."""
    return bytes(((v >> s) & 0x7F) << 1 | 1 for s in (0, 7, 14, 21))


def test_index_pulses_in_a_stream_are_counted():
    # INDEX opcode (FF 01 + N28 ticks since the last flux), between intervals.
    blob = b"\x48" + b"\xff\x01" + _n28(10) + b"\x48" + b"\xff\x01" + _n28(20) + b"\x48"
    assert len(gwstream.parse(blob).index_ticks) == 2


def test_missing_segments_from_long_gaps():
    ticks = [k * PERIOD for k in range(10)]
    assert missing_segments(ticks) == 0
    # Drop three pulses in a row: one 4-period gap hides 3 segments.
    holed = ticks[:4] + ticks[7:]
    assert missing_segments(holed) == 3
    # +-4% jitter (the bench spread) is not a gap.
    jitter = [k * PERIOD + (PERIOD // 25 if k % 2 else 0) for k in range(10)]
    assert missing_segments(jitter) == 0


def test_a_cue_pulse_at_the_start_is_not_a_segment():
    clk = 72_000_000
    leader = int(1.9 * clk)  # cue at t=0, then ~1.9 s of leader, then segments
    ticks = [0] + [leader + k * PERIOD for k in range(10)]
    segs = segment_pulses(ticks, clk)
    assert len(segs) == 10 and missing_segments(segs) == 0
    assert missing_segments(ticks) == 2  # what the cue used to cost


GOOD = TrackResult(
    track=1, path="t", bytes=1, seconds=1.0, end_reason="EOT", verified=True,
    tape_seconds=148.0, index_pulses=207, missing_est=0, status_after=0x25, error_after=None,
)  # fmt: skip


def test_a_clean_pass_carries_on():
    assert check_pass(GOOD, best_index=207) is None
    assert check_pass(GOOD, best_index=0) is None  # first pass: no reference yet


def test_stop_rules():
    assert "overflow" in check_pass(GOOD, 207, flux_ack=1)
    assert "not EOT" in check_pass(replace(GOOD, end_reason="WATCHDOG"), 207)
    assert "verify" in check_pass(replace(GOOD, verified=False), 207)
    assert "segments" in check_pass(replace(GOOD, index_pulses=150), 207)
    assert "missed" in check_pass(replace(GOOD, missing_est=20), 207)


def test_crc_rule_only_with_check():
    assert GOOD.good_fraction is None  # not decoded: rule doesn't apply
    decoded = replace(GOOD, sectors=6624, good=1000, segments=207)
    assert "CRC-clean" in check_pass(decoded, 207)
    assert check_pass(replace(decoded, good=6623), 207) is None

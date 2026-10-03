"""RawFluxCapture container: round-trip, marker parse, END verify (TWS-1 §5-§7).

Every stream here is built the way the firmware writes it (GW's
``rdata_encode_flux()`` plus Tapewyrm markers on the 0xFF opcode escape), and
deliberately includes the bytes that broke the old 0xFF-scanning helpers: a
two-byte interval whose second byte is 0xFF, INDEX/SPACE opcodes whose N28
argument bytes are 0xFF or look like marker codes, and marker payloads that do.
"""

import struct

import pytest

from tapewyrm_archive.twrf import (
    RawFluxCapture,
    WireMarker,
    encode_index,
    encode_interval,
    encode_space,
    flux_checksum,
    flux_data_only,
    frame_marker,
    iter_markers,
    parse_body,
)
from tapewyrm_archive.types import CaptureHeader, Direction, MarkerKind, TapeFormat


def _header() -> CaptureHeader:
    return CaptureHeader(
        rate_kbps=500,
        sample_clock_hz=72_000_000,
        track=2,
        direction=Direction.FORWARD,
        pass_id=0,
        utc="2026-06-18T00:00:00Z",
        tape_format=TapeFormat.QIC80,
        segments_per_track=207,
        tracks=28,
        device_serial="abc123",
    )


def _flux(*intervals: int) -> bytes:
    return b"".join(encode_interval(t) for t in intervals)


def _session_start() -> bytes:
    return frame_marker(
        MarkerKind.SESSION_START, struct.pack("<HIHBH", 500, 72_000_000, 2, 0, 0xFFFF)
    )


def _segment_marker(ticks: int, index: int) -> bytes:
    return frame_marker(MarkerKind.SEGMENT, struct.pack("<II", ticks, index))


def _end_marker(reason: int, flux_count: int, byte_count: int, checksum: int) -> bytes:
    return frame_marker(
        MarkerKind.END, struct.pack("<BIII", reason, flux_count, byte_count, checksum)
    )


def _end_for(intervals: list[int]) -> bytes:
    data = _flux(*intervals)
    return _end_marker(3, len(intervals), len(data), flux_checksum(data))


# ---------------------------------------------------------------------------
# Encoders: the byte forms the tests below rely on
# ---------------------------------------------------------------------------


def test_encode_interval_matches_rdata_encode_flux_byte_forms():
    """1..249 one byte; 250..1524 two bytes (second may be 0xFF); longer is FF 02 N28 F9."""
    assert encode_interval(249) == b"\xf9"
    assert encode_interval(504) == b"\xfa\xff"  # 250 + 0*255 + 255 - 1
    assert encode_interval(759) == b"\xfb\xff"
    assert encode_interval(1524) == b"\xfe\xff"
    assert encode_interval(1525)[:2] == b"\xff\x02" and encode_interval(1525)[-1] == 249
    assert encode_index(127)[2] == 0xFF  # N28 argument bytes can be 0xFF too
    with pytest.raises(ValueError):
        encode_interval(0)


# ---------------------------------------------------------------------------
# Markers and flux data on a hostile synthetic stream
# ---------------------------------------------------------------------------

# Intervals chosen so the stream holds 0xFF in every non-escape position the
# old scanner could trip on: two-byte intervals ending in 0xFF (504, 759,
# 1524), one directly before an INDEX opcode and one before a marker.
INTERVALS = [144, 504, 216, 759, 1524, 3, 72_000, 1524, 249]


def _hostile_stream() -> tuple[bytes, list[int]]:
    """A full pass: every marker kind, INDEX and dead-time SPACE, then NUL.

    The intervals the stream decodes to are returned too: the dead time before
    the 7th interval adds to it, so it is not INTERVALS[6].
    """
    iv = INTERVALS
    body = _session_start()
    body += frame_marker(MarkerKind.EVENT, b"\x01")
    body += _flux(iv[0], iv[1])  # ... fa ff
    body += encode_index(127)  # ff 01 ff 01 01 01 -- right after the 0xFF data byte
    body += _segment_marker(0xFFF1F0FF, 1)  # payload looks like escapes and codes
    body += _flux(iv[2], iv[3], iv[4])  # ... fe ff
    body += frame_marker(MarkerKind.HEARTBEAT)  # ff f4 00 right after 0xFF data
    body += _flux(iv[5])
    body += encode_space(0x7F | (0x79 << 7))  # dead time, N28 bytes ff f3 ..
    body += _flux(iv[6], iv[7], iv[8])
    data = _flux(*iv)
    body += _end_marker(3, len(iv), len(data), flux_checksum(data))
    body += b"\x00"
    decoded = list(iv)
    decoded[6] += 0x7F | (0x79 << 7)
    return body, decoded


def test_every_marker_kind_is_found_exactly_once_with_its_fields():
    body, _ = _hostile_stream()
    markers = list(iter_markers(body))
    assert [m.kind for m in markers] == [
        MarkerKind.SESSION_START,
        MarkerKind.EVENT,
        MarkerKind.SEGMENT,
        MarkerKind.HEARTBEAT,
        MarkerKind.END,
    ]
    assert markers[0].fields["pass_id"] == 0xFFFF
    assert markers[1].fields == {"code": 1}
    assert markers[2].fields == {"ticks": 0xFFF1F0FF, "index": 1}
    assert markers[3].fields == {"raw_len": 0}
    assert markers[4].fields["flux_count"] == len(INTERVALS)
    # Each offset points at the marker's own escape + code.
    for m in markers:
        assert body[m.offset] == 0xFF
        assert body[m.offset + 1] in {int(w) for w in WireMarker}


def test_flux_data_and_checksum_are_exactly_the_interval_bytes():
    """Two-byte 0xFF tails are data; INDEX/SPACE args and markers are not."""
    body, _ = _hostile_stream()
    data = flux_data_only(body)
    assert data == _flux(*INTERVALS)
    assert flux_checksum(data) == sum(_flux(*INTERVALS)) & 0xFFFFFFFF


def test_hostile_stream_verifies_and_decodes_intervals_and_index_time():
    body, decoded = _hostile_stream()
    cap = RawFluxCapture(header=_header(), flux=body)
    assert not cap.is_truncated
    assert cap.verify() is True
    assert [m.fields["index"] for m in cap.segments()] == [1]
    ps = parse_body(body)
    assert ps.intervals == decoded and ps.terminated
    assert ps.index_ticks == [144 + 504 + 127]


def test_index_after_dead_time_counts_from_the_sample_cursor():
    """GW's INDEX N28 is relative to the sample cursor, which dead time advances."""
    body = _flux(100) + encode_space(14_400) + encode_index(50) + _flux(20) + b"\x00"
    ps = parse_body(body)
    assert ps.index_ticks == [100 + 14_400 + 50]
    assert ps.intervals == [100, 14_420]
    assert flux_data_only(body) == _flux(100, 20)


def test_end_with_wrong_flux_count_does_not_verify():
    iv = [10, 504, 20]
    data = _flux(*iv)
    body = data + _end_marker(3, len(iv) + 1, len(data), flux_checksum(data))
    assert RawFluxCapture(header=_header(), flux=body).verify() is False


def test_bytes_after_the_terminator_are_not_stream():
    body = _flux(1, 2, 3) + _end_for([1, 2, 3]) + b"\x00" + _segment_marker(9, 9) + b"\x05"
    assert [m.kind for m in iter_markers(body)] == [MarkerKind.END]
    assert flux_data_only(body) == _flux(1, 2, 3)
    assert RawFluxCapture(header=_header(), flux=body).verify() is True


def test_cut_two_byte_interval_ends_the_data_before_it():
    body = _flux(5, 6) + b"\xfb"  # USB stopped mid-interval
    assert flux_data_only(body) == _flux(5, 6)
    assert RawFluxCapture(header=_header(), flux=body).is_truncated


def test_unknown_opcode_is_refused_not_read_as_flux():
    with pytest.raises(ValueError, match="unknown stream opcode 0x03"):
        flux_data_only(_flux(5) + b"\xff\x03" + _flux(6))


# ---------------------------------------------------------------------------
# Container behaviour
# ---------------------------------------------------------------------------


def test_truncated_capture_has_no_end():
    flux = _flux(1, 2, 3) + _segment_marker(10, 1)
    cap = RawFluxCapture(header=_header(), flux=flux)
    assert cap.is_truncated
    assert cap.verify() is False


def test_reassigning_flux_drops_the_cached_parse():
    cap = RawFluxCapture(header=_header(), flux=_flux(1, 2, 3) + _end_for([1, 2, 3]))
    assert cap.verify() is True
    cap.flux = _flux(1, 2, 3)
    assert cap.is_truncated


def test_save_load_round_trip(tmp_path):
    iv = list(range(1, 250)) + list(range(250, 1525, 7)) + [2000, 1_000_000]
    flux = _flux(*iv) + _segment_marker(99, 1) + _end_for(iv) + b"\x00"
    cap = RawFluxCapture(header=_header(), flux=flux)
    p = tmp_path / "pass.twrf"
    cap.save(p)
    back = RawFluxCapture.load(p)
    assert back.flux == cap.flux
    assert back.header == cap.header
    assert back.verify() is True


def test_from_stream_concatenates_chunks():
    """A chunk boundary may split a two-byte interval; the parse must not care."""
    whole = _flux(1, 504, 3) + _end_for([1, 504, 3])
    chunks = [whole[:2], whole[2:4], whole[4:]]  # splits fa|ff
    cap = RawFluxCapture.from_stream(_header(), iter(chunks))
    assert cap.verify() is True

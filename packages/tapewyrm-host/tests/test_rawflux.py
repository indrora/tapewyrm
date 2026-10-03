"""RawFluxCapture container: round-trip, marker parse, END verify."""

import struct

from tapewyrm.rawflux import RawFluxCapture, frame_marker
from tapewyrm.rawflux.container import flux_checksum, flux_data_only, stuff_flux
from tapewyrm.types import CaptureHeader, Direction, MarkerKind, TapeFormat


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


def _segment_marker(ticks: int, index: int) -> bytes:
    return frame_marker(MarkerKind.SEGMENT, struct.pack("<II", ticks, index))


def _end_marker(reason: int, flux_count: int, byte_count: int, checksum: int) -> bytes:
    return frame_marker(
        MarkerKind.END, struct.pack("<BIII", reason, flux_count, byte_count, checksum)
    )


def test_marker_framing_and_iteration():
    raw = b"\x01\x02\xff\x03"  # contains a literal ESC (0xFF)
    flux = stuff_flux(raw) + _segment_marker(1234, 1) + _segment_marker(5678, 2)
    cap = RawFluxCapture(header=_header(), flux=flux)
    segs = cap.segments()
    assert len(segs) == 2
    assert segs[0].fields == {"ticks": 1234, "index": 1}
    assert segs[1].fields == {"ticks": 5678, "index": 2}
    # markers must not leak into recovered flux data
    assert flux_data_only(flux) == raw


def test_end_verify_ok():
    raw = b"\x10\x20\x30\xff\x40"
    data = raw
    flux = stuff_flux(raw) + _end_marker(0, 5, len(data), flux_checksum(data))
    cap = RawFluxCapture(header=_header(), flux=flux)
    assert not cap.is_truncated
    assert cap.verify() is True


def test_truncated_capture_has_no_end():
    flux = stuff_flux(b"\x01\x02\x03") + _segment_marker(10, 1)
    cap = RawFluxCapture(header=_header(), flux=flux)
    assert cap.is_truncated
    assert cap.verify() is False


def test_save_load_round_trip(tmp_path):
    raw = b"".join(bytes([i & 0xFF]) for i in range(300))
    flux = (
        stuff_flux(raw) + _segment_marker(99, 1) + _end_marker(0, 0, len(raw), flux_checksum(raw))
    )
    cap = RawFluxCapture(header=_header(), flux=flux)
    p = tmp_path / "pass.twrf"
    cap.save(p)
    back = RawFluxCapture.load(p)
    assert back.flux == cap.flux
    assert back.header == cap.header
    assert back.verify() is True


def test_from_stream_concatenates_chunks():
    chunks = [b"\x01\x02", b"\x03", _end_marker(0, 0, 3, flux_checksum(b"\x01\x02\x03"))]
    cap = RawFluxCapture.from_stream(_header(), iter(chunks))
    assert cap.verify() is True

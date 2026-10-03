"""TWRF v2: self-describing captures (rate + drive identity) and v1 compatibility."""

import io
import json
import struct

from tapewyrm_archive.twrf import RawFluxCapture, read_header, write_preamble
from tapewyrm_archive.types import CaptureHeader, Direction, TapeFormat

from tapewyrm.image import convert

HDR = CaptureHeader(
    rate_kbps=1000,
    sample_clock_hz=72_000_000,
    track=3,
    direction=Direction.REVERSE,
    pass_id=1,
    utc="2026-10-01T03:00:00+00:00",
    tape_format=TapeFormat.QIC3010,
    device_serial="GW053C112228470000077CA413",
    drive_status=0x65,
    drive_config=0x98,  # the Colorado 1400: 1 Mbps
    drive_rom=0x79,
    drive_vendor_id=0x11C3,  # make 71 (Colorado), model 3
    tape_status=0x12,
    tw_commit="23e343c" + "0" * 33,
    firmware_commit="23e343c" + "0" * 33,
    firmware_dirty=False,
)


def test_v2_header_round_trips_drive_identity(tmp_path):
    path = tmp_path / "track-03.twrf"
    RawFluxCapture(header=HDR, flux=b"\x10\x20\x00").save(path)
    loaded = RawFluxCapture.load(path)
    assert loaded.header == HDR
    assert loaded.flux == b"\x10\x20\x00"
    hdr, flux_at = read_header(path)
    assert hdr.rate_kbps == 1000 and hdr.drive_vendor_id == 0x11C3
    assert path.read_bytes()[flux_at:] == b"\x10\x20\x00"


def test_streaming_write_matches_save(tmp_path):
    saved = tmp_path / "a.twrf"
    RawFluxCapture(header=HDR, flux=b"abcdef").save(saved)
    buf = io.BytesIO()
    write_preamble(buf, HDR)
    for chunk in (b"abc", b"def"):  # as the device streams them
        buf.write(chunk)
    assert buf.getvalue() == saved.read_bytes()


def test_v1_files_still_load_with_identity_unknown(tmp_path):
    v1 = {
        "rate_kbps": 500, "sample_clock_hz": 72_000_000, "track": 0, "direction": "forward",
        "pass_id": 1, "utc": "1998-12-23T02:19:27+00:00", "tape_format": 2,
        "segments_per_track": 207, "tracks": 28, "sectors_per_segment": 32,
        "device_serial": "", "physical_reverse": False,
    }  # fmt: skip
    hdr_json = json.dumps(v1).encode()
    path = tmp_path / "old.twrf"
    path.write_bytes(b"TWRF" + struct.pack("<HI", 1, len(hdr_json)) + hdr_json + b"\x05")
    cap = RawFluxCapture.load(path)
    assert cap.header.rate_kbps == 500 and cap.header.direction is Direction.FORWARD
    assert cap.header.drive_config is None and cap.header.tw_commit is None
    assert cap.flux == b"\x05"


def test_convert_reads_each_captures_own_rate(tmp_path):
    twrf = tmp_path / "track-00.twrf"
    RawFluxCapture(header=HDR, flux=b"\x00").save(twrf)
    _, meta = convert.decode_capture(twrf)
    assert meta["twrf"]["rate_kbps"] == 1000 and meta["twrf"]["drive_vendor_id"] == 0x11C3
    legacy = tmp_path / "track-00.raw"
    legacy.write_bytes(b"\x00")
    _, meta = convert.decode_capture(legacy)
    assert meta["twrf"]["rate_kbps"] == convert.LEGACY_RAW_RATE_KBPS

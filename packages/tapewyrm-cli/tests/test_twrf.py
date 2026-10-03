"""TWRF v2: self-describing captures (rate + drive identity), and refusing bad ones."""

import dataclasses
import io
import json
import struct

import pytest
from click.testing import CliRunner
from tapewyrm_archive.errors import MalformedFileError
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


def _twrf_with(path, header: dict, *, version: int = 2) -> object:
    """Hand-write a TWRF file with ``header`` as its JSON, bypassing the writer."""
    hdr_json = json.dumps(header).encode()
    path.write_bytes(b"TWRF" + struct.pack("<HI", version, len(hdr_json)) + hdr_json + b"\x05")
    return path


def _full_header() -> dict:
    """Every member the writer puts in a v2 header (TWS-1 4.2), as JSON values."""
    d = dataclasses.asdict(HDR)
    d["direction"] = HDR.direction.value
    d["tape_format"] = int(HDR.tape_format)
    return d


def test_reader_requires_exactly_the_members_the_writer_writes():
    """The reader's required set and the writer's (every CaptureHeader field) agree."""
    from tapewyrm_archive.twrf import _HEADER_MEMBERS

    assert set(_HEADER_MEMBERS) == set(_full_header())


@pytest.mark.parametrize("member", ["tape_format", "rate_kbps", "direction", "drive_config"])
def test_header_missing_a_required_member_is_malformed(tmp_path, member):
    """TWS-1 8.2: every v2 member is required; an absent one names itself."""
    header = _full_header()
    del header[member]
    path = _twrf_with(tmp_path / "track-00.twrf", header)
    with pytest.raises(MalformedFileError, match=member) as info:
        read_header(path)
    assert "track-00.twrf" in str(info.value)


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        ({"direction": "sideways"}, "direction"),
        ({"tape_format": 9}, "tape_format"),
        ({"rate_kbps": "500"}, "rate_kbps"),
    ],
)
def test_header_with_a_bad_member_is_malformed(tmp_path, change, problem):
    path = _twrf_with(tmp_path / "t.twrf", {**_full_header(), **change})
    with pytest.raises(MalformedFileError, match=problem):
        read_header(path)


def test_header_that_is_not_a_json_object_is_malformed(tmp_path):
    path = tmp_path / "t.twrf"
    path.write_bytes(b"TWRF" + struct.pack("<HI", 2, 5) + b"{nope")
    with pytest.raises(MalformedFileError, match="JSON"):
        read_header(path)
    with pytest.raises(MalformedFileError, match="object"):
        read_header(_twrf_with(tmp_path / "u.twrf", [1, 2]))  # type: ignore[arg-type]


def test_version_1_files_are_refused(tmp_path):
    """Pre-release: only version 2 is read (TWS-1 9); re-dump older captures."""
    path = _twrf_with(tmp_path / "old.twrf", _full_header(), version=1)
    with pytest.raises(MalformedFileError, match="version 1"):
        RawFluxCapture.load(path)


def test_convert_reads_each_captures_own_rate(tmp_path):
    twrf = tmp_path / "track-00.twrf"
    RawFluxCapture(header=HDR, flux=b"\x00").save(twrf)
    _, meta = convert.decode_capture(twrf)
    assert meta["twrf"]["rate_kbps"] == 1000 and meta["twrf"]["drive_vendor_id"] == 0x11C3


def test_convert_identifies_captures_by_magic_not_suffix(tmp_path):
    """A TWRF capture renamed away from .twrf still decodes."""
    renamed = tmp_path / "pass-1.bin"
    RawFluxCapture(header=HDR, flux=b"\x00").save(renamed)
    _, meta = convert.decode_capture(renamed)
    assert meta["twrf"]["rate_kbps"] == 1000


def test_convert_refuses_a_source_without_the_twrf_magic(tmp_path):
    """No headerless streams: a file that is not TWRF is a clean error naming it."""
    bare = tmp_path / "track-00.raw"
    bare.write_bytes(b"\x00" * 64)
    with pytest.raises(ValueError, match="track-00.raw.*TWRF"):
        convert.decode_capture(bare)


@pytest.mark.parametrize("kind", ["not-twrf", "no-tape-format"])
def test_tw_convert_reports_a_bad_capture_and_exits_1(tmp_path, kind):
    """No traceback: one error line naming the file and the problem, exit 1."""
    from tapewyrm import cli as cli_mod

    src = tmp_path / "track-00.twrf"
    if kind == "not-twrf":
        src.write_bytes(b"\x00" * 64)
        problem = "TWRF"
    else:
        header = _full_header()
        del header["tape_format"]
        _twrf_with(src, header)
        problem = "tape_format"
    res = CliRunner().invoke(cli_mod.cli, ["convert", str(src), str(tmp_path / "t.twti")])
    assert res.exit_code == 1, res.output
    assert not isinstance(res.exception, MalformedFileError)  # handled, not raised
    assert "track-00.twrf" in res.output and problem in res.output
    assert "Traceback" not in res.output

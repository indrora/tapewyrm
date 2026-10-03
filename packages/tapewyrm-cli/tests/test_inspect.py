"""`tw inspect`: header summary or verbatim --json for every format, clean errors.

The files are tiny synthetic ones built with the archive's own writers; the
library side (what is read, how it is worded) is pinned down in
tapewyrm-archive's tests/test_inspect.py. These check the command: format by
magic, the rendered sections on stdout, --json byte-for-byte, and a broken
file becoming a one-line error with exit status 1.
"""

from __future__ import annotations

import struct
from pathlib import Path

import pytest
from click.testing import CliRunner
from tapewyrm_archive.twrf import RawFluxCapture
from tapewyrm_archive.twti import SEGMENT_STRIDE, SegmentEntry, SegmentState, TapeImage
from tapewyrm_archive.twvl import Volume
from tapewyrm_archive.types import CaptureHeader, Direction, TapeFormat

import tapewyrm.cli as cli_mod


def _twrf(path: Path) -> Path:
    header = CaptureHeader(
        rate_kbps=1000,
        sample_clock_hz=72_000_000,
        track=0,
        direction=Direction.FORWARD,
        pass_id=1,
        utc="2026-10-03T12:00:00+00:00",
        tape_format=TapeFormat.QIC3020,
        tape_status=0x63,
    )
    RawFluxCapture(header=header, flux=b"\x10\x20\x00").save(path)
    return path


def _image(path: Path) -> Path:
    header = {
        "format": "TWTI",
        "version": 1,
        "segment_count": 2,
        "segment_stride": SEGMENT_STRIDE,
        "geometry": {"tracks": 1, "segments_per_track": 2},
        "qic80_header": {"tape_name": "[bold]NOT MARKUP[/bold]"},
    }
    entries = [SegmentEntry(SegmentState.CLEAN, 0, 2), SegmentEntry(SegmentState.MISSING)]
    TapeImage(header=header, entries=entries).save(path, lambda n: b"hi" if n == 0 else b"")
    return path


def _twvl(path: Path) -> Path:
    header = {"format": "TWVL", "volume_size": 4, "holes": [[0, 2]], "lost_segments": [7]}
    Volume(header=header, data=b"\x00\x00ab").save(path)
    return path


def _stored_header(path: Path) -> bytes:
    """The stored header bytes of an uncompressed file (preamble: 4s, u16, u32)."""
    raw = path.read_bytes()
    (hlen,) = struct.unpack_from("<I", raw, 6)
    return raw[10 : 10 + hlen]


def _run(*args: str):
    return CliRunner().invoke(cli_mod.cli, ["inspect", *args])


@pytest.mark.parametrize(
    ("builder", "name", "expected"),
    [
        (_twrf, "c.twrf", ["TWRF version 2", "0x63 -> QIC3020, variable length 900 Oe"]),
        (_image, "i.twti", ["TWTI version 1", "1 tracks x 2 segments = 2", "50.00%"]),
        (_image, "i.twtz", ["TWTZ version 1", "decompressed"]),
        (_twvl, "v.twvl", ["TWVL version 1", "1 ranges, 2 bytes", "1: 7"]),
    ],
)
def test_summary_for_each_format(tmp_path, builder, name, expected):
    result = _run(str(builder(tmp_path / name)))
    assert result.exit_code == 0, result.output
    for text in expected:
        assert text in result.output


def test_header_values_are_not_rich_markup(tmp_path):
    """A tape name is data from the file: printed literally, never styled."""
    result = _run(str(_image(tmp_path / "i.twti")))
    assert "[bold]NOT MARKUP[/bold]" in result.output


@pytest.mark.parametrize("builder", [_twrf, _image, _twvl])
def test_json_is_the_stored_header_verbatim(tmp_path, builder):
    """--json prints the stored bytes, plus one newline since the writers add none."""
    path = builder(tmp_path / "file.bin")
    stored = _stored_header(path)
    assert not stored.endswith(b"\n")
    result = _run("--json", str(path))
    assert result.exit_code == 0, result.output
    assert result.stdout_bytes == stored + b"\n"


def test_json_of_twtz_matches_its_twti(tmp_path):
    twti = _stored_header(_image(tmp_path / "i.twti"))
    result = _run("--json", str(_image(tmp_path / "i.twtz")))
    assert result.stdout_bytes == twti + b"\n"


def test_truncated_file_is_a_clean_error(tmp_path):
    path = _twrf(tmp_path / "c.twrf")
    with path.open("r+b") as f:
        f.truncate(30)
    result = _run(str(path))
    assert result.exit_code == 1
    assert "truncated" in result.output and "JSON header" in result.output
    assert "Traceback" not in result.output


def test_unknown_file_is_a_clean_error(tmp_path):
    path = tmp_path / "notes.txt"
    path.write_text("not a tape file")
    result = _run(str(path))
    assert result.exit_code == 1
    assert "not a Tapewyrm file" in result.output

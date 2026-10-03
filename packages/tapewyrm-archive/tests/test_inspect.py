"""``tapewyrm_archive.inspect``: header-only inspection of every format.

Each file here is built with the archive's own writers (``RawFluxCapture``,
``TapeImage.save``, ``Volume.save``), so what inspect reads is exactly what
the tools write. Pinned down: identification by magic (never the name), the
stored header bytes coming back verbatim, segment-state counts from the
table, a TWTZ being read only as far as its table, the readers' truncation
and malformation errors, and the human-readable sections.
"""

from __future__ import annotations

import json
import os
import struct
from pathlib import Path

import pytest

from tapewyrm_archive.errors import MalformedFileError, TruncatedFileError
from tapewyrm_archive.inspect import Section, describe, inspect
from tapewyrm_archive.twrf import RawFluxCapture
from tapewyrm_archive.twti import SEGMENT_STRIDE, SegmentEntry, SegmentState, TapeImage
from tapewyrm_archive.twvl import Volume
from tapewyrm_archive.types import CaptureHeader, Direction, TapeFormat

# Report bytes of a QIC-3020 cartridge in a Colorado drive: tape status 0x63
# is format 3 (QIC-3020), type 6 (variable length 900 Oe); drive config 0x18
# is rate code 11 (1000 kbit/s); vendor 0x0047 is Colorado's legacy word.
_DRIVE = {
    "device_serial": "SYNTH0",
    "drive_status": 0x25,
    "drive_config": 0x18,
    "drive_rom": 0x6A,
    "drive_vendor_id": 0x0047,
    "tape_status": 0x63,
    "rate_kbps": 1000,
    "firmware_commit": "1" * 40,
}


def _rows(sections: list[Section]) -> dict[str, dict[str, str]]:
    """Sections as {title: {label: value}} for easy asserts."""
    return {section.title: dict(section.rows) for section in sections}


def _stored_header(path: Path, *, compressed: bool = False) -> bytes:
    """The header bytes exactly as the file stores them (decompressing a TWTZ)."""
    raw = path.read_bytes()
    if compressed:
        from tapewyrm_archive._zstd import zstd

        raw = zstd.decompress(raw)
    (hlen,) = struct.unpack_from("<I", raw, 6)
    return raw[10 : 10 + hlen]


# ---------------------------------------------------------------------------
# Synthetic files
# ---------------------------------------------------------------------------


def _twrf(path: Path) -> Path:
    header = CaptureHeader(
        rate_kbps=1000,
        sample_clock_hz=72_000_000,
        track=3,
        direction=Direction.REVERSE,
        pass_id=1,
        utc="2026-10-03T12:00:00+00:00",
        tape_format=TapeFormat.QIC3020,
        segments_per_track=1475,
        tracks=40,
        device_serial="SYNTH0",
        drive_status=0x25,
        drive_config=0x18,
        drive_rom=0x6A,
        drive_vendor_id=0x0047,
        tape_status=0x63,
        tw_commit="0" * 40,
        firmware_commit="1" * 40,
        firmware_dirty=False,
    )
    # A short body: three intervals and the terminator (TWS-1 5.1, 5.4).
    RawFluxCapture(header=header, flux=b"\x10\x20\x30\x00").save(path)
    return path


def _image_header(count: int) -> dict:
    return {
        "format": "TWTI",
        "version": 1,
        "created": "2026-10-03T12:00:00+00:00",
        "tw_commit": "0" * 40,
        "segment_count": count,
        "segment_stride": SEGMENT_STRIDE,
        "geometry": {
            "tracks": 2,
            "segments_per_track": count // 2,
            "sectors_per_segment": 32,
            "ftk_per_side": 255,
        },
        "qic80_header": {
            "format_code": 4,
            "tape_name": "SYNTHETIC",
            "valid_signature": True,
            "revision": 13,
            "tracks": 2,
            "segments_per_track": count // 2,
            "header_seg": 0,
            "dup_header_seg": 1,
            "first_data_seg": 2,
            "last_data_seg": count - 1,
            # 2026-10-03 12:00:00: (2026-1970) << 25 | 0-based month 9, day 2.
            "format_date": (56 << 25) | (0 + 60 * (0 + 60 * (12 + 24 * (2 + 31 * 9)))),
            "write_date": 0,
        },
        "drive": _DRIVE,
        "sources": [
            {
                "file": "synthetic/track-00.twrf",
                "verified": True,
                "sectors": 64,
                "twrf": {"track": 0, "direction": "forward", "pass_id": 1, "utc": "2026"},
            }
        ],
    }


def _image(path: Path, count: int = 4) -> Path:
    """A ``count``-segment image (TWTI or TWTZ by suffix): two clean, one corrected, rest missing."""
    payload = b"hello"
    states = [SegmentState.CLEAN, SegmentState.CLEAN, SegmentState.CORRECTED]
    entries = [
        SegmentEntry(states[n], 0, len(payload))
        if n < len(states)
        else SegmentEntry(SegmentState.MISSING)
        for n in range(count)
    ]
    image = TapeImage(header=_image_header(count), entries=entries)
    image.save(path, lambda n: payload if n < len(states) else b"")
    return path


def _random_twtz(path: Path, count: int = 60) -> Path:
    """A TWTZ of ``count`` clean segments of distinct random bytes (~1.7 MB packed).

    Distinct per segment, or zstd would find the repeats and pack it small.
    """
    entries = [SegmentEntry(SegmentState.CLEAN, 0, SEGMENT_STRIDE)] * count
    image = TapeImage(header=_image_header(count), entries=entries)
    image.save(path, lambda n: os.urandom(SEGMENT_STRIDE))
    return path


def _twvl(path: Path) -> Path:
    data = bytes(range(256)) * 4  # 1,024 volume bytes
    header = {
        "format": "TWVL",
        "version": 1,
        "volume_index": 0,
        "tape_name": "SYNTHETIC",
        "vtbl": {
            "signature": "VTBL",
            "start_seg": 2,
            "end_seg": 9,
            "description": "Synthetic backup",
            "flags": 0x30,
            "date": 0,
            "date_decoded": [2026, 10, 3, 12, 0, 0],
            "compressed": False,
        },
        "data_section_size": 1000,
        "dir_section_size": 24,
        "directory_offset": 1000,
        "volume_size": len(data),
        "holes": [[100, 200], [300, 356]],
        "lost_segments": [4, 5],
        "source_image": "synthetic/image.twtz",
        "drive": _DRIVE,
    }
    Volume(header=header, data=data).save(path)
    return path


def _cut(path: Path, length: int) -> Path:
    with path.open("r+b") as f:
        f.truncate(length)
    return path


# ---------------------------------------------------------------------------
# Identification and verbatim headers
# ---------------------------------------------------------------------------


def test_twrf_header_and_body_size(tmp_path):
    path = _twrf(tmp_path / "capture.twrf")
    found = inspect(path)
    assert found.kind == "TWRF" and found.version == 2
    assert found.header_bytes == _stored_header(path)
    assert found.header == json.loads(_stored_header(path))
    assert found.body_size == 4 and found.file_size == os.path.getsize(path)


@pytest.mark.parametrize("suffix", [".twti", ".twtz"])
def test_image_header_counts_and_size(tmp_path, suffix):
    path = _image(tmp_path / f"image{suffix}")
    found = inspect(path)
    assert found.kind == suffix[1:].upper()
    assert found.header_bytes == _stored_header(path, compressed=suffix == ".twtz")
    assert found.segment_counts == {"CLEAN": 2, "CORRECTED": 1, "MISSING": 1}
    table_end = 10 + found.header_len + 4 * 8
    assert found.image_size == table_end + 4 * SEGMENT_STRIDE


def test_twvl_header(tmp_path):
    path = _twvl(tmp_path / "vol-00.twvl")
    found = inspect(path)
    assert found.kind == "TWVL"
    assert found.header_bytes == _stored_header(path)
    assert found.body_size == 1024


def test_format_comes_from_magic_not_name(tmp_path):
    """A TWTZ named .twrf and a TWVL named .twti are still what their magic says."""
    twtz = _image(tmp_path / "image.twtz")
    twtz.rename(tmp_path / "misnamed.twrf")
    twvl = _twvl(tmp_path / "vol.twvl")
    twvl.rename(tmp_path / "misnamed.twti")
    assert inspect(tmp_path / "misnamed.twrf").kind == "TWTZ"
    assert inspect(tmp_path / "misnamed.twti").kind == "TWVL"


def test_unknown_magic_is_refused(tmp_path):
    path = tmp_path / "notes.twrf"
    path.write_bytes(b"hello, not a capture")
    with pytest.raises(ValueError, match="not a Tapewyrm file"):
        inspect(path)


# ---------------------------------------------------------------------------
# TWTZ: only the prefix is decompressed
# ---------------------------------------------------------------------------


def test_twtz_reads_only_a_bounded_prefix(tmp_path):
    """Incompressible segment data makes the TWTZ ~1.7 MB; inspect reads one chunk of it.

    Cutting the file right after what inspect read must not change the
    result: nothing past that point was ever looked at.
    """
    path = _random_twtz(tmp_path / "image.twtz")
    size = path.stat().st_size
    assert size > 1_000_000
    found = inspect(path)
    assert found.bytes_read <= 64 * 1024 < size
    cut = _cut(path, found.bytes_read)
    assert inspect(cut).header_bytes == found.header_bytes


# ---------------------------------------------------------------------------
# Truncated and malformed files
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("builder", [_twrf, _twvl, _image])
def test_cut_preamble_is_truncated(tmp_path, builder):
    path = _cut(builder(tmp_path / "file.twti"), 7)
    with pytest.raises(TruncatedFileError, match="preamble"):
        inspect(path)


@pytest.mark.parametrize("builder", [_twrf, _twvl, _image])
def test_cut_header_is_truncated(tmp_path, builder):
    path = _cut(builder(tmp_path / "file.twti"), 40)
    with pytest.raises(TruncatedFileError, match="JSON header"):
        inspect(path)


def test_twti_cut_in_its_data_area_is_truncated(tmp_path):
    path = _image(tmp_path / "image.twti")
    _cut(path, path.stat().st_size - 1)
    with pytest.raises(TruncatedFileError, match="segment data area"):
        inspect(path)


def test_twtz_cut_in_its_header_is_truncated(tmp_path):
    """A stream that ends before the header does is truncated, in decompressed bytes."""
    path = _cut(_random_twtz(tmp_path / "image.twtz"), 40)
    with pytest.raises(TruncatedFileError, match="decompressed"):
        inspect(path)


def test_twvl_short_of_its_volume_size_is_truncated(tmp_path):
    path = _twvl(tmp_path / "vol.twvl")
    _cut(path, path.stat().st_size - 10)
    with pytest.raises(TruncatedFileError, match="volume bytes"):
        inspect(path)


def test_unreadable_version_is_malformed(tmp_path):
    path = _twrf(tmp_path / "capture.twrf")
    raw = bytearray(path.read_bytes())
    raw[4:6] = (9).to_bytes(2, "little")
    path.write_bytes(bytes(raw))
    with pytest.raises(MalformedFileError, match="version 9"):
        inspect(path)


def test_twrf_header_missing_a_member_is_malformed(tmp_path):
    """The TWRF reader's own member checks apply."""
    path = tmp_path / "capture.twrf"
    header = json.dumps({"rate_kbps": 500}).encode()
    path.write_bytes(b"TWRF" + struct.pack("<HI", 2, len(header)) + header)
    with pytest.raises(MalformedFileError, match="lacks"):
        inspect(path)


# ---------------------------------------------------------------------------
# describe()
# ---------------------------------------------------------------------------


def test_describe_twrf_decodes_the_drive_reports(tmp_path):
    rows = _rows(describe(inspect(_twrf(tmp_path / "capture.twrf"))))
    assert rows["Capture"]["Geometry"] == "40 tracks x 1,475 segments = 59,000"
    assert rows["Capture"]["Track"] == "3 (reverse)"
    assert rows["Capture"]["Captured"] == "2026-10-03T12:00:00+00:00"
    assert rows["Capture"]["Sample clock"] == "72,000,000 Hz"
    drive = rows["Drive"]
    assert drive["Tape status"] == "0x63 -> QIC3020, variable length 900 Oe"
    assert drive["Drive config"] == "0x18 -> 1,000 kbit/s"
    assert drive["Vendor"] == "0x0047 -> Colorado Memory Systems (legacy ID)"
    assert drive["Drive status"] == "0x25 -> ready, cartridge present, referenced"


def test_describe_image_lists_counts_geometry_and_sources(tmp_path):
    rows = _rows(describe(inspect(_image(tmp_path / "image.twtz"))))
    assert rows["Geometry"]["Segments"] == "2 tracks x 2 segments = 4"
    assert rows["Segments"] == {
        "clean": "2 (50.00%)",
        "corrected": "1 (25.00%)",
        "missing": "1 (25.00%)",
    }
    assert rows["Tape header (QIC-80 format parameters)"]["Formatted"].startswith(
        "2026-10-03 12:00:00"
    )
    assert rows["Drive"]["Capture rate"] == "1,000 kbit/s"
    source = rows["Sources"]["synthetic/track-00.twrf"]
    assert "verified" in source and "64 sectors" in source


def test_describe_twvl_totals_holes_and_lost_segments(tmp_path):
    rows = _rows(describe(inspect(_twvl(tmp_path / "vol-00.twvl"))))
    assert rows["Recovery"]["Holes"] == "2 ranges, 156 bytes, 15.23% of the volume"
    assert rows["Recovery"]["Lost segments"] == "2: 4, 5"
    entry = rows["Volume table entry"]
    assert entry["Segments"] == "2 to 9 (8 segments)"
    assert entry["Flags"] == "0x30 -> segment spanning, directory last"
    assert entry["Date"] == "2026-10-03 12:00:00 (0x00000000)"

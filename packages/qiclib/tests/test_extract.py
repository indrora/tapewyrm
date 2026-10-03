"""`tw extract` (image.twvl.extract): volumes read through the tape profile.

The regression this pins down is captures/old-connor.twti: an MTN-written tape
whose volume table the plain Rev N parser misreads (data size taken from the
middle of the label, compression read as off) and whose data segments start
with a 4-byte extent offset, not Rev G's 8.
"""

from __future__ import annotations

import struct
from pathlib import Path

import pytest
from tapewyrm_archive.twti import SEGMENT_STRIDE, SegmentEntry, SegmentState, TapeImage
from tapewyrm_archive.twvl import Volume

from qiclib import segment as seg_mod
from qiclib.extract import extract
from qiclib.testing.builders import (
    build_header_segment,
    build_volume_table_segment,
    build_vtbl_entry,
)

SEGMENT_BYTES = 29 * 1024
# QIC-122 Rev B Appendix A: this stream decompresses to b"ABAAAAAACABABABA".
APPENDIX_A = bytes.fromhex("20 90 88 38 1C 21 E2 5C 15 80")


def _mtn_entry(*, start_seg: int, end_seg: int, dir_size: int, data_size: int) -> bytes:
    """A VTBL record laid out the way profiles/tape/mtn.toml describes."""
    rec = bytearray(build_vtbl_entry(start_seg=start_seg, end_seg=end_seg, description=""))
    rec[58:61] = b"MTN"
    struct.pack_into("<I", rec, 92, dir_size)
    struct.pack_into("<I", rec, 96, data_size)  # doubleword; Rev N reads a quadword here
    rec[102:118] = b"SBPRO DISK1     "  # lands in Rev N's data_section_size high bytes
    rec[120] = 0x81  # compressed, QIC-123 code 1
    return bytes(rec)


def _extent(offset: int, *frames: tuple[bool, bytes]) -> bytes:
    """One data segment: 4-byte uncompressed offset, then (raw?, body) frames."""
    out = struct.pack("<I", offset)
    for raw, body in frames:
        out += struct.pack("<H", (0x8000 if raw else 0) | len(body)) + body
    return out.ljust(SEGMENT_BYTES, b"\x00")


def _write_image(path: Path, segments: dict[int, bytes]) -> Path:
    count = max(segments) + 1
    entries = [
        SegmentEntry(SegmentState.CLEAN, 0, len(segments[n])) if n in segments
        else SegmentEntry(SegmentState.MISSING)
        for n in range(count)
    ]  # fmt: skip
    header = {
        "format": "TWTI",
        "segment_count": count,
        "segment_stride": SEGMENT_STRIDE,
        "qic80_header": {
            "header_seg": 0,
            "dup_header_seg": 1,
            "first_data_seg": 2,
            "tape_name": "OLD CONNOR",
        },
    }
    TapeImage(header=header, entries=entries).save(path, lambda n: segments.get(n, b""))
    return path


def _mtn_image(tmp_path: Path) -> Path:
    # Directory stored raw in segment 3, compressed data in segment 4: the
    # shape of old-connor's segments 4-6.
    vtbl = build_volume_table_segment(
        2, [_mtn_entry(start_seg=3, end_seg=4, dir_size=5, data_size=16)]
    )
    return _write_image(
        tmp_path / "mtn.twti",
        {
            0: seg_mod.segment_data(build_header_segment(0)),
            1: seg_mod.segment_data(build_header_segment(1)),
            2: seg_mod.segment_data(vtbl),
            3: _extent(0, (True, b"HELLO")),
            4: _extent(5, (False, APPENDIX_A)),
        },
    )


def test_mtn_volume_extracts_with_four_byte_extent_offsets(tmp_path):
    [path] = extract(_mtn_image(tmp_path), tmp_path / "out")
    vol = Volume.load(path)
    assert vol.data == b"HELLO" + b"ABAAAAAACABABABA"
    assert vol.holes == []
    assert vol.header["lost_segments"] == []


def test_wrong_profile_size_is_refused_not_a_memory_error(tmp_path):
    # Read as Rev N, bytes 96-103 hold the size *and* "SB" of the label: ~10^18.
    with pytest.raises(ValueError, match="wrong layout"):
        extract(_mtn_image(tmp_path), tmp_path / "out", tape_profile="qic80-rev-n")

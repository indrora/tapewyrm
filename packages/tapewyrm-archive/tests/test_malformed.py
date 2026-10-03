"""Truncated and malformed TWTI/TWTZ/TWVL files are refused with a clear error.

TWS-2 section 9.2 and TWS-3 section 6.2: a reader MUST NOT hand back short
data from a truncated file as if it were the image. Every case here cuts or
corrupts a file that round-trips fine, then checks that opening it raises a
``MalformedFileError`` (a ``ValueError``) naming the file and the sizes.
"""

from __future__ import annotations

import json
import struct
from pathlib import Path

import pytest

from tapewyrm_archive.errors import MalformedFileError, TruncatedFileError
from tapewyrm_archive.twti import _ENTRY, SEGMENT_STRIDE, SegmentEntry, SegmentState, TapeImage
from tapewyrm_archive.twvl import SparseVolume, Volume

_COUNT = 3


def _good_twti(path: Path) -> Path:
    """A valid 3-segment sparse TWTI with data in segment 0."""
    entries = [SegmentEntry(SegmentState.CLEAN, 0, 5, 0)] + [SegmentEntry(SegmentState.MISSING)] * (
        _COUNT - 1
    )
    header = {"format": "TWTI", "segment_count": _COUNT, "segment_stride": SEGMENT_STRIDE}
    TapeImage(header=header, entries=entries).save(path, lambda n: b"hello" if n == 0 else b"")
    return path


def _layout(path: Path) -> tuple[int, int, int]:
    """(header end, table end, file length L) of a TWTI file."""
    raw = path.read_bytes()[:10]
    (hlen,) = struct.unpack_from("<I", raw, 6)
    header_end = 10 + hlen
    table_end = header_end + _COUNT * _ENTRY.size
    return header_end, table_end, table_end + _COUNT * SEGMENT_STRIDE


def _cut(path: Path, length: int) -> Path:
    """Truncate ``path`` to ``length`` bytes (a sparse file keeps its holes)."""
    with path.open("r+b") as f:
        f.truncate(length)
    return path


# ---------------------------------------------------------------------------
# TWTI
# ---------------------------------------------------------------------------


def test_good_image_still_opens(tmp_path):
    with TapeImage.open(_good_twti(tmp_path / "t.twti")) as img:
        assert img.segment(0) == b"hello"


def test_truncated_preamble_is_refused(tmp_path):
    path = _cut(_good_twti(tmp_path / "t.twti"), 7)  # magic + 3 bytes
    with pytest.raises(TruncatedFileError, match="t.twti") as info:
        TapeImage.open(path)
    assert info.value.expected == 10 and info.value.found == 7


def test_truncated_header_is_refused(tmp_path):
    path = _good_twti(tmp_path / "t.twti")
    header_end, _, _ = _layout(path)
    _cut(path, header_end - 4)
    with pytest.raises(TruncatedFileError, match="header") as info:
        TapeImage.open(path)
    assert info.value.expected == header_end and info.value.found == header_end - 4


def test_segment_table_cut_short_is_refused(tmp_path):
    path = _good_twti(tmp_path / "t.twti")
    header_end, table_end, _ = _layout(path)
    _cut(path, header_end + _ENTRY.size + 3)
    with pytest.raises(TruncatedFileError, match="segment table") as info:
        TapeImage.open(path)
    assert info.value.expected == table_end


def test_data_area_shorter_than_required_is_refused(tmp_path):
    path = _good_twti(tmp_path / "t.twti")
    _, _, length = _layout(path)
    _cut(path, length - 1)
    with pytest.raises(TruncatedFileError, match="data area") as info:
        TapeImage.open(path)
    assert info.value.expected == length and info.value.found == length - 1


def test_sparse_image_is_judged_by_length_not_allocation(tmp_path):
    """A TWTI that is all holes past its header is complete if its length is."""
    path = tmp_path / "holes.twti"
    entries = [SegmentEntry(SegmentState.MISSING)] * _COUNT
    TapeImage(header={"segment_count": _COUNT}, entries=entries).save(path, lambda n: b"")
    with TapeImage.open(path) as img:
        assert img.segment(_COUNT - 1) == b""


def _rewrite_entry(path: Path, n: int, entry: bytes) -> None:
    _, table_end, _ = _layout(path)
    at = table_end - (_COUNT - n) * _ENTRY.size
    with path.open("r+b") as f:
        f.seek(at)
        f.write(entry)


def test_data_len_over_the_stride_is_refused(tmp_path):
    path = _good_twti(tmp_path / "t.twti")
    _rewrite_entry(path, 1, _ENTRY.pack(SegmentState.CLEAN, 0, SEGMENT_STRIDE + 1, 0))
    with pytest.raises(MalformedFileError, match="data_len"):
        TapeImage.open(path)


def test_reserved_state_is_refused(tmp_path):
    path = _good_twti(tmp_path / "t.twti")
    _rewrite_entry(path, 1, _ENTRY.pack(9, 0, 0, 0))
    with pytest.raises(MalformedFileError, match="state"):
        TapeImage.open(path)


def _with_header(path: Path, header: dict) -> Path:
    hdr = json.dumps(header).encode()
    path.write_bytes(struct.pack("<4sHI", b"TWTI", 1, len(hdr)) + hdr)
    return path


@pytest.mark.parametrize(
    ("header", "problem"),
    [
        ({"segment_count": 0, "segment_stride": 4096}, "segment_stride"),
        ({"segment_count": 0, "format": "TWVL"}, "format"),
        ({"segment_count": -1}, "segment_count"),
        ({"segment_count": "3"}, "segment_count"),
        ({}, "segment_count"),
    ],
)
def test_bad_header_members_are_refused(tmp_path, header, problem):
    with pytest.raises(MalformedFileError, match=problem):
        TapeImage.open(_with_header(tmp_path / "t.twti", header))


def test_header_that_is_not_json_is_refused(tmp_path):
    path = tmp_path / "t.twti"
    path.write_bytes(struct.pack("<4sHI", b"TWTI", 1, 5) + b"{nope")
    with pytest.raises(MalformedFileError, match="JSON"):
        TapeImage.open(path)


def test_twtz_truncated_mid_stream_is_refused_and_cleaned_up(tmp_path, monkeypatch):
    import random
    import tempfile

    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "tmp"))
    (tmp_path / "tmp").mkdir()
    path = tmp_path / "t.twtz"
    entries = [SegmentEntry(SegmentState.CLEAN, 0, SEGMENT_STRIDE, 0)] * _COUNT
    noise = random.Random(1).randbytes(SEGMENT_STRIDE)  # does not compress
    TapeImage(header={"segment_count": _COUNT}, entries=entries).save(path, lambda n: noise)
    _cut(path, path.stat().st_size // 2)
    with pytest.raises(TruncatedFileError, match="t.twtz"):
        TapeImage.open(path)
    assert list((tmp_path / "tmp").iterdir()) == []  # the temp image is gone


def test_twtz_of_a_truncated_twti_is_refused(tmp_path):
    """A complete zstd stream holding a short TWTI is truncation too."""
    from tapewyrm_archive._zstd import zstd

    twti = _good_twti(tmp_path / "t.twti")
    _, _, length = _layout(twti)
    twtz = tmp_path / "t.twtz"
    twtz.write_bytes(zstd.compress(twti.read_bytes()[: length - 100]))
    with pytest.raises(TruncatedFileError, match="t.twtz"):
        TapeImage.open(twtz)


# ---------------------------------------------------------------------------
# TWVL
# ---------------------------------------------------------------------------


def _good_twvl(path: Path) -> Path:
    data = b"0123\x00\x00\x00\x00890"
    header = {"format": "TWVL", "holes": [[4, 8]], "volume_size": len(data)}
    Volume(header=header, data=data).save(path)
    return path


def test_twvl_truncated_in_its_preamble_is_refused(tmp_path):
    path = _cut(_good_twvl(tmp_path / "v.twvl"), 6)
    with pytest.raises(TruncatedFileError, match="v.twvl"):
        Volume.load(path)


def test_twvl_truncated_in_its_header_is_refused(tmp_path):
    path = _good_twvl(tmp_path / "v.twvl")
    _cut(path, 20)
    with pytest.raises(TruncatedFileError, match="header"):
        Volume.load(path)


def test_twvl_truncated_in_its_data_is_refused(tmp_path):
    path = _good_twvl(tmp_path / "v.twvl")
    _cut(path, path.stat().st_size - 2)
    with pytest.raises(TruncatedFileError, match="volume bytes") as info:
        Volume.load(path)
    assert info.value.expected - info.value.found == 2


@pytest.mark.parametrize("size", [None, "missing", -1, "11"])
def test_twvl_without_a_valid_volume_size_is_refused(tmp_path, size):
    """TWS-3 3.1: volume_size is required; without it truncation is undetectable."""
    header = {"format": "TWVL", "holes": [[4, 8]], "volume_size": size}
    if size == "missing":
        del header["volume_size"]
    path = tmp_path / "v.twvl"
    Volume(header=header, data=b"0123\x00\x00\x00\x00890").save(path)
    with pytest.raises(MalformedFileError, match="volume_size") as info:
        Volume.load(path)
    assert "v.twvl" in str(info.value)


def test_twvl_with_the_wrong_format_member_is_refused(tmp_path):
    path = tmp_path / "v.twvl"
    Volume(header={"format": "TWTI", "holes": [], "volume_size": 0}, data=b"").save(path)
    with pytest.raises(MalformedFileError, match="format"):
        Volume.load(path)


def test_volume_read_past_the_end_counts_as_missing():
    """TWS-3 6.2 rule 6: bytes past the end are missing, like a hole."""
    vol = Volume(header={"holes": [[2, 4]]}, data=b"abcdef")
    assert vol.read(4, 4) == (b"ef\x00\x00", 2)
    assert vol.read(0, 8) == (b"abcdef\x00\x00", 4)  # 2 in the hole, 2 past the end
    # overlapping or out-of-range hole ranges do not double-count
    vol = Volume(header={"holes": [[1, 3], [2, 10]]}, data=b"abcdef")
    assert vol.read(0, 8)[1] == 7


# ---------------------------------------------------------------------------
# SparseVolume
# ---------------------------------------------------------------------------


def test_sparse_volume_read_does_not_double_count_overlapping_extents():
    s = SparseVolume(size=20)
    s.add(0, b"a" * 10)
    s.add(5, b"b" * 10)
    data, missing = s.read(0, 15)
    assert missing == 0
    assert s.read(0, 20)[1] == 5


def test_sparse_volume_read_sees_a_long_extent_that_starts_further_back():
    s = SparseVolume(size=100)
    s.add(0, b"a" * 100)
    s.add(10, b"b" * 5)
    assert s.read(50, 10) == (b"a" * 10, 0)
    assert s.coverage() == 100

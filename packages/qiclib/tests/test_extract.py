"""`qicsilver extract` (qiclib.extract): volumes read through the volume profile.

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
    """A VTBL record laid out the way profiles/volume/mtn.toml describes."""
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


def _write_image(
    path: Path,
    segments: dict[int, bytes],
    overrides: dict[int, SegmentEntry] | None = None,
    qic80: dict | None = None,
) -> Path:
    """Save a TWTI; ``overrides`` replaces the default entry (CLEAN/MISSING) per segment.

    ``qic80`` replaces the header's qic80_header object (malformed-header tests).
    """
    overrides = overrides or {}
    count = max([*segments, *overrides]) + 1
    entries = [
        overrides[n] if n in overrides
        else SegmentEntry(SegmentState.CLEAN, 0, len(segments[n])) if n in segments
        else SegmentEntry(SegmentState.MISSING)
        for n in range(count)
    ]  # fmt: skip
    header = {
        "format": "TWTI",
        "segment_count": count,
        "segment_stride": SEGMENT_STRIDE,
        "qic80_header": qic80
        if qic80 is not None
        else {
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


def test_extract_from_a_zstd_compressed_image(tmp_path):
    """`zstd x.twti` output (TWTZ) extracts exactly like the plain image."""
    from tapewyrm_archive._zstd import zstd

    twtz = tmp_path / "mtn.twtz"
    twtz.write_bytes(zstd.compress(_mtn_image(tmp_path).read_bytes()))
    [path] = extract(twtz, tmp_path / "out")
    assert Volume.load(path).data == b"HELLO" + b"ABAAAAAACABABABA"


def test_wrong_profile_size_is_refused_not_a_memory_error(tmp_path):
    # Read as Rev N, bytes 96-103 hold the size *and* "SB" of the label: ~10^18.
    with pytest.raises(ValueError, match="wrong layout"):
        extract(_mtn_image(tmp_path), tmp_path / "out", volume_profile="qic80-rev-n")


K = 1024


def _plain_entry(*, start_seg: int, end_seg: int, data_size: int) -> bytes:
    """An uncompressed Rev N VTBL record with a quadword data_section_size."""
    rec = bytearray(build_vtbl_entry(start_seg=start_seg, end_seg=end_seg, description="PLAIN"))
    struct.pack_into("<Q", rec, 96, data_size)
    return bytes(rec)


def _plain_image(tmp_path: Path, *, end_seg: int = 6) -> Path:
    """Uncompressed volume over segments 3..6 whose segments are not all 29 KiB.

    seg 3: CLEAN, 2 sectors excluded by the bad-sector map -> 27 KiB of b"a"
    seg 4: MISSING, 1 sector excluded                      -> 28 KiB hole
    seg 5: BAD (whole segment mapped out)                  -> contributes nothing
    seg 6: CLEAN, full                                     -> 29 KiB of b"c"
    """
    vtbl = build_volume_table_segment(
        2, [_plain_entry(start_seg=3, end_seg=end_seg, data_size=(27 + 28 + 29) * K)]
    )
    return _write_image(
        tmp_path / "plain.twti",
        {
            0: seg_mod.segment_data(build_header_segment(0)),
            1: seg_mod.segment_data(build_header_segment(1)),
            2: seg_mod.segment_data(vtbl),
            3: b"a" * (27 * K),
            6: b"c" * (29 * K),
        },
        {
            3: SegmentEntry(SegmentState.CLEAN, 0, 27 * K, 0b11 << 5),
            4: SegmentEntry(SegmentState.MISSING, 0, 0, 1 << 7),
            5: SegmentEntry(SegmentState.BAD),
        },
    )


def test_uncompressed_volume_concatenates_short_segments_in_order(tmp_path):
    """Each segment lands after the previous one's usable bytes, not at n * 29 KiB."""
    [path] = extract(_plain_image(tmp_path), tmp_path / "out")
    vol = Volume.load(path)
    assert vol.data == b"a" * (27 * K) + bytes(28 * K) + b"c" * (29 * K)
    assert vol.holes == [(27 * K, 55 * K)]
    assert vol.header["lost_segments"] == [4]


def test_end_seg_past_image_is_a_value_error(tmp_path):
    """A volume running off the end of the image is refused, not an IndexError."""
    with pytest.raises(ValueError, match="past the end"):
        extract(_plain_image(tmp_path, end_seg=9), tmp_path / "out")


def test_uncompressed_directory_last_keeps_the_gap_and_records_the_directory(tmp_path):
    """QIC-113 Rev G §7: data, Segment Gap, then the directory on a segment boundary.

    Sizing the volume from the table (data + directory = 150 bytes) used to cut
    the directory off; the volume is every segment's bytes, and the directory's
    exact start is the first segment boundary after the data section.
    """
    rec = bytearray(
        build_vtbl_entry(
            start_seg=3, end_seg=4, description="DIRLAST", flags=0x20, dir_section_size=50
        )
    )
    struct.pack_into("<Q", rec, 96, 100)  # data_section_size (Rev N quadword)
    vtbl = build_volume_table_segment(2, [bytes(rec)])
    path = _write_image(
        tmp_path / "dirlast.twti",
        {
            0: seg_mod.segment_data(build_header_segment(0)),
            1: seg_mod.segment_data(build_header_segment(1)),
            2: seg_mod.segment_data(vtbl),
            3: (b"d" * 100).ljust(SEGMENT_BYTES, b"\x00"),  # data, then the gap
            4: (b"D" * 50).ljust(SEGMENT_BYTES, b"\x00"),  # directory section
        },
    )
    [out] = extract(path, tmp_path / "out", volume_profile="qic80-rev-n")
    vol = Volume.load(out)
    assert vol.header["directory_offset"] == SEGMENT_BYTES
    assert vol.data[:100] == b"d" * 100
    assert vol.data[SEGMENT_BYTES : SEGMENT_BYTES + 50] == b"D" * 50


def test_volume_header_records_size_and_only_dir_and_name_of_the_image(tmp_path):
    """source_image is ``dir/name``, never the absolute path (TWS-3 3.1, 8.2)."""
    image = _mtn_image(tmp_path)
    [path] = extract(image.resolve(), tmp_path / "out")
    vol = Volume.load(path)
    assert vol.header["source_image"] == f"{tmp_path.name}/mtn.twti"
    assert vol.header["volume_size"] == len(vol.data)


@pytest.mark.parametrize(
    ("qic80", "member"),
    [
        ({"header_seg": 0, "dup_header_seg": 1, "tape_name": "T"}, "first_data_seg"),
        ({"header_seg": 0, "first_data_seg": "2", "tape_name": "T"}, "first_data_seg"),
        ({"header_seg": 0, "first_data_seg": 99, "tape_name": "T"}, "first_data_seg"),
        ({"header_seg": 0, "first_data_seg": 2}, "tape_name"),
        ({"header_seg": 0, "first_data_seg": 2, "tape_name": 7}, "tape_name"),
    ],
)
def test_malformed_qic80_header_is_a_clean_error(tmp_path, qic80, member):
    """A header member extract needs that is absent, mistyped or out of range is refused
    as a malformed file naming the member -- not a KeyError, TypeError or IndexError.
    """
    from tapewyrm_archive.errors import MalformedFileError

    with TapeImage.open(_mtn_image(tmp_path)) as good:
        segments = {n: good.segment(n) for n in range(len(good.entries))}
    broken = _write_image(tmp_path / "broken.twti", segments, qic80=qic80)
    with pytest.raises(MalformedFileError, match=f"qic80_header.{member}") as info:
        extract(broken, tmp_path / "out")
    assert "broken.twti" in str(info.value)


# ---------------------------------------------------------------------------
# Volume selection and output naming (--volumes, --prefix, -o)
# ---------------------------------------------------------------------------


def _multi_image(tmp_path: Path, count: int = 3) -> Path:
    """``count`` one-segment uncompressed volumes; volume k's bytes are all ``k``."""
    entries = []
    for k in range(count):
        rec = bytearray(build_vtbl_entry(start_seg=3 + k, end_seg=3 + k, description=f"VOLUME {k}"))
        struct.pack_into("<Q", rec, 96, 29 * K)
        entries.append(bytes(rec))
    segments = {
        0: seg_mod.segment_data(build_header_segment(0)),
        1: seg_mod.segment_data(build_header_segment(1)),
        2: seg_mod.segment_data(build_volume_table_segment(2, entries)),
    }
    segments.update({3 + k: bytes([k]) * (29 * K) for k in range(count)})
    return _write_image(tmp_path / "multi.twti", segments)


@pytest.mark.parametrize(
    ("spec", "expected"),
    [("0", [0]), ("0,2", [0, 2]), ("1-3", [1, 2, 3]), ("0,2-4", [0, 2, 3, 4]), ("3,1,3", [1, 3])],
)
def test_parse_volume_list(spec, expected):
    """Single numbers, ranges and lists, sorted and de-duplicated (tw dump's TRACKS syntax)."""
    from qiclib.extract import parse_volume_list

    assert parse_volume_list(spec) == expected


@pytest.mark.parametrize("spec", ["", "a", "3-1", "-1", "1-", "1,,2", "0x1"])
def test_parse_volume_list_rejects_nonsense(spec):
    from qiclib.extract import parse_volume_list

    with pytest.raises(ValueError, match="bad volume"):
        parse_volume_list(spec)


def test_volumes_selects_only_those_volumes(tmp_path):
    """Only the selected volumes are written, still named by their table index."""
    written = extract(_multi_image(tmp_path), tmp_path / "out", volumes=[0, 2])
    assert [p.name for p in written] == ["vol-00.twvl", "vol-02.twvl"]
    assert sorted(p.name for p in (tmp_path / "out").iterdir()) == ["vol-00.twvl", "vol-02.twvl"]
    assert Volume.load(written[1]).data == bytes([2]) * (29 * K)


def test_prefix_names_the_output_files(tmp_path):
    written = extract(_multi_image(tmp_path), tmp_path / "out", prefix="jc-")
    assert [p.name for p in written] == ["jc-00.twvl", "jc-01.twvl", "jc-02.twvl"]


def test_outfile_names_the_single_selected_volume(tmp_path):
    """-o: a plain path for the one selected volume; out_dir is not used."""
    target = tmp_path / "sub" / "only.twvl"
    written = extract(_multi_image(tmp_path), tmp_path / "unused", volumes=[1], outfile=target)
    assert written == [target]
    assert Volume.load(target).data == bytes([1]) * (29 * K)
    assert not (tmp_path / "unused").exists()


def test_outfile_with_a_one_volume_image_needs_no_selection(tmp_path):
    target = tmp_path / "one.twvl"
    assert extract(_multi_image(tmp_path, count=1), tmp_path, outfile=target) == [target]


def test_outfile_with_several_volumes_is_refused_before_writing(tmp_path):
    with pytest.raises(ValueError, match="3 volumes"):
        extract(_multi_image(tmp_path), tmp_path / "out", outfile=tmp_path / "x.twvl")
    with pytest.raises(ValueError, match="2 volumes"):
        extract(
            _multi_image(tmp_path), tmp_path / "out", volumes=[0, 1], outfile=tmp_path / "x.twvl"
        )
    assert not (tmp_path / "out").exists() and not (tmp_path / "x.twvl").exists()


def test_volume_not_on_the_tape_is_refused_before_writing(tmp_path):
    """The error names the valid range, and nothing is written, not even the good ones."""
    with pytest.raises(ValueError, match=r"no volume 5.*0-2"):
        extract(_multi_image(tmp_path), tmp_path / "out", volumes=[0, 5])
    assert not (tmp_path / "out").exists()

"""TWTI tape images and TWVL volumes: round trips, holes, random access."""

from tapewyrm_archive.twti import SEGMENT_STRIDE, SegmentEntry, SegmentState, TapeImage
from tapewyrm_archive.twvl import SparseVolume, Volume, find_holes


def test_tape_image_round_trip_and_random_access(tmp_path):
    entries = [
        SegmentEntry(SegmentState.CLEAN, 0, 5, 0),
        SegmentEntry(SegmentState.MISSING),
        SegmentEntry(SegmentState.CORRECTED, 2, 3, 1 << 11),  # sector 11 excluded
        SegmentEntry(SegmentState.BAD),
    ]
    data = {0: b"hello", 2: b"abc"}
    img = TapeImage(header={"format": "TWTI", "segment_count": 4, "drive": {"drive_config": 0x90}},
                    entries=entries)  # fmt: skip
    path = tmp_path / "t.twti"
    img.save(path, lambda n: data.get(n, b""))
    back = TapeImage.open(path)
    assert back.header["drive"]["drive_config"] == 0x90
    assert back.entries == entries
    assert back.segment(0) == b"hello" and back.segment(2) == b"abc"
    assert back.segment(1) == b""  # missing
    assert back.counts() == {"CLEAN": 1, "MISSING": 1, "CORRECTED": 1, "BAD": 1}
    assert path.stat().st_size > 4 * SEGMENT_STRIDE  # fixed stride per segment


def test_volume_round_trip_and_hole_accounting(tmp_path):
    vol = Volume(header={"format": "TWVL", "holes": [[4, 8]]}, data=b"0123\x00\x00\x00\x00890")
    path = tmp_path / "v.twvl"
    vol.save(path)
    back = Volume.load(path)
    assert back.data == vol.data and back.holes == [(4, 8)]
    assert back.read(2, 4) == (b"23\x00\x00", 2)  # two of the four bytes are in the hole
    assert back.read(8, 3) == (b"890", 0)


def testholes_from_sparse_extents():
    s = SparseVolume(size=100)
    s.add(10, b"x" * 20)
    s.add(50, b"y" * 10)
    assert find_holes(s, 100) == [[0, 10], [30, 50], [60, 100]]


def test_tape_image_save_reports_one_step_per_segment(tmp_path):
    """save() opens one "writing image" task in segments and finishes it."""
    from contextlib import contextmanager

    seen: list[tuple[str, float | None, str]] = []
    position = []

    class _Task:
        def advance(self, amount: float = 1) -> None:
            position.append(amount)

        def update(self, completed: float) -> None:
            raise AssertionError("save advances; it never jumps")

    class _Progress:
        @contextmanager
        def task(self, description, total=None, unit=""):
            seen.append((description, total, unit))
            yield _Task()

    img = TapeImage(header={"segment_count": 3}, entries=[SegmentEntry(SegmentState.MISSING)] * 3)
    img.save(tmp_path / "t.twti", lambda n: b"", progress=_Progress())
    assert seen == [("writing image", 3, "segments")]
    assert sum(position) == 3


# ---------------------------------------------------------------------------
# Sparse TWTI and zstd TWTZ
# ---------------------------------------------------------------------------


def _image(count: int = 4) -> tuple[TapeImage, dict[int, bytes]]:
    entries = [SegmentEntry(SegmentState.MISSING)] * count
    entries[0] = SegmentEntry(SegmentState.CLEAN, 0, 5, 0)
    entries[2] = SegmentEntry(SegmentState.CORRECTED, 1, SEGMENT_STRIDE, 0)
    data = {0: b"hello", 2: bytes(range(256)) * (SEGMENT_STRIDE // 256)}
    return TapeImage(header={"segment_count": count}, entries=entries), data


def _dense_bytes(img: TapeImage, data: dict[int, bytes], tmp_path) -> bytes:
    """The TWTI byte stream as the pre-sparse writer laid it out, for comparison."""
    import json

    from tapewyrm_archive.twti import _ENTRY, _PREAMBLE, MAGIC, VERSION

    hdr = json.dumps(img.header, indent=1).encode("utf-8")
    out = _PREAMBLE.pack(MAGIC, VERSION, len(hdr)) + hdr
    out += b"".join(
        _ENTRY.pack(e.state, e.erasures, e.data_len, e.excluded_mask) for e in img.entries
    )
    return out + b"".join(
        data.get(n, b"").ljust(SEGMENT_STRIDE, b"\x00") for n in range(len(img.entries))
    )


def test_sparse_twti_reads_back_byte_identical(tmp_path):
    img, data = _image()
    data[3] = bytes(100)  # all-zero data is a hole too
    path = tmp_path / "t.twti"
    img.save(path, lambda n: data.get(n, b""))
    assert path.read_bytes() == _dense_bytes(img, data, tmp_path)
    with TapeImage.open(path) as back:
        assert back.segment(0) == b"hello" and back.segment(2) == data[2]


def test_sparse_twti_allocates_less_than_its_length(tmp_path):
    import os

    import pytest

    # ~23 MB logical, two segments of data. Bigger than 16 MiB on purpose: APFS
    # quietly densifies smaller files that have interior holes (measured on
    # macOS 26: an 11.9 MB file with a 70 KB gap came back fully allocated).
    img, data = _image(800)
    path = tmp_path / "t.twti"
    img.save(path, lambda n: data.get(n, b""))
    st = os.stat(path)
    if not hasattr(st, "st_blocks"):
        pytest.skip("platform reports no st_blocks")
    assert st.st_size > 800 * SEGMENT_STRIDE
    if st.st_blocks * 512 >= st.st_size:
        pytest.skip("filesystem made no holes")
    assert st.st_blocks * 512 < st.st_size // 10


def test_twtz_round_trip_is_a_zstd_twti(tmp_path):
    from tapewyrm_archive._zstd import zstd

    img, data = _image(50)
    path = tmp_path / "t.twtz"
    img.save(path, lambda n: data.get(n, b""))
    raw = path.read_bytes()
    assert raw[:4] == b"\x28\xb5\x2f\xfd"
    assert len(raw) < 50 * SEGMENT_STRIDE // 10  # zero slots compress away
    assert zstd.decompress(raw) == _dense_bytes(img, data, tmp_path)  # == `zstd -d`
    back = TapeImage.open(path)
    assert back.entries == img.entries
    assert back.segment(0) == b"hello" and back.segment(2) == data[2]
    assert back.segment(1) == b""


def test_open_sniffs_magic_not_suffix(tmp_path):
    from tapewyrm_archive.twti import sniff

    img, data = _image()
    twtz, twti = tmp_path / "a.twtz", tmp_path / "b.twti"
    img.save(twtz, lambda n: data.get(n, b""))
    img.save(twti, lambda n: data.get(n, b""))
    swapped_z, swapped_i = twtz.rename(tmp_path / "a.twti.bak"), twti.rename(tmp_path / "b.twtz")
    assert sniff(swapped_z) == "TWTZ" and sniff(swapped_i) == "TWTI"
    for p in (swapped_z, swapped_i):
        with TapeImage.open(p) as back:
            assert back.segment(0) == b"hello"
    junk = tmp_path / "junk.twti"
    junk.write_bytes(b"nope" * 4)
    assert sniff(junk) is None


def test_twtz_temp_file_is_removed_on_close(tmp_path):
    img, data = _image()
    path = tmp_path / "t.twtz"
    img.save(path, lambda n: data.get(n, b""))
    back = TapeImage.open(path)
    assert back._finalizer is not None
    _obj, _func, args, _kwargs = back._finalizer.peek()  # (mm, file, temp path)
    temp = args[2]
    from pathlib import Path

    assert Path(temp).exists()
    back.close()
    back.close()  # idempotent
    assert not Path(temp).exists()


def test_twtz_holding_garbage_is_refused_and_cleaned_up(tmp_path):
    import pytest

    from tapewyrm_archive._zstd import zstd

    path = tmp_path / "x.twtz"
    path.write_bytes(zstd.compress(b"not a tape image at all"))
    with pytest.raises(ValueError, match="not a TWTI"):
        TapeImage.open(path)

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

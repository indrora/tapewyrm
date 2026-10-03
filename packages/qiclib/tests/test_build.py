"""qiclib.build: sectors -> TWTI image."""

from __future__ import annotations

from tapewyrm_archive.twti import SegmentState, TapeImage

from qiclib.build import build_image
from qiclib.testing.builders import (
    build_header_segment,
    build_volume_table_segment,
    build_vtbl_entry,
    segment_raw_sectors,
)


def test_missing_segment_carries_the_bad_sector_maps_exclusions(tmp_path):
    """A segment never read still has the map's excluded sectors recorded.

    The map describes the tape, not what was read: qiclib.extract sizes a lost
    segment's hole from this mask, and mask 0 would make it a full 29 KB.
    """
    seg, slot = 5, 3
    header = {
        "segments_per_track": 4,
        "tracks": 2,
        "bad_lsns": [seg * 32 + slot],
    }  # 0-based; the builder stores it 1-based
    sectors = [
        *segment_raw_sectors(build_header_segment(0, **header), ftk_per_side=4),
        *segment_raw_sectors(build_header_segment(1, **header), ftk_per_side=4),
        *segment_raw_sectors(
            build_volume_table_segment(2, [build_vtbl_entry(start_seg=3, end_seg=7)]),
            ftk_per_side=4,
        ),
    ]
    img = build_image([sectors], tmp_path / "t.twti", sources=[{"twrf": {}}])
    entry = TapeImage.open(tmp_path / "t.twti").entries[seg]
    assert img.entries[seg].state is SegmentState.MISSING
    assert entry.state is SegmentState.MISSING
    assert entry.excluded_mask == 1 << slot


def test_build_reports_merge_correct_and_write_stages(tmp_path, caplog):
    """build_image opens its three stage bars, fills each, and logs the summary."""
    import logging
    from contextlib import contextmanager

    bars: dict[str, list] = {}

    class _Task:
        def __init__(self, rec: list) -> None:
            self._rec = rec

        def advance(self, amount: float = 1) -> None:
            self._rec[1] += amount

        def update(self, completed: float) -> None:
            self._rec[1] = completed

    class _Progress:
        @contextmanager
        def task(self, description, total=None, unit=""):
            bars[description] = rec = [total, 0, unit]
            yield _Task(rec)

    header = {"segments_per_track": 4, "tracks": 2}
    sectors = [
        *segment_raw_sectors(build_header_segment(0, **header), ftk_per_side=4),
        *segment_raw_sectors(build_header_segment(1, **header), ftk_per_side=4),
    ]
    with caplog.at_level(logging.INFO, logger="qiclib.build"):
        build_image(
            [sectors, sectors], tmp_path / "t.twti", sources=[{"twrf": {}}], progress=_Progress()
        )
    assert bars == {
        "merging passes": [2, 2, "passes"],
        "correcting segments": [8, 8, "segments"],
        "writing image": [8, 8, "segments"],
    }
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert (
        "merge: 2 passes, 128 sectors -> 64 unique; later passes added 0, fixed 0 CRC-bad" in text
    )
    assert "header: segment 0 (clean)" in text
    assert "geometry: 2 tracks x 4 segs/track = 8 segments" in text
    assert "bad-sector map: 0 whole segments + 0 sectors" in text
    assert "segments: 2 clean, 0 corrected, 0 uncorrectable, 6 missing, 0 bad (map)" in text
    assert "wrote t.twti: 0.2 MB in " in text

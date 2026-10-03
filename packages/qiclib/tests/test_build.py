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

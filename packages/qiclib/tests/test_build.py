"""qiclib.build: sectors -> TWTI image."""

from __future__ import annotations

from tapewyrm_archive.twti import SegmentState, TapeImage

from qiclib.build import build_image
from qiclib.testing.builders import (
    build_header_segment,
    build_volume_table_segment,
    build_vtbl_entry,
    segment_raw_sectors,
    with_parity,
)


def test_missing_segment_carries_the_bad_sector_maps_exclusions(tmp_path):
    """A segment never read still has the map's excluded sectors recorded.

    The map describes the tape, not what was read: qiclib.extract sizes a lost
    segment's hole from this mask, and mask 0 would make it a full 29 KiB.
    """
    seg, slot = 5, 3
    header = {
        "segments_per_track": 4,
        "tracks": 2,
        "bad_lsns": [seg * 32 + slot],
    }  # 0-based; the builder stores it 1-based
    sectors = [
        *segment_raw_sectors(with_parity(build_header_segment(0, **header)), ftk_per_side=4),
        *segment_raw_sectors(with_parity(build_header_segment(1, **header)), ftk_per_side=4),
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
        *segment_raw_sectors(with_parity(build_header_segment(0, **header)), ftk_per_side=4),
        *segment_raw_sectors(with_parity(build_header_segment(1, **header)), ftk_per_side=4),
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
    assert "merge: 2 reads, 128 sectors -> 64 unique; later reads added 0, fixed 0 CRC-bad" in text
    assert "header: segment 0 (clean)" in text
    assert "geometry: 2 tracks x 4 segs/track = 8 segments" in text
    assert "bad-sector map: 0 whole segments + 0 sectors" in text
    assert "segments: 2 clean, 0 corrected, 0 uncorrectable, 6 missing, 0 bad (map)" in text
    assert "wrote t.twti: 0.2 MB in " in text


# ---------------------------------------------------------------------------
# The image's drive identity: which source capture speaks for the drive
# ---------------------------------------------------------------------------

# A TWRF v2 header whose drive answered none of the QIC-117 reports: every
# report key is present, every value is null (see docs/spec/twrf.md).
NULL_REPORTS = {
    "device_serial": "",
    "drive_status": None,
    "drive_config": None,
    "drive_rom": None,
    "drive_vendor_id": None,
    "tape_status": None,
    "rate_kbps": 500,
    "firmware_commit": "abc123",
}
REAL_REPORTS = {
    "device_serial": "GW-0001",
    "drive_status": 0x25,
    "drive_config": 0xD8,
    "drive_rom": 0x40,
    "drive_vendor_id": 4550,
    "tape_status": 0x63,
    "rate_kbps": 500,
    "firmware_commit": "abc123",
}
OTHER_DRIVE = {**REAL_REPORTS, "device_serial": "GW-0002", "drive_vendor_id": 71}


def _drive_of(tmp_path, *twrfs: dict) -> dict:
    """Build a two-segment image from sources carrying ``twrfs``; return its drive."""
    header = {"segments_per_track": 4, "tracks": 2}
    sectors = [
        *segment_raw_sectors(with_parity(build_header_segment(0, **header)), ftk_per_side=4),
        *segment_raw_sectors(with_parity(build_header_segment(1, **header)), ftk_per_side=4),
    ]
    sources = [{"twrf": dict(twrf)} for twrf in twrfs]
    build_image([sectors] * len(sources), tmp_path / "t.twti", sources=sources)
    return TapeImage.open(tmp_path / "t.twti").header["drive"]


def test_drive_skips_a_capture_whose_reports_are_all_null(tmp_path):
    """drive_config present but null is not a report; the later real one wins."""
    assert _drive_of(tmp_path, NULL_REPORTS, REAL_REPORTS) == REAL_REPORTS


def test_drive_disagreement_keeps_the_first_and_warns(tmp_path, caplog):
    """Captures from two drives: the first reporting one wins, with a warning."""
    import logging

    with caplog.at_level(logging.WARNING, logger="qiclib.build"):
        drive = _drive_of(tmp_path, NULL_REPORTS, REAL_REPORTS, OTHER_DRIVE)
    assert drive == REAL_REPORTS
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "device_serial" in warnings[0] and "drive_vendor_id" in warnings[0]
    assert "tape_status" not in warnings[0]


def test_drive_members_are_null_when_no_capture_reported(tmp_path):
    """No reporting source: all eight members are present and null."""
    drive = _drive_of(tmp_path, NULL_REPORTS, NULL_REPORTS)
    assert drive == dict.fromkeys(REAL_REPORTS)


def test_best_track_builds_from_the_winning_read_alone(tmp_path):
    """Two reads of track 0. A: header, its copy, the volume table and segment 3, all
    clean (128 sectors). B: the same but its header copy has 2 CRC-bad sectors and it
    read segment 4 instead of 3 (126 clean). By track, A wins and B is left out whole,
    so segment 4 is MISSING; the default union takes segment 4 from B."""
    from dataclasses import replace

    from qiclib.merge import Best
    from qiclib.testing.builders import make_segment_from_sectors

    header = {"segments_per_track": 4, "tracks": 2}

    def raw(seg):
        return segment_raw_sectors(seg, ftk_per_side=4)

    common = [
        *raw(build_header_segment(0, **header)),
        *raw(build_volume_table_segment(2, [build_vtbl_entry(start_seg=3, end_seg=7)])),
    ]
    copy = raw(build_header_segment(1, **header))
    a = [*common, *copy, *raw(make_segment_from_sectors(0, 3, 3, [b"a"] * 29))]
    b = [
        *common,
        *[replace(s, data_crc_ok=False) if i in (5, 6) else s for i, s in enumerate(copy)],
        *raw(make_segment_from_sectors(1, 0, 4, [b"b"] * 29)),
    ]
    sources = [{"file": "d/track-00.1.twrf", "twrf": {"track": 0}}] * 2
    by_track = build_image([a, b], tmp_path / "t.twti", sources=sources, best=Best.TRACK)
    assert by_track.entries[3].state is SegmentState.CLEAN
    assert by_track.entries[4].state is SegmentState.MISSING
    union = build_image([a, b], tmp_path / "u.twti", sources=sources)
    assert union.entries[4].state is SegmentState.CLEAN


def _two_damaged_reads():
    """Two reads of track 0 whose segment 3 has the same 5 sectors CRC-bad in both,
    garbled in different bytes each time (and no two sectors in the same columns)."""
    import random
    from dataclasses import replace

    from qiclib.testing.builders import make_segment_from_sectors

    rng = random.Random(3)
    header = {"segments_per_track": 4, "tracks": 2}

    def raw(seg):
        return segment_raw_sectors(with_parity(seg), ftk_per_side=4)

    common = [
        *raw(build_header_segment(0, **header)),
        *raw(build_header_segment(1, **header)),
        *raw(build_volume_table_segment(2, [build_vtbl_entry(start_seg=3, end_seg=7)])),
    ]
    rows = [bytes(rng.randrange(256) for _ in range(1024)) for _ in range(29)]
    truth = raw(make_segment_from_sectors(0, 3, 3, rows))

    def garbled(sec, lo, hi):
        data = bytearray(sec.data)
        for i in range(lo, hi):
            data[i] ^= rng.randrange(1, 256)
        return replace(sec, data=bytes(data), data_crc_ok=False)

    reads = []
    for r in range(2):
        seg3 = list(truth)
        for k, slot in enumerate((2, 7, 11, 19, 30)):
            seg3[slot] = garbled(truth[slot], 150 * k + 50 * r, 150 * k + 50 * r + 30)
        reads.append([*common, *seg3])
    sources = [
        {"file": f"d/track-00.{r + 1}.twrf", "twrf": {"track": 0, "pass_id": r + 1}}
        for r in range(2)
    ]
    return reads, sources, truth


def test_rs_columns_rescue_a_segment_no_read_could_and_the_log_says_so(tmp_path, caplog):
    import logging

    reads, sources, truth = _two_damaged_reads()
    with caplog.at_level(logging.INFO, logger="qiclib"):
        img = build_image(reads, tmp_path / "t.twti", sources=sources)
    assert img.entries[3].state is SegmentState.CLEAN
    assert img.segment(3)[: 29 * 1024] == b"".join(s.data for s in truth[:29])
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "track  0 segment   3 (#3): rescued by RS columns (5 sectors rebuilt), CLEAN" in text
    assert "track  0: 2 reads; 4 segments: 3 clean, 1 rescued" in text
    assert "track  0:   track-00.1.twrf: 128 sectors read, 123 CRC-clean, " in text
    assert "track  0:   RS columns: 5 sectors rebuilt" in text
    assert "track  1: not read (4 segments missing)" in text


def test_no_vote_leaves_that_segment_uncorrectable(tmp_path, caplog):
    import logging

    reads, sources, _ = _two_damaged_reads()
    with caplog.at_level(logging.INFO, logger="qiclib"):
        img = build_image(reads, tmp_path / "t.twti", sources=sources, vote=False)
    assert img.entries[3].state is SegmentState.UNCORRECTABLE
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "track  0 segment   3 (#3): UNCORRECTABLE; 5 erasures; " in text


def test_an_unwritten_segment_is_format_fill_not_damage(tmp_path, caplog):
    """A segment of one byte repeated, with parity never computed for it (as the
    format leaves space no backup wrote), fails the parity check; the log calls it
    unwritten, not a wrong CRC-good sector, and gives it no WARNING."""
    import logging

    from qiclib.testing.builders import make_segment_from_sectors

    header = {"segments_per_track": 4, "tracks": 2}
    sectors = [
        *segment_raw_sectors(with_parity(build_header_segment(0, **header)), ftk_per_side=4),
        *segment_raw_sectors(with_parity(build_header_segment(1, **header)), ftk_per_side=4),
        *segment_raw_sectors(
            make_segment_from_sectors(0, 3, 3, [b"\x6b" * 1024] * 29), ftk_per_side=4
        ),
    ]
    with caplog.at_level(logging.INFO, logger="qiclib"):
        build_image([sectors], tmp_path / "t.twti", sources=[{"twrf": {"track": 0}}])
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "parity: 1 unwritten (format fill)" in text
    assert "0 with spare parity that disagrees, 1 unwritten (format fill)" in text

"""`tw convert` reporting: stage progress bars, their totals, and the INFO narrative.

The decode loops report progress from an outer loop over slices (STYLE.md
§2.5: never per flux transition), so these tests also shrink the slice size
to a few items and check the output is identical to one big slice -- the
chunk boundaries must not change a single interval, bitcell or sector.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest
from qiclib.testing.builders import (
    build_header_segment,
    build_volume_table_segment,
    build_vtbl_entry,
    segment_raw_sectors,
)
from qiclib.types import RawSector
from tapewyrm_archive import twrf
from tapewyrm_archive.twrf import RawFluxCapture
from tapewyrm_archive.twti import SegmentState
from tapewyrm_archive.types import CaptureHeader, Direction, TapeFormat

from tapewyrm.codec import gwpll, gwstream, mfm
from tapewyrm.image import convert

# ---------------------------------------------------------------------------
# A Progress that remembers every task
# ---------------------------------------------------------------------------


@dataclass
class _Task:
    description: str
    total: float | None
    unit: str
    position: float = 0
    updates: int = 0
    closed: bool = False

    def advance(self, amount: float = 1) -> None:
        self.position += amount
        self.updates += 1

    def update(self, completed: float) -> None:
        self.position = completed
        self.updates += 1


class RecordingProgress:
    """:class:`tapewyrm_archive.progress.Progress` that records what was opened."""

    def __init__(self) -> None:
        self.tasks: list[_Task] = []

    @contextmanager
    def task(self, description: str, total: float | None = None, unit: str = "") -> Iterator[_Task]:
        rec = _Task(description, total, unit)
        self.tasks.append(rec)
        try:
            yield rec
        finally:
            rec.closed = True

    def named(self, description: str) -> list[_Task]:
        return [t for t in self.tasks if t.description == description]


# ---------------------------------------------------------------------------
# Synthetic flux: sectors -> MFM cells -> tick intervals -> GW stream -> TWRF
# ---------------------------------------------------------------------------

_SYNC_A1 = "0100010010001001"  # A1 with its missing clock (0x4489)
_TICKS_PER_CELL = 72  # 72 MHz sample clock, 1 us cells (500 kbit/s MFM)

HDR = CaptureHeader(
    rate_kbps=500,
    sample_clock_hz=72_000_000,
    track=0,
    direction=Direction.FORWARD,
    pass_id=1,
    utc="2026-10-03T00:00:00+00:00",
    tape_format=TapeFormat.QIC80,
    tape_status=0x22,  # QIC-80 on a 307.5 ft XL tape
)


def _mfm_cells(stream: bytes) -> str:
    """MFM-encode ``stream``, writing each ``A1 A1 A1`` as the missing-clock sync."""
    out: list[str] = []
    prev = 0
    i = 0
    while i < len(stream):
        if stream[i : i + 3] == b"\xa1\xa1\xa1":
            out.append(_SYNC_A1 * 3)
            prev = 1  # A1 ends in a 1 bit
            i += 3
            continue
        for k in range(7, -1, -1):
            bit = stream[i] >> k & 1
            out.append("1" if (not bit and not prev) else "0")
            out.append(str(bit))
            prev = bit
        i += 1
    return "".join(out)


def _gw_encode(ticks: int) -> bytes:
    """rdata_encode_flux() for one short transition (all MFM intervals fit)."""
    if ticks < 250:
        return bytes([ticks])
    return bytes([250 + (ticks - 250) // 255, 1 + (ticks - 250) % 255])


def _flux_for(sectors: list[RawSector]) -> tuple[bytes, list[int]]:
    """A GW stream (and its intervals) that reads back as ``sectors``."""
    stream = b"".join(
        mfm.build_sector_bytes(s.fsd, s.ftk, s.fsc, s.data, deleted=s.deleted) for s in sectors
    )
    cells = _mfm_cells(bytes(16) + stream + bytes(16))
    ones = [k for k, c in enumerate(cells) if c == "1"]
    intervals = [(b - a) * _TICKS_PER_CELL for a, b in zip([-1, *ones], ones, strict=False)]
    return b"".join(_gw_encode(t) for t in intervals) + b"\x00", intervals


def _tape_sectors() -> list[RawSector]:
    """Header, duplicate header and volume table of a tiny 2 x 4 segment tape."""
    header = {"segments_per_track": 4, "tracks": 2}
    return [
        *segment_raw_sectors(build_header_segment(0, **header), ftk_per_side=4),
        *segment_raw_sectors(build_header_segment(1, **header), ftk_per_side=4),
        *segment_raw_sectors(
            build_volume_table_segment(2, [build_vtbl_entry(start_seg=3, end_seg=7)]),
            ftk_per_side=4,
        ),
    ]


@pytest.fixture(scope="module")
def tape_flux() -> tuple[bytes, list[int], list[RawSector]]:
    sectors = _tape_sectors()
    blob, intervals = _flux_for(sectors)
    return blob, intervals, sectors


# ---------------------------------------------------------------------------
# Chunked hot loops: identical output, bars that reach their totals
# ---------------------------------------------------------------------------


def test_parse_in_tiny_slices_matches_one_slice_and_fills_the_bar(tape_flux, monkeypatch):
    """Slicing the byte loop must not change a single interval."""
    blob, intervals, _ = tape_flux
    whole = gwstream.parse(blob)
    monkeypatch.setattr(twrf, "_PROGRESS_EVERY", 7)  # gwstream.parse lives there
    progress = RecordingProgress()
    sliced = gwstream.parse(blob, progress=progress)
    assert sliced.intervals == whole.intervals == intervals
    assert sliced.terminated and sliced.data_bytes == whole.data_bytes
    (bar,) = progress.named("parsing flux stream")
    assert bar.unit == "bytes" and bar.total == len(blob) and bar.position == len(blob)
    assert bar.closed


def test_pll_in_tiny_slices_matches_one_slice_and_fills_the_bar(tape_flux, monkeypatch):
    """The PLL's clock and phase carry across slices exactly as across iterations."""
    _, intervals, _ = tape_flux
    clock = mfm.bitcell_seconds(500)
    whole = gwpll.flux_to_bitcells(intervals, 72_000_000, clock)
    monkeypatch.setattr(gwpll, "_PROGRESS_EVERY", 5)
    progress = RecordingProgress()
    sliced = gwpll.flux_to_bitcells(intervals, 72_000_000, clock, progress=progress)
    assert sliced == whole
    (bar,) = progress.named("PLL: flux -> bitcells")
    assert bar.unit == "transitions" and bar.position == bar.total == len(intervals)
    # One update per slice, not per transition.
    assert bar.updates == -(-len(intervals) // 5)


def test_pll_accepts_a_plain_iterator(tape_flux):
    """Callers may still pass any iterable of intervals."""
    _, intervals, _ = tape_flux
    clock = mfm.bitcell_seconds(500)
    assert gwpll.flux_to_bitcells(iter(intervals), 72_000_000, clock) == gwpll.flux_to_bitcells(
        intervals, 72_000_000, clock
    )


def test_scan_reports_bytes_and_stats(tape_flux, monkeypatch):
    """The sector scan's bar ends at the stream length; stats match the sectors."""
    _, intervals, sectors = tape_flux
    cells = gwpll.flux_to_bitcells(intervals, 72_000_000, mfm.bitcell_seconds(500))
    decoded, runs = mfm.frame_bitcells(cells)
    assert runs == 2 * len(sectors)  # an ID and a data field per sector
    monkeypatch.setattr(mfm, "_PROGRESS_EVERY", 100)
    progress, stats = RecordingProgress(), mfm.ScanStats()
    found = list(mfm.recover_sectors_from_bytes(decoded, progress=progress, stats=stats))
    assert [s.data for s in found] == [s.data for s in sectors]
    assert stats.sectors == stats.data_crc_ok == len(sectors)
    assert stats.data_bad == stats.id_only == 0
    (bar,) = progress.named("scanning for sectors")
    assert bar.position == bar.total == len(decoded) and bar.updates > 1


def test_scan_counts_an_id_without_its_data_field():
    """An ID followed by another ID (no data between) is tallied as ID-only."""
    first = mfm.build_sector_bytes(0, 0, 1, bytes(1024))
    id_only = first[: first.index(b"\xa1\xa1\xa1\xfb")]  # keep only its ID field
    stats = mfm.ScanStats()
    found = list(mfm.recover_sectors_from_bytes(id_only + first, stats=stats))
    assert len(found) == 1 and stats.id_only == 1


# ---------------------------------------------------------------------------
# decode_capture / convert: stage bars and the INFO narrative
# ---------------------------------------------------------------------------


def test_decode_capture_opens_each_stage_once_and_reaches_its_total(tmp_path, tape_flux, caplog):
    blob, _, sectors = tape_flux
    path = tmp_path / "track-00.twrf"
    RawFluxCapture(header=HDR, flux=blob).save(path)
    progress = RecordingProgress()
    with caplog.at_level(logging.INFO, logger="tapewyrm.image.convert"):
        found, meta = convert.decode_capture(path, progress=progress)

    assert [s.data for s in found] == [s.data for s in sectors]
    assert meta["sectors"] == len(sectors)
    assert [t.description for t in progress.tasks] == [
        "parsing flux stream",
        "PLL: flux -> bitcells",
        "framing MFM bytes",
        "scanning for sectors",
    ]
    for bar in progress.tasks:
        assert bar.closed and bar.total and bar.position == bar.total, bar

    lines = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert all(line.startswith("track-00.twrf: ") for line in lines)
    assert "track 0 forward, QIC-80 on 307.5 ft 550 Oe (XL), 500 kbps" in lines[0]
    assert "transitions" in lines[1]
    assert "bitcells" in lines[2] and f"{2 * len(sectors)} sync marks" in lines[2]
    assert f"{len(sectors)} sectors: {len(sectors)} CRC-clean, 0 data-CRC bad" in lines[3]


def test_convert_runs_one_outer_bar_per_capture_then_builds(tmp_path, tape_flux, caplog):
    """Two passes: outer bars "capture k/2 <name>", then merge/correct/write."""
    blob, _, sectors = tape_flux
    dump = tmp_path / "dump"
    dump.mkdir()
    for n in (0, 1):
        RawFluxCapture(header=HDR, flux=blob).save(dump / f"track-0{n}.twrf")
    progress = RecordingProgress()
    with caplog.at_level(logging.INFO):
        img = convert.convert([dump], tmp_path / "t.twti", progress=progress)

    outer = [t for t in progress.tasks if t.description.startswith("capture ")]
    assert [t.description for t in outer] == [
        "capture 1/2 track-00.twrf",
        "capture 2/2 track-01.twrf",
    ]
    assert [(t.total, t.position) for t in outer] == [(2, 1), (2, 2)]
    for name in ("merging passes", "correcting segments", "writing image"):
        (bar,) = progress.named(name)
        assert bar.closed and bar.position == bar.total, bar
    assert progress.named("writing image")[0].total == 8  # 2 tracks x 4 segments
    assert img.entries[0].state is SegmentState.CLEAN

    text = "\n".join(r.getMessage() for r in caplog.records if r.levelno == logging.INFO)
    assert f"merge: 2 passes, {2 * len(sectors)} sectors -> {len(sectors)} unique" in text
    assert "geometry: 2 tracks x 4 segs/track = 8 segments" in text
    assert "cartridge: " in text
    assert "segments: 3 clean, 0 corrected, 0 uncorrectable, 5 missing, 0 bad (map)" in text
    assert "converted 2 capture(s) in " in text


def test_mb_formats_decimal_megabytes():
    assert convert._mb(43_486_571) == "43.5 MB"
    assert convert._mb(1_750_000_000) == "1,750.0 MB"


def test_describe_header_without_a_tape_status_uses_the_header_format():
    hdr = CaptureHeader(
        rate_kbps=500,
        sample_clock_hz=72_000_000,
        track=3,
        direction=Direction.REVERSE,
        pass_id=1,
        utc="",
    )
    assert convert.describe_header(hdr) == "track 3 reverse, QIC-80"


def test_legacy_stream_still_converts(tmp_path, tape_flux):
    """Headerless .raw captures decode at the legacy rate with the same stages."""
    blob, _, sectors = tape_flux
    path = tmp_path / "track-00.raw"
    path.write_bytes(blob)
    found, meta = convert.decode_capture(Path(path))
    assert len(found) == len(sectors) and meta["twrf"]["rate_kbps"] == 500

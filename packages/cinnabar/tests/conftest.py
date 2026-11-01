"""Synthetic tapes for cinnabar's tests: a TWTI image and TWRF captures, invented data only.

The image is three segments of a QIC-80 tape built with
``qiclib.testing.builders``: the header segment (format parameter record),
a volume-table segment and one Basic-DOS volume segment. The QIC-80
capture is the same three segments as MFM flux (``mfm_cells`` encodes
bytes the way the tape would, A1 syncs with their missing clock), with an
INDEX pulse before each; the QIC-100 capture is two frames of GCR records
from :mod:`qiclib.qic100`. Both are written as real TWRF files, so the
loaders see exactly what ``tw dump`` would have written.
"""

from __future__ import annotations

import struct
from pathlib import Path

import pytest
from qiclib import qic100
from qiclib import volume as volume_mod
from qiclib.build import build_image
from qiclib.testing.builders import (
    build_data_entry,
    build_dir_entry,
    build_header_segment,
    build_volume_table_segment,
    build_vtbl_entry,
    make_segment_from_sectors,
    segment_raw_sectors,
)
from tapewyrm.codec import mfm
from tapewyrm_archive import twrf
from tapewyrm_archive.types import CaptureHeader, Direction, MarkerKind, TapeFormat

SAMPLE_HZ = 72_000_000
RATE_KBPS = 500
TICKS_PER_CELL = SAMPLE_HZ // (RATE_KBPS * 2000)  # 72 ticks = 1 us MFM bitcell
GAP = 0x4E


def basic_volume_stream() -> bytes:
    """A tiny Basic-DOS, directory-first volume: one directory, two files."""
    read, write, subdir, last, end = 0x01, 0x02, 0x20, 0x40, 0x80
    hello, world = b"hello tape\n", b"world\n"
    table = (
        build_dir_entry("DOCS", attrs=read | write | subdir, data_entry_size=0)
        + build_dir_entry("HELLO.TXT", attrs=read | write, data_entry_size=4 + 20 + 1 + len(hello))
        + build_dir_entry(
            "WORLD.TXT", attrs=read | write | last, data_entry_size=4 + 20 + 1 + len(world)
        )
        + build_dir_entry("INNER.TXT", attrs=read | last | end, data_entry_size=0)
    )
    data = build_data_entry(build_dir_entry("HELLO.TXT", attrs=read | write), "", hello)
    data += build_data_entry(build_dir_entry("WORLD.TXT", attrs=read | write | last), "", world)
    return table + data


def tape_segments():
    """(header, vtbl, volume) segments of the synthetic tape, as qiclib Segments."""
    header = build_header_segment(0)
    vol_info, _bsm = volume_mod.parse_header(header)
    vtbl_at = vol_info.first_data_seg or 1
    entry = build_vtbl_entry(start_seg=vtbl_at + 1, end_seg=vtbl_at + 1, description="TEST BACKUP")
    vtbl = build_volume_table_segment(vtbl_at, [entry])
    stream = basic_volume_stream()
    chunks = [stream[i : i + 1024] for i in range(0, len(stream), 1024)]
    volume = make_segment_from_sectors(0, vtbl_at + 1, vtbl_at + 1, chunks)
    return header, vtbl, volume


@pytest.fixture(scope="session")
def image_path(tmp_path_factory) -> Path:
    header, vtbl, volume = tape_segments()
    sectors = [s for seg in (header, vtbl, volume) for s in segment_raw_sectors(seg)]
    out = tmp_path_factory.mktemp("image") / "synthetic.twti"
    hdr = capture_header(0)
    build_image([sectors], out, sources=[{"file": "synthetic", "twrf": twrf.header_to_dict(hdr)}])
    return out


# ---------------------------------------------------------------------------
# Flux
# ---------------------------------------------------------------------------


def capture_header(track: int) -> CaptureHeader:
    return CaptureHeader(
        rate_kbps=RATE_KBPS,
        sample_clock_hz=SAMPLE_HZ,
        track=track,
        direction=Direction.FORWARD if track % 2 == 0 else Direction.REVERSE,
        pass_id=1,
        utc="2026-01-02T03:04:05+00:00",
        tape_format=TapeFormat.QIC80,
        device_serial="TEST",
    )


def mfm_cells(data: bytes) -> bytearray:
    """MFM-encode bytes: clock bit between data bits unless either neighbour is 1.

    An A1 byte is the sync mark with its missing clock
    (``mfm._SYNC_A1_CELLS``), as on tape.
    """
    cells = bytearray()
    prev = 0
    for byte in data:
        if byte == mfm.A1:
            cells.extend(c - ord("0") for c in mfm._SYNC_A1_CELLS)  # b"0100..." -> 0/1
            prev = 1
            continue
        for i in range(7, -1, -1):
            bit = (byte >> i) & 1
            cells.append(0 if (prev or bit) else 1)
            cells.append(bit)
            prev = bit
    return cells


def cells_to_intervals(cells, ticks_per_cell: int) -> list[int]:
    return qic100.cells_to_intervals(cells, ticks_per_cell)


def _n28(value: int) -> bytes:
    return bytes((((value >> (7 * i)) & 0x7F) << 1) | 1 for i in range(4))


def write_capture(path: Path, track: int, segments: list[list[int]], *, lead_in: list[int]) -> Path:
    """A TWRF file: SESSION_START, lead-in intervals, then INDEX + intervals per segment."""
    body = bytearray()
    body += twrf.frame_marker(
        MarkerKind.SESSION_START, struct.pack("<HIHBH", RATE_KBPS, SAMPLE_HZ, track, 0, 1)
    )
    for x in lead_in:
        body += twrf.encode_interval(x)
    for k, intervals in enumerate(segments):
        body += twrf.encode_index(1)
        body += twrf.frame_marker(MarkerKind.SEGMENT, struct.pack("<II", 1, k + 1))
        for x in intervals:
            body += twrf.encode_interval(x)
    body += b"\x00"
    cap = twrf.RawFluxCapture(header=capture_header(track), flux=bytes(body))
    cap.save(path)
    return path


def qic80_segment_cells(seg) -> bytearray:
    """One segment as it sits on tape: 32 sectors with gaps, from a qiclib Segment."""
    cells = bytearray()
    for sec in segment_raw_sectors(seg):
        # build_sector_bytes already carries the sync zeros and gaps of the
        # ID and data fields; a gap byte run goes between sectors.
        cells += mfm_cells(
            bytes([GAP] * 24) + mfm.build_sector_bytes(sec.fsd, sec.ftk, sec.fsc, sec.data)
        )
    cells += mfm_cells(bytes([GAP] * 64))
    return cells


@pytest.fixture(scope="session")
def qic80_capture(tmp_path_factory) -> Path:
    header, vtbl, volume = tape_segments()
    # 60 ms of gap so the first INDEX is past the 50 ms cue window.
    lead = cells_to_intervals(mfm_cells(bytes([GAP] * 4000)), TICKS_PER_CELL)
    segs = [
        cells_to_intervals(qic80_segment_cells(s), TICKS_PER_CELL) for s in (header, vtbl, volume)
    ]
    return write_capture(tmp_path_factory.mktemp("flux") / "track-00.twrf", 0, segs, lead_in=lead)


@pytest.fixture(scope="session")
def qic100_capture(tmp_path_factory) -> Path:
    """Two frames (ID record + data record each) of GCR, no INDEX pulses."""
    ticks = 150  # a made-up GCR bitcell: 2.08 us at 72 MHz
    cells = bytearray()
    for frame in (0, 1):
        id_raw = qic100.build_id_field(qic100.RecordType.ID, track=0, frame=frame)
        cells += qic100.encode_record(qic100.RecordType.ID, None, id_raw)
        cells += bytes([0, 1, 0, 1]) * 40
        data = (b"frame %d " % frame).ljust(qic100.DATA_FIELD_BYTES, b"\x5a")
        id_raw = qic100.build_id_field(qic100.RecordType.DATA, track=0, frame=frame)
        cells += qic100.encode_record(qic100.RecordType.DATA, data, id_raw)
        cells += bytes([0, 1, 0, 1]) * 100
    intervals = cells_to_intervals(cells, ticks)
    return write_capture(tmp_path_factory.mktemp("gcr") / "track-00.twrf", 0, [], lead_in=intervals)

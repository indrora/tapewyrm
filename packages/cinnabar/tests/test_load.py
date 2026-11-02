"""cinnabar.load: the image and flux loaders on the synthetic tapes, and ``--dump``."""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner
from tapewyrm_archive.progress import NULL_PROGRESS

from cinnabar.cli import cli, dump_track
from cinnabar.load import open_path
from cinnabar.model import SEGMENT_BYTES, AxisKind, level, rows_of


def test_image_tracks_segments_and_holds(image_path: Path):
    tape = open_path(image_path)
    assert tape.kind == "TWTI" and tape.track_numbers[0] == 0
    track = tape.load(0, NULL_PROGRESS)
    assert track.axis.kind is AxisKind.BYTES and track.physical == "QIC-80"
    assert track.segments[0].start == 0 and track.segments[1].start == SEGMENT_BYTES
    holds = {dict(s.detail)["holds"] for s in track.segments if s.state != "MISSING"}
    assert {"Header", "Volume Table", "TEST BACKUP"} <= holds
    header = track.segments[0]
    assert [s.label for s in header.fields] == ["Format Parameter Record", "Bad Sector Map"]
    vtbl = next(s for s in track.segments if dict(s.detail)["holds"] == "Volume Table")
    assert vtbl.fields[0].label == "Volume Table"
    assert vtbl.fields[0].start == vtbl.start
    volume = next(s for s in track.segments if dict(s.detail)["holds"] == "TEST BACKUP")
    assert [s.label for s in volume.fields] == ["Directory Section", "Data Section"]
    names = [s.label for s in level(volume.fields, 1)]
    assert "HELLO.TXT" in names and "DOCS" in names
    assert volume.byte_runs[0].data[:1] != b""
    assert track.overview[0] == "clean"
    missing = [s for s in track.segments if s.state == "MISSING"]
    assert missing and missing[0].records[0].style == "slot-missing"


def test_dump_lists_every_lane(image_path: Path):
    track = open_path(image_path).load(0)
    text = dump_track(track, segment=0)
    assert "Track 0: bytes axis" in text
    assert "TWTI: Sectors" in text and "Format Parameter Record" in text
    assert "QIC-80: Header fields" in text and "tape name" in text
    result = CliRunner().invoke(cli, ["--dump", "--segment", "0", str(image_path)])
    assert result.exit_code == 0, result.output
    assert "Segment 0" in result.output


def test_flux_capture_decodes_sectors_in_time(qic80_capture: Path):
    tape = open_path(qic80_capture)
    assert tape.kind == "TWRF"
    track = tape.load(0, NULL_PROGRESS)
    assert track.axis.kind is AxisKind.TIME and track.physical == "QIC-80"
    assert track.decoder is not None
    assert track.segments[1].state == "decoding"  # placeholders until the workers land
    assert track.segments[1].records[0].style == "pending"
    track.decoder.wait()
    titles = [s.title for s in track.segments]
    assert titles == ["Lead-in", "Segment 0", "Segment 1", "Segment 2"]
    for seg in track.segments[1:]:
        assert seg.state == "clean", (seg.title, seg.detail)
        assert len(seg.records) == 32
        assert all(r.style == "record" for r in seg.records)
        assert seg.start <= seg.records[0].start < seg.records[-1].end <= seg.end
        fields = [c.label for c in seg.records[0].children]
        assert fields == ["sync", "FE", "ID", "crc", "gap", "sync", "FB", "data", "crc"]
    # The tape got identified from the sectors, so the logical lanes filled.
    holds = [dict(s.detail)["holds"] for s in track.segments[1:]]
    assert holds == ["Header", "Volume Table", "TEST BACKUP"]
    header = track.segments[1]
    assert header.fields[0].label == "Format Parameter Record"
    assert header.byte_runs[0].start >= header.start
    volume = track.segments[3]
    assert [s.label for s in volume.fields] == ["Directory Section", "Data Section"]
    assert track.overview[1:] == ["clean", "clean", "clean"]
    assert track.flux is not None and track.flux.cell_count_between(track.start, track.end) > 0
    text = dump_track(track, segment=2)
    assert "flux:" in text and "Volume Table" in text
    track.decoder.close()


def test_flux_capture_of_gcr_records(qic100_capture: Path):
    tape = open_path(qic100_capture)
    track = tape.load(0, NULL_PROGRESS)
    assert track.physical == "QIC-100-MC"
    track.decoder.wait()
    records = [r for seg in track.segments for r in seg.records]
    assert [r.label for r in records] == ["Id Record", "Data Record"] * 2
    assert [r.sublabel for r in records] == ["frame 0", "frame 0", "frame 1", "frame 1"]
    pieces = [c.label for c in records[1].children]
    assert pieces == ["Preamble", "SOD", "F0", "system", "user data", "ID", "CRCC", "Postamble"]
    data_seg = next(s for s in track.segments if s.byte_runs)
    assert data_seg.byte_runs[0].data.startswith(b"frame 0 ")
    track.decoder.close()


def test_opening_one_capture_takes_its_dump_sidecar(qic80_capture: Path, tmp_path: Path):
    import json
    import shutil

    from cinnabar.load import sidecar_captures

    folder = tmp_path / "dump"
    folder.mkdir()
    t0 = folder / "track-00.twrf"
    t1 = folder / "track-01.twrf"
    shutil.copy(qic80_capture, t0)
    shutil.copy(qic80_capture, t1)
    # Paths as tw dump writes them: relative to wherever it ran, plus one file that is gone.
    lines = [
        {"track": 1, "path": "../captures/dump/track-01.twrf"},
        {"track": 0, "path": "captures/dump/track-00.twrf"},
        {"track": 2, "path": "captures/dump/track-02.twrf"},
    ]
    (folder / "dump.jsonl").write_text("\n".join(json.dumps(e) for e in lines) + "\n")
    assert sidecar_captures(t0) == [t1, t0]
    tape = open_path(t0)
    # Both files carry track 0 in their header; the second becomes a numbered pass.
    assert len(tape.track_numbers) == 2
    assert sidecar_captures(qic80_capture) == [qic80_capture]  # no sidecar there


def _compressed_image(tmp_path: Path) -> Path:
    """An image whose volume is QIC-122 extents of *raw* frames (no compressor needed)."""
    import struct

    from qiclib import volume as volume_mod
    from qiclib.build import build_image
    from qiclib.testing.builders import (
        build_header_segment,
        build_volume_table_segment,
        build_vtbl_entry,
        make_segment_from_sectors,
        segment_raw_sectors,
    )
    from tapewyrm_archive import twrf

    from tests.conftest import basic_volume_stream, capture_header

    stream = basic_volume_stream()
    header = build_header_segment(0)
    vtbl_at = volume_mod.parse_header(header)[0].first_data_seg or 1
    # Two extents: the stream split in half, each a raw frame at its offset.
    half = len(stream) // 2
    segs = []
    for k, (off, chunk) in enumerate(((0, stream[:half]), (half, stream[half:]))):
        extent = struct.pack("<Q", off) + struct.pack("<H", 0x8000 | len(chunk)) + chunk
        segs.append(
            make_segment_from_sectors(
                0,
                vtbl_at + 1 + k,
                vtbl_at + 1 + k,
                [extent[i : i + 1024] for i in range(0, len(extent), 1024)],
            )
        )
    entry = bytearray(
        build_vtbl_entry(
            start_seg=vtbl_at + 1, end_seg=vtbl_at + 2, description="PACKED", compressed=True
        )
    )
    entry[96:104] = len(stream).to_bytes(8, "little")  # data section size: the uncompressed length
    vtbl = build_volume_table_segment(vtbl_at, [bytes(entry)])
    sectors = [s for seg in (header, vtbl, *segs) for s in segment_raw_sectors(seg)]
    tmp_path.mkdir(parents=True, exist_ok=True)
    out = tmp_path / "packed.twti"
    build_image(
        [sectors],
        out,
        sources=[{"file": "synthetic", "twrf": twrf.header_to_dict(capture_header(0))}],
    )
    return out


def test_compressed_volume_decompresses_into_files(tmp_path: Path):
    from cinnabar.model import VOLUME_BASE

    tape = open_path(_compressed_image(tmp_path))
    assert VOLUME_BASE in tape.track_numbers
    assert tape.label(VOLUME_BASE) == "Volume 0: PACKED"
    track = tape.load(VOLUME_BASE, NULL_PROGRESS)
    assert track.physical == "Volume" and track.axis.kind is AxisKind.BYTES
    assert [s.title for s in track.segments][:2] and len(track.segments) == 2
    assert track.segments[0].records[0].row == "Volume: Source segment"
    rows = rows_of([s for seg in track.segments for s in seg.fields])
    assert "QIC-113: Sections" in rows and "QIC-113: Entries" in rows
    names = [s.label for seg in track.segments for s in level(seg.fields, 1)]
    assert "HELLO.TXT" in names and "DOCS" in names
    hello = next(
        s
        for seg in track.segments
        for s in level(seg.fields, 1)
        if s.label == "HELLO.TXT" and s.style == "data-entry"
    )
    data = next(c for c in hello.children if c.label == "data")
    pieces = [p for seg in track.segments for p in seg.bytes_between(data.start, data.end)]
    assert b"".join(p.data for p in pieces) == b"hello tape\n"
    text = dump_track(track, segment=1)
    assert "Volume: Source segment" in text
    result = CliRunner().invoke(
        cli, ["--dump", "--volume", "0", "--segment", "0", str(_compressed_image(tmp_path / "b"))]
    )
    assert result.exit_code == 0, result.output
    assert "Directory Section" in result.output

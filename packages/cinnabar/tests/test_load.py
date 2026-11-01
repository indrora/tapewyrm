"""cinnabar.load: the image and flux loaders on the synthetic tapes, and ``--dump``."""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from cinnabar.cli import cli, dump_track
from cinnabar.load import open_path
from cinnabar.model import SEGMENT_BYTES, AxisKind, level


def test_image_tracks_segments_and_holds(image_path: Path):
    tape = open_path(image_path)
    assert tape.kind == "TWTI" and tape.track_numbers[0] == 0
    track = tape.load(0)
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
    assert "QIC-80 Records" in text and "Format Parameter Record" in text
    assert "Fields 1" in text and "tape name" in text
    result = CliRunner().invoke(cli, ["--dump", "--segment", "0", str(image_path)])
    assert result.exit_code == 0, result.output
    assert "Segment 0" in result.output


def test_flux_capture_decodes_sectors_in_time(qic80_capture: Path):
    tape = open_path(qic80_capture)
    assert tape.kind == "TWRF"
    track = tape.load(0)
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
    track = tape.load(0)
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

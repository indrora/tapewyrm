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


def test_image_rows_carry_the_profiles_verdict(image_path: Path):
    """The tape's rows say which profile read the table, its parser and its warnings, as identify does."""
    tape = open_path(image_path)
    rows = dict(tape.detail)
    assert rows["volume profile"] == "qic80-rev-n (spec)"
    assert rows["profile score"].endswith("(fingerprint held)")
    assert rows["file-set parser"] == "qic113-basic"
    assert rows["warnings"] == "generic" and tape.warnings == ("generic",)
    track = tape.load(0, NULL_PROGRESS)
    vtbl = next(s for s in track.segments if dict(s.detail)["holds"] == "Volume Table")
    entry = vtbl.fields[0].children[0]
    assert dict(entry.detail)["file-set parser"] == "qic113-basic"
    assert "extended OS" not in dict(entry.detail)
    # Past byte 56 the fields are the profile's: Rev N's offsets, under the profile's names.
    labels = [c.label for c in entry.children if c.start >= vtbl.start + 57]
    assert labels == ["seq", "dir size", "data size", "label", "cmp", "os"]


def test_extended_image_is_laid_out_by_the_extended_parser(extended_image_path: Path):
    """A QIC-113 record picks qic113-rev-g, whose parser draws Data then Directory: no is_extended_os."""
    tape = open_path(extended_image_path)
    rows = dict(tape.detail)
    assert rows["volume profile"] == "qic113-rev-g (spec)"
    assert rows["file-set parser"] == "qic113-extended"
    assert tape.warnings == ()
    track = tape.load(0, NULL_PROGRESS)
    volume = next(s for s in track.segments if dict(s.detail)["holds"] == "EXT BACKUP")
    assert [s.label for s in volume.fields] == ["Data Section", "Directory Section"]
    names = [s.label for s in level(volume.fields, 1)]
    assert "HELLO.TXT" in names and "DOCS" in names
    vtbl = next(s for s in track.segments if dict(s.detail)["holds"] == "Volume Table")
    entry = vtbl.fields[0].children[0]
    match = next(c for c in entry.children if c.label == "match")
    assert match.style == "sig" and dict(match.detail)["value"] == "71 00"
    result = CliRunner().invoke(cli, ["--dump", "--segment", "0", str(extended_image_path)])
    assert result.exit_code == 0, result.output
    assert "file-set parser: qic113-extended" in result.output


def test_a_profile_without_a_parser_leaves_the_volume_unparsed(image_path: Path):
    """Parser ``none``: the volume's lane says so and the rows carry the warnings, instead of a guess."""
    from qiclib import identify
    from qiclib.volume import profile as vp
    from tapewyrm_archive.twti import TapeImage

    from cinnabar.load.volumes import Layout
    from cinnabar.model import ByteMap

    img = TapeImage.open(image_path)
    info = identify.from_image(img)
    mystery = vp.VolumeProfile(name="mystery", status="untested", vendor_bit=False)
    info.verdicts = [vp.evaluate(info.vtbl_records, info.vol, mystery)]
    layout = Layout(info, img.segment)
    assert layout.reading.parser == "none"
    assert layout.warnings == ("generic", "untested")
    rows = dict(layout.summary())
    assert rows["file-set parser"] == "none" and rows["warnings"] == "generic, untested"
    seg = layout.volumes[0].entry.start_seg
    bmap = ByteMap.identity(0.0, SEGMENT_BYTES)
    (note,) = layout.fields(seg, img.segment(seg), bmap)
    assert note.label == "No file-set parser" and note.style == "no-parser"
    assert info.vtbl_seg is not None
    (table,) = layout.fields(info.vtbl_seg, img.segment(info.vtbl_seg), bmap)
    entry_rows = dict(table.children[0].detail)
    assert entry_rows["volume profile"] == "mystery (untested)"
    assert entry_rows["file-set parser"] == "none"
    past = [c for c in table.children[0].children if c.start > 56]
    assert [c.label for c in past] == ["vendor"]  # the profile places nothing past byte 56


def test_overrides_reach_the_tapes_rows_and_dump(image_path: Path):
    """--fileset-parser & co.: the reading names the tuned profile, its parser and the manual warning."""
    from qiclib.volume import profile as vp

    tape = open_path(
        image_path, volume_profile="qic80-rev-n", overrides=vp.Overrides(fileset_parser="none")
    )
    rows = dict(tape.detail)
    assert rows["volume profile"] == "qic80-rev-n+overrides (spec)"
    assert rows["file-set parser"] == "none"
    assert tape.warnings == ("generic", "manual")
    track = tape.load(0, NULL_PROGRESS)
    volume = next(s for s in track.segments if dict(s.detail)["holds"] == "TEST BACKUP")
    (note,) = volume.fields
    assert note.label == "No file-set parser"  # laid out as given, not as the profile says
    flags = ["--fileset-parser", "none", "--vtbl-field", "source_label=102,16"]
    result = CliRunner().invoke(cli, ["--dump", "--segment", "0", *flags, str(image_path)])
    assert result.exit_code == 0, result.output
    assert "volume profile: qic80-rev-n+overrides (spec)" in result.output
    assert "file-set parser: none" in result.output
    assert "warnings: generic, manual" in result.output
    bad = CliRunner().invoke(cli, ["--dump", "--vtbl-field", "label=1,2", str(image_path)])
    assert bad.exit_code == 2 and "unknown [vtbl] field" in bad.output
    missing = CliRunner().invoke(cli, ["--dump", "--volume-profile", "nope", str(image_path)])
    assert missing.exit_code == 1 and "no volume profile 'nope'" in missing.output


def test_dump_lists_every_lane(image_path: Path):
    track = open_path(image_path).load(0)
    text = dump_track(track, segment=0)
    assert "Track 0: bytes axis" in text
    assert "TWTI: Sectors" in text and "Format Parameter Record" in text
    assert "QIC-80: Header fields" in text and "tape name" in text
    result = CliRunner().invoke(cli, ["--dump", "--segment", "0", str(image_path)])
    assert result.exit_code == 0, result.output
    assert "Segment 0" in result.output
    assert "geometry: " in result.output and "volume table: " in result.output


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
        assert [r.style for r in seg.records] == ["record"] * 29 + ["ecc"] * 3
        assert seg.start <= seg.records[0].start < seg.records[-1].end <= seg.end
        fields = [c.label for c in seg.records[0].children]
        assert fields == ["sync", "FE", "ID", "crc", "gap", "sync", "FB", "data", "crc"]
    # The tape got identified from the sectors, so the logical lanes filled.
    holds = [dict(s.detail)["holds"] for s in track.segments[1:]]
    assert holds == ["Header", "Volume Table", "TEST BACKUP"]
    header = track.segments[1]
    assert header.fields[0].label == "Format Parameter Record"
    assert header.byte_runs[0].start >= header.start
    # The three parity sectors: labelled ECC, bytes shown past the data, not in it.
    assert [r.sublabel for r in header.records[-3:]] == ["ECC"] * 3
    assert len(header.byte_runs) == 32 and header.byte_runs[-1].offset == 31 * 1024
    assert header.fields[1].end <= header.byte_runs[29].start  # the BSM ends before the parity
    volume = track.segments[3]
    assert [s.label for s in volume.fields] == ["Directory Section", "Data Section"]
    assert track.overview[1:] == ["clean", "clean", "clean"]
    assert track.flux is not None and track.flux.cell_count_between(track.start, track.end) > 0
    # The identification is on the tape's rows (and the header's segment count on the track's).
    rows = dict(tape.detail)
    assert rows["tape name"] and rows["volumes"] == "1" and rows["volume table"].endswith("(clean)")
    assert rows["geometry"].endswith(" segments")
    assert dict(track.detail)["segments per track (header)"] == rows["geometry"].split()[3]
    text = dump_track(track, segment=2)
    assert "flux:" in text and "Volume Table" in text
    assert "segments per track (header)" in text
    track.decoder.close()


def test_header_read_from_the_duplicate_still_labels_the_first_copy():
    """Segments land in any order: identified from the duplicate, segment 0 is still the header."""
    from qiclib.testing.builders import build_header_segment, segment_raw_sectors

    from cinnabar.load.flux import _Identification
    from tests.conftest import capture_header

    hdr = capture_header(0)
    first, dup = (
        segment_raw_sectors(build_header_segment(0)),
        segment_raw_sectors(build_header_segment(1)),
    )
    ident = _Identification()
    assert ident.attempt(dup, hdr, final=False, fresh=dup)
    layout = ident.layout
    assert layout is not None and layout.info.header_seg == 1
    assert layout.holds(0) == "Header" and layout.holds(1) == "Header"
    assert 0 in ident.wanted  # the first copy is the one to read, once it lands
    assert ident.attempt([*dup, *first], hdr, final=False, fresh=first)
    assert ident.layout is not None and ident.layout.info.header_seg == 0
    assert 0 not in ident.wanted


def test_identification_fires_on_the_header_and_again_for_the_volume_table():
    """The tape is identified the moment its header lands, and completed when the volume table does."""
    from qiclib.testing.builders import segment_raw_sectors

    from cinnabar.load.flux import _Identification
    from tests.conftest import capture_header, tape_segments

    header, vtbl, volume = tape_segments()
    hdr = capture_header(0)
    h, t, v = (segment_raw_sectors(s) for s in (header, vtbl, volume))
    ident = _Identification()
    # A data segment on its own is no reason to try: it cannot be the header.
    assert not ident.attempt(v, hdr, final=False, fresh=v)
    assert ident.layout is None
    # The header: identified at once, with no volumes yet and the table wanted.
    assert ident.attempt([*v, *h], hdr, final=False, fresh=h)
    assert ident.layout is not None and ident.layout.entries == []
    assert ident.wanted == {vtbl.seg}
    # Another data segment changes nothing; the table's landing completes it.
    assert not ident.attempt([*v, *h], hdr, final=False, fresh=v)
    assert ident.attempt([*v, *h, *t], hdr, final=False, fresh=t)
    assert [e.description.strip() for e in ident.layout.entries] == ["TEST BACKUP"]
    assert ident.wanted == set()
    assert not ident.attempt([*v, *h, *t], hdr, final=True)


def test_flux_identification_takes_the_volume_read_flags():
    """A flux tape's identification carries --volume-profile and the overrides into every attempt."""
    from qiclib.testing.builders import segment_raw_sectors
    from qiclib.volume import profile as vp

    from cinnabar.load.flux import _Identification
    from tests.conftest import capture_header, tape_segments

    sectors = [s for seg in tape_segments() for s in segment_raw_sectors(seg)]
    ident = _Identification("qic80-rev-n", vp.Overrides(fileset_parser="none"))
    assert ident.attempt(sectors, capture_header(0), final=True)
    assert ident.layout is not None
    assert ident.layout.reading.profile == "qic80-rev-n+overrides"
    assert ident.layout.reading.parser == "none" and "manual" in ident.layout.warnings
    # A parameter the parser does not take is the flags' fault: warned, not identified.
    bad = _Identification(overrides=vp.Overrides(fileset_params={"depth": 3}))
    assert not bad.attempt(sectors, capture_header(0), final=True)
    assert bad.layout is None


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

    from qiclib import header as qiclib_header
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
    vtbl_at = qiclib_header.parse_header(header)[0].first_data_seg or 1
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
    # The stream was laid out by the profile's parser, and probed with it as extract would.
    rows = dict(track.detail)
    assert rows["volume profile"] == "qic80-rev-n (spec)"
    assert rows["file-set parser"] == "qic113-basic"
    assert rows["probe"].startswith("passed: score "), rows["probe"]
    text = dump_track(track, segment=1)
    assert "Volume: Source segment" in text
    result = CliRunner().invoke(
        cli, ["--dump", "--volume", "0", "--segment", "0", str(_compressed_image(tmp_path / "b"))]
    )
    assert result.exit_code == 0, result.output
    assert "Directory Section" in result.output

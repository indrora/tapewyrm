"""cinnabar.annotate: header, volume table, volume data and compression spans."""

from __future__ import annotations

import struct

from qiclib import header as qiclib_header
from qiclib.testing.builders import build_format_parameter_record, build_vtbl_entry
from qiclib.volume import profile as vp
from qiclib.volume import table as qiclib_table
from tapewyrm_archive.twti import SegmentEntry, SegmentState

from cinnabar.annotate import header as header_mod
from cinnabar.annotate import sectors as sectors_mod
from cinnabar.annotate import volume as volume_ann
from cinnabar.model import VolumeReading, depth, level
from tests.conftest import basic_volume_stream, extended_entry, extended_volume_stream


def test_header_spans_cover_fpr_fields_and_bsm():
    fpr = build_format_parameter_record(tape_name="SYNTH")
    data = fpr.ljust(29_696, b"\x00")
    spans = header_mod.header_spans(data)
    assert [s.label for s in spans] == ["Format Parameter Record", "Bad Sector Map"]
    fpr_span = spans[0]
    assert fpr_span.sublabel == "SYNTH"
    labels = {c.label: c for c in fpr_span.children}
    assert labels["tape name"].start == 30 and labels["tape name"].end == 74
    assert labels["55 AA 55 AA"].sublabel == "55 aa 55 aa"
    assert dict(labels["seg/trk"].detail)["value"] == "207"
    bsm = spans[1]
    assert bsm.start == (
        qiclib_header.BSM_OFFSET if data[4] == 4 else qiclib_header.FIXED_BSM_OFFSET
    )


def test_logical_families_match_the_annotators_rows():
    """The shared-lane registry repeats the annotators' ROWS tuples; keep them in step."""
    from cinnabar.annotate import LOGICAL_FAMILIES, level_of, row_title
    from cinnabar.annotate import volume as volume_mod

    assert LOGICAL_FAMILIES == (
        header_mod.ROWS,
        volume_mod.VTBL_ROWS,
        volume_mod.VOLUME_ROWS,
        volume_mod.EXTENT_ROWS,
    )
    assert level_of("QIC-113: Entry parts") == 2 and level_of("MFM: Sectors") is None
    assert row_title("QIC-113: Volume table") == "Volume table"


def test_fpr_span_lists_every_field_and_the_geometry():
    """One click on the record shows all of it; short fields carry their value as sublabel."""
    from qiclib import segment as seg_mod
    from qiclib.testing.builders import build_header_segment

    data = seg_mod.segment_data(build_header_segment(0))
    fpr = header_mod.fpr_spans(data)
    rows = dict(fpr.detail)
    assert rows["segments per track"] == str(int.from_bytes(data[24:26], "little"))
    assert rows["tracks"] == str(data[26])
    assert rows["geometry"] == f"{data[26]} tracks x {rows['segments per track']} segments"
    assert rows["max fsd / ftk / fsc"] == f"{data[27]} / {data[28]} / {data[29]}"
    short = {c.label: c.sublabel for c in fpr.children if c.end - c.start <= 2}
    assert short["seg/trk"] == rows["segments per track"]
    assert short["fmt"] == rows["format code"]


def test_vtbl_spans_show_entries_and_the_terminator():
    e1 = build_vtbl_entry(start_seg=3, end_seg=10, description="FIRST", flags=0x20)
    e2 = build_vtbl_entry(start_seg=11, end_seg=12, description="SECOND", compressed=True)
    data = (e1 + e2).ljust(1024, b"\x00")
    entries = qiclib_table.parse_volume_table_data(data)
    (table,) = volume_ann.vtbl_spans(data, entries)
    assert table.sublabel == "2 entries"
    kids = table.children
    assert [k.label for k in kids] == ["Volume", "Volume", "end"]
    assert kids[0].sublabel == "FIRST" and kids[1].sublabel == "SECOND"
    assert dict(kids[0].detail)["flags"].endswith("dir-last")
    assert dict(kids[1].detail)["compressed"] == "True"
    fields = {f.label: f for f in kids[0].children}
    assert (fields["sig"].start, fields["sig"].end) == (0, 4)
    assert fields["description"].sublabel == "FIRST"
    assert depth([table]) == 3
    # No reading: the fixed Rev G table past byte 56, and no verdict rows.
    assert "os ver" in fields and "volume profile" not in dict(kids[0].detail)
    assert "extended OS" not in dict(kids[0].detail)


def test_vtbl_entry_rows_and_fields_follow_the_profiles_reading():
    """Under a profile, bytes 57+ are drawn where it places them and every entry carries its verdict."""
    rec = bytearray(build_vtbl_entry(start_seg=3, end_seg=10, description="MTN-LIKE"))
    rec[58:61] = b"MTN"
    rec[102:108] = b"LABEL "
    data = bytes(rec).ljust(1024, b"\x00")
    entries = [qiclib_table.parse_vtbl_base(bytes(rec))]
    reading = VolumeReading(
        "mtn",
        "qic113-basic",
        "bench",
        7,
        False,
        ("single-tape",),
        fields=(
            ("multi_cartridge_seq", 57, 1),
            ("match", 58, 3),
            ("dir_section_size", 92, 4),
            ("data_section_size", 96, 4),
            ("source_label", 102, 16),
            ("compression", 120, 1),
        ),
        extent_offset_bytes=4,
    )
    (table,) = volume_ann.vtbl_spans(data, entries, reading)
    assert dict(table.detail)["volume profile"] == "mtn (bench)"
    entry = table.children[0]
    rows = dict(entry.detail)
    assert rows["volume profile"] == "mtn (bench)"
    assert rows["profile score"] == "7 (fingerprint held)"
    assert rows["file-set parser"] == "qic113-basic"
    assert rows["warnings"] == "single-tape"
    assert "extended OS" not in rows
    fields = {f.label: f for f in entry.children}
    assert (fields["match"].start, fields["match"].end, fields["match"].style) == (58, 61, "sig")
    assert dict(fields["match"].detail)["value"] == "4d 54 4e"
    assert (fields["label"].start, fields["label"].end) == (102, 118)
    assert fields["label"].sublabel == "LABEL"
    assert (fields["data size"].start, fields["data size"].end) == (96, 100)
    assert fields["cmp"].start == 120
    # The fixed Rev G table is not drawn under a profile; bytes 0-56 are everyone's.
    assert "os ver" not in fields and "113" not in fields
    assert fields["sig"].start == 0 and fields["flags"].start == 56


def test_vtbl_fields_are_the_vendors_when_the_profile_places_none():
    """vendor-unknown (no [vtbl] fields), and a vendor-bit record with no reading, leave bytes 57+ grey."""
    rec = build_vtbl_entry(start_seg=3, end_seg=4, flags=0x01)
    data = rec.ljust(1024, b"\x00")
    entries = [qiclib_table.parse_vtbl_base(rec)]
    for reading in (VolumeReading("vendor-unknown", "qic113-basic", "spec", 2), None):
        (table,) = volume_ann.vtbl_spans(data, entries, reading)
        past = [f for f in table.children[0].children if f.start > 56]
        assert [(f.label, f.style, f.start, f.end) for f in past] == [("vendor", "vendor", 57, 128)]
        assert past[0].sublabel == ""
    eliminated = VolumeReading("qic80-rev-n", "qic113-basic", "spec", -3, True, ("generic",))
    rows = dict(volume_ann.vtbl_spans(data, entries, eliminated)[0].children[0].detail)
    assert rows["profile score"] == "-3 (fingerprint failed)" and rows["warnings"] == "generic"


def test_basic_volume_spans_directory_first():
    stream = basic_volume_stream()
    entry = qiclib_table.parse_vtbl_base(build_vtbl_entry(start_seg=2, end_seg=2))
    spans = volume_ann.volume_spans(stream, entry)
    assert [s.label for s in spans] == ["Directory Section", "Data Section"]
    directory, data = spans
    assert [c.label for c in directory.children] == ["DOCS", "HELLO.TXT", "WORLD.TXT", "INNER.TXT"]
    assert directory.children[0].style == "dir-subdir"
    assert [c.label for c in data.children] == ["HELLO.TXT", "WORLD.TXT"]
    hello = data.children[0]
    assert [g.label for g in hello.children] == ["33CC33CC", "HELLO.TXT", "path", "data"]
    sig, _entry, _path, filedata = hello.children
    assert stream[int(sig.start) : int(sig.end)] == b"\xcc\x33\xcc\x33"
    assert stream[int(filedata.start) : int(filedata.end)] == b"hello tape\n"
    assert data.end == len(stream)
    assert list(level(spans, 2))  # three levels deep


def test_volume_sections_follow_the_named_parser():
    """The profile's parser lays the volume out: basic on a Basic-DOS stream, extended on an Extended one."""
    basic = basic_volume_stream()
    basic_entry = qiclib_table.parse_vtbl_base(build_vtbl_entry(start_seg=2, end_seg=2))
    spans = volume_ann.volume_spans(basic, basic_entry, parser="qic113-basic")
    assert [s.label for s in spans] == ["Directory Section", "Data Section"]
    ext, record = extended_volume_stream()
    ext_entry = extended_entry(record)
    assert ext_entry.data_section_size and ext_entry.dir_section_size  # the profile read them
    spans = volume_ann.volume_spans(ext, ext_entry, parser="qic113-extended")
    assert [s.label for s in spans] == ["Data Section", "Directory Section"]
    data, directory = spans
    assert [c.label for c in directory.children] == ["C:", "DOCS", "HELLO.TXT", "NOTES.TXT"]
    assert directory.start == ext_entry.data_section_size
    hello = next(c for c in data.children if c.label == "HELLO.TXT")
    filedata = next(g for g in hello.children if g.label == "data")
    assert ext[int(filedata.start) : int(filedata.end)] == b"hello tape\n"


def test_a_volume_without_a_parser_is_one_span_saying_so():
    """Parser ``none`` (recognised, not listable) and a parser cinnabar cannot draw get a note, not a guess."""
    stream = basic_volume_stream()
    entry = qiclib_table.parse_vtbl_base(build_vtbl_entry(start_seg=2, end_seg=2))
    (note,) = volume_ann.volume_spans(stream, entry, parser="none")
    assert (note.label, note.style, note.start, note.end) == (
        "No file-set parser",
        "no-parser",
        0,
        len(stream),
    )
    assert dict(note.detail)["file-set parser"] == "none"
    assert note.row == "QIC-113: Sections"
    (note,) = volume_ann.volume_spans(stream, entry, parser="qicstream-v2")
    assert note.label == "qicstream-v2: not drawn"
    assert "no annotator" in dict(note.detail)["note"]


def test_a_stream_with_no_verdict_is_probed_for_its_parser():
    """No profile (a hand-built stream): every parser is probed, as inspect does with a bare TWVL."""
    basic = basic_volume_stream()
    basic_entry = qiclib_table.parse_vtbl_base(build_vtbl_entry(start_seg=2, end_seg=2))
    assert volume_ann.probe_parser(basic, basic_entry) == "qic113-basic"
    ext, record = extended_volume_stream()
    ext_entry = extended_entry(record)
    assert volume_ann.probe_parser(ext, ext_entry) == "qic113-extended"
    labels = [s.label for s in volume_ann.volume_spans(ext, ext_entry)]
    assert labels == ["Data Section", "Directory Section"]
    assert volume_ann.probe_parser(bytes(4096), basic_entry) == "none"
    (note,) = volume_ann.volume_spans(bytes(4096), basic_entry)
    assert note.style == "no-parser"


def test_probe_rows_report_the_parsers_verdict():
    """The volume track's probe rows: what extract would stamp into the TWVL header."""
    ext, record = extended_volume_stream()
    ext_entry = extended_entry(record)
    rows, warnings = volume_ann.probe_rows(ext, ext_entry, "qic113-extended")
    verdict = dict(rows)["probe"]
    assert verdict.startswith("passed: score ") and "4 entries" in verdict
    assert warnings == ()
    basic_entry = qiclib_table.parse_vtbl_base(build_vtbl_entry(start_seg=2, end_seg=2))
    rows, _ = volume_ann.probe_rows(basic_volume_stream(), basic_entry, "qic113-extended")
    verdict = dict(rows)["probe"]
    assert verdict.startswith("FAILED") and "next profile" in verdict
    rows, _ = volume_ann.probe_rows(ext, ext_entry, "none")
    assert dict(rows)["probe"].startswith("FAILED")
    rows, _ = volume_ann.probe_rows(ext, ext_entry, "no-such-parser")
    assert dict(rows)["probe"].startswith("no file-set parser named")


def test_compressed_volume_has_no_stream_spans_but_extents_per_segment():
    # The compression bit is past byte 56, so it is the profile's to read (parse_vtbl_base leaves it None).
    rec = build_vtbl_entry(start_seg=2, end_seg=2, compressed=True)
    entry = vp.decode_entry(rec, vp.load("qic80-rev-n"))
    assert entry.compressed is True
    assert volume_ann.volume_spans(b"\x00" * 100, entry) == []
    # An extent: 8-byte offset, a 100-byte compressed frame, a 50-byte raw frame, null fill.
    seg = bytearray(29_696)
    struct.pack_into("<Q", seg, 0, 123_456)
    struct.pack_into("<H", seg, 8, 100)
    struct.pack_into("<H", seg, 8 + 2 + 100, 0x8000 | 50)
    (extent,) = volume_ann.extent_spans(bytes(seg), spanning=False)
    assert extent.sublabel.startswith("2 frames @ 123,456")
    kinds = [(c.label, c.style) for c in extent.children]
    assert kinds[0] == ("offset", "extent-offset")
    assert kinds[1] == ("frame", "frame") and kinds[2] == ("raw", "frame-raw")
    assert kinds[-1] == ("fill", "fill")
    # Segment-spanning: the first two bytes say where the next extent starts.
    seg2 = bytearray(29_696)
    struct.pack_into("<H", seg2, 0, 0)
    spans = volume_ann.extent_spans(bytes(seg2), spanning=True)
    assert [s.label for s in spans] == ["next", "frame (cont.)"]
    # A profile with 4-byte frame sizes (NovaStor): the size field is a doubleword, raw flag in bit 15.
    seg3 = bytearray(29_696)
    struct.pack_into("<Q", seg3, 0, 7)
    struct.pack_into("<I", seg3, 8, 0x8000 | 20)
    (extent,) = volume_ann.extent_spans(bytes(seg3), spanning=False, frame_size_bytes=4)
    raw = extent.children[1]
    assert (raw.label, raw.start, raw.end) == ("raw", 8, 8 + 4 + 20)
    assert raw.children[0].end - raw.children[0].start == 4


def test_flux_parity_sectors_are_labelled_ecc():
    """Sectors 30-32 of a segment are RS parity: ECC, whatever the segment holds."""
    from qiclib.types import RawSector

    sectors = [RawSector(0, 0, fsc, bytes(1024), True, True, False) for fsc in range(1, 33)]
    positions = [(k * 10.0, k * 10.0 + 1, k * 10.0 + 2, k * 10.0 + 9) for k in range(32)]
    spans = sectors_mod.sector_spans(sectors, positions, ["Header"] * 32)
    assert [s.sublabel for s in spans[:2]] == ["Header", "Header"]
    assert [s.sublabel for s in spans[-3:]] == ["ECC"] * 3
    assert {s.style for s in spans[:29]} == {"record"} and {s.style for s in spans[29:]} == {"ecc"}
    assert dict(spans[-1].detail)["holds"] == "RS parity for the segment"


def test_slot_spans_follow_the_segment_table():
    entry = SegmentEntry(SegmentState.CORRECTED, erasures=2, data_len=29_696, excluded_mask=0b100)
    slots = sectors_mod.slot_spans(entry, base=29_696)
    assert len(slots) == 29  # the image keeps no parity sectors
    assert slots[0].start == 29_696 and slots[1].start == 29_696 + 1024
    assert slots[2].style == "slot-excluded" and slots[3].style == "slot-corrected"
    missing = sectors_mod.slot_spans(SegmentEntry(SegmentState.MISSING))
    assert {s.style for s in missing} == {"slot-missing"}

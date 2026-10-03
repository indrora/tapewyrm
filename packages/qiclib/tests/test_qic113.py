"""QIC-113 Basic-DOS extraction tests (DESIGN.md §7.5)."""

from __future__ import annotations

import pytest

from qiclib.qic113 import extract, is_extended_os
from qiclib.testing.builders import build_data_entry, build_dir_entry, build_vtbl_entry
from qiclib.volume import VtblEntry

# Attribute bits (DESIGN.md §7.5).
ATTR_READ = 0x01
ATTR_SUBDIR = 0x20
ATTR_LAST_IN_DIR = 0x40
ATTR_LAST_IN_TABLE = 0x80


def _vtbl(**kw) -> VtblEntry:
    from qiclib.volume import _parse_vtbl_entry

    return _parse_vtbl_entry(build_vtbl_entry(start_seg=2, end_seg=3, **kw))


def test_basic_dos_directory_tree_and_files():
    """Synthesize a small tree: root has file1.txt + subdir; subdir has file2.txt."""
    file1_data = b"hello world"
    file2_data = b"nested file contents"

    # --- Directory Section (breadth-first preorder) ---
    # Root level: file1.txt (not last), subdir (last-in-dir).
    de_file1 = build_dir_entry("file1.txt", attrs=ATTR_READ, data_entry_size=len(file1_data))
    de_subdir = build_dir_entry("subdir", attrs=ATTR_READ | ATTR_SUBDIR | ATTR_LAST_IN_DIR)
    # subdir's level: file2.txt (last-in-dir AND last-in-table).
    de_file2 = build_dir_entry(
        "file2.txt",
        attrs=ATTR_READ | ATTR_LAST_IN_DIR | ATTR_LAST_IN_TABLE,
        data_entry_size=len(file2_data),
    )
    directory_section = de_file1 + de_subdir + de_file2

    # --- Data Section (same order; directories have no data entry) ---
    # file1.txt (root) and file2.txt (subdir).
    de_file1_copy = build_dir_entry("file1.txt", attrs=ATTR_READ, data_entry_size=len(file1_data))
    de_file2_copy = build_dir_entry("file2.txt", attrs=ATTR_READ, data_entry_size=len(file2_data))
    # Path Entries name the directory (QIC-113 §7.2.2): root = "".
    data_section = build_data_entry(de_file1_copy, "", file1_data) + build_data_entry(
        de_file2_copy, "subdir", file2_data
    )

    stream = directory_section + data_section
    vtbl = _vtbl(description="C:", os_type=1, flags=0)

    fileset = extract(stream, vtbl)
    assert fileset.name == "C:"
    assert fileset.extended_os is False

    by_path = {f.path: f for f in fileset.files}
    assert "subdir" in by_path and by_path["subdir"].is_dir
    assert "file1.txt" in by_path
    assert by_path["file1.txt"].data == file1_data
    assert by_path["subdir/file2.txt"].data == file2_data
    assert by_path["subdir/file2.txt"].is_dir is False


def test_data_entry_signature_resync():
    """A junk gap before the data section's signature must be skipped (resync)."""
    fdata = b"important"
    de = build_dir_entry(
        "a.txt", attrs=ATTR_READ | ATTR_LAST_IN_DIR | ATTR_LAST_IN_TABLE, data_entry_size=len(fdata)
    )
    directory_section = de
    de_copy = build_dir_entry("a.txt", attrs=ATTR_READ, data_entry_size=len(fdata))
    data_section = b"\x99\x88\x77garbage" + build_data_entry(de_copy, "", fdata)

    stream = directory_section + data_section
    vtbl = _vtbl(description="C:")
    fileset = extract(stream, vtbl)
    by_path = {f.path: f for f in fileset.files}
    assert by_path["a.txt"].data == fdata


def test_unreadable_at_backup_flag():
    fdata = b"x"
    de = build_dir_entry(
        "bad.txt",
        attrs=ATTR_READ | ATTR_LAST_IN_DIR | ATTR_LAST_IN_TABLE,
        data_entry_size=len(fdata),
        extra_info=2,  # bits 0..5 == 2 => unreadable-at-backup
    )
    de_copy = build_dir_entry("bad.txt", attrs=ATTR_READ, data_entry_size=len(fdata), extra_info=2)
    stream = de + build_data_entry(de_copy, "", fdata)
    fileset = extract(stream, _vtbl())
    f = next(f for f in fileset.files if f.path == "bad.txt")
    assert f.unreadable_at_backup is True


def test_ltlt_subsection_skipped():
    fdata = b"data"
    de = build_dir_entry(
        "f.txt", attrs=ATTR_READ | ATTR_LAST_IN_DIR | ATTR_LAST_IN_TABLE, data_entry_size=len(fdata)
    )
    de_copy = build_dir_entry("f.txt", attrs=ATTR_READ, data_entry_size=len(fdata))
    stream = (
        de
        + build_data_entry(de_copy, "", fdata)
        + b"LTLT"
        + b"\x01\x00\x00\x00multi-cartridge-junk"
    )
    fileset = extract(stream, _vtbl())
    by_path = {f.path: f for f in fileset.files}
    assert by_path["f.txt"].data == fdata


def test_ltlt_inside_file_data_is_not_a_link_section():
    """Only an LTLT near the end is the Link Section; one inside a file is data."""
    fdata = b"before LTLT after" + b"x" * 2000
    de = build_dir_entry(
        "f.txt", attrs=ATTR_READ | ATTR_LAST_IN_DIR | ATTR_LAST_IN_TABLE, data_entry_size=len(fdata)
    )
    de_copy = build_dir_entry("f.txt", attrs=ATTR_READ, data_entry_size=len(fdata))
    stream = de + build_data_entry(de_copy, "", fdata)
    by_path = {f.path: f for f in extract(stream, _vtbl()).files}
    assert by_path["f.txt"].data == fdata


def test_directory_order_follows_the_spec_example_and_skips_empty_dirs():
    """QIC-113 Rev G §7.1.4's example tree, plus an empty directory.

    ROOT: COMEXE, EMPTY (empty), TEXT; COMEXE: STUFF, LANGUAGE;
    LANGUAGE: APL, C, BASIC. Levels are written ROOT, COMEXE, STUFF, LANGUAGE,
    APL, C, BASIC, TEXT; EMPTY has no level (its size is a data header's, not 0).
    """
    from qiclib.qic113 import _build_tree, _flatten, _parse_directory_section

    sub, last = ATTR_READ | ATTR_SUBDIR, ATTR_LAST_IN_DIR

    def level(*names: str, end: bool = False) -> bytes:
        out = b""
        for k, name in enumerate(names):
            is_dir = name.isupper()
            attrs = sub if is_dir else ATTR_READ
            if k == len(names) - 1:
                attrs |= last | (ATTR_LAST_IN_TABLE if end else 0)
            out += build_dir_entry(name, attrs=attrs, data_entry_size=30 if name == "EMPTY" else 0)
        return out

    table = (
        level("COMEXE", "EMPTY", "TEXT", "root.txt")
        + level("STUFF", "LANGUAGE", "make.exe")
        + level("s.dat")
        + level("APL", "C", "BASIC")
        + level("a.apl")
        + level("c.c")
        + level("b.bas")
        + level("t.txt", end=True)
    )
    entries, _ = _parse_directory_section(table)
    paths = {n.path for n in _flatten(_build_tree(entries))}
    assert {
        "COMEXE/STUFF/s.dat",
        "COMEXE/LANGUAGE/APL/a.apl",
        "COMEXE/LANGUAGE/C/c.c",
        "COMEXE/LANGUAGE/BASIC/b.bas",
        "TEXT/t.txt",
        "EMPTY",
    } <= paths


@pytest.mark.parametrize("sizes_in_vtbl", [True, False], ids=["vtbl-sizes", "scan-fallback"])
def test_directory_last_finds_segment_aligned_directory(sizes_in_vtbl):
    """Directory-Last: the directory sits after the Segment Gap (QIC-113 Rev G §7, §7.1.1).

    The stream is laid out in whole segments, as an uncompressed volume's
    segments are: [data + zero gap][directory + zero padding]. The directory
    must be found from the VTBL's data section size rounded up to a segment
    and, when the VTBL has no sizes, from the end of the data-section walk --
    not from ``len(stream) - dir_section_size`` (that lands in the padding)
    nor at the last data entry's signature (that parses as garbage).
    """
    import struct

    from qiclib.tape_profile import SEGMENT_DATA_BYTES

    fdata = b"directory-last payload"
    directory = build_dir_entry(
        "sub", attrs=ATTR_READ | ATTR_SUBDIR | ATTR_LAST_IN_DIR, modify_date=0x3A000000
    ) + build_dir_entry(
        "f.txt",
        attrs=ATTR_READ | ATTR_LAST_IN_DIR | ATTR_LAST_IN_TABLE,
        data_entry_size=len(fdata),
    )
    data = build_data_entry(build_dir_entry("f.txt", attrs=ATTR_READ), "sub", fdata)
    stream = (
        data
        + bytes(SEGMENT_DATA_BYTES - len(data))
        + directory
        + bytes(SEGMENT_DATA_BYTES - len(directory))
    )

    from qiclib.volume import _parse_vtbl_entry

    raw = bytearray(build_vtbl_entry(start_seg=2, end_seg=3, flags=0x20, os_type=1))
    if sizes_in_vtbl:
        struct.pack_into("<I", raw, 92, len(directory))
        struct.pack_into("<Q", raw, 96, len(data))
    vtbl = _parse_vtbl_entry(bytes(raw))
    assert vtbl.directory_last

    by_path = {f.path: f for f in extract(stream, vtbl).files}
    assert by_path["sub"].is_dir  # only the directory section lists it
    assert by_path["sub"].mtime is not None
    assert by_path["sub/f.txt"].data == fdata
    assert not by_path["sub/f.txt"].lost
    assert set(by_path) == {"sub", "sub/f.txt"}


def test_extended_os_detection():
    import struct

    raw = bytearray(build_vtbl_entry(start_seg=2, end_seg=3, flags=0x01, os_type=0))
    struct.pack_into("<H", raw, 58, 113)
    struct.pack_into("<H", raw, 60, 7)
    from qiclib.volume import _parse_vtbl_entry

    entry = _parse_vtbl_entry(bytes(raw))
    assert is_extended_os(entry) is True

    basic = _parse_vtbl_entry(build_vtbl_entry(start_seg=2, end_seg=3, os_type=1))
    assert is_extended_os(basic) is False


def test_compression_hook_passthrough_flag():
    fdata = b"z"
    de = build_dir_entry(
        "c.txt", attrs=ATTR_READ | ATTR_LAST_IN_DIR | ATTR_LAST_IN_TABLE, data_entry_size=len(fdata)
    )
    de_copy = build_dir_entry("c.txt", attrs=ATTR_READ, data_entry_size=len(fdata))
    stream = de + build_data_entry(de_copy, "", fdata)
    vtbl = _vtbl(compressed=True)
    fileset = extract(stream, vtbl)
    assert fileset.compressed is True

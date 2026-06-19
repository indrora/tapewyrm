"""QIC-113 Basic-DOS extraction tests (DESIGN.md §7.5)."""

from __future__ import annotations

from tapewyrm.codec.qic113 import extract, is_extended_os
from tapewyrm.codec.volume import VtblEntry
from tests.fixtures.builders import build_data_entry, build_dir_entry, build_vtbl_entry

# Attribute bits (DESIGN.md §7.5).
ATTR_READ = 0x01
ATTR_SUBDIR = 0x20
ATTR_LAST_IN_DIR = 0x40
ATTR_LAST_IN_TABLE = 0x80


def _vtbl(**kw) -> VtblEntry:
    from tapewyrm.codec.volume import _parse_vtbl_entry

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
    data_section = build_data_entry(de_file1_copy, "file1.txt", file1_data) + build_data_entry(
        de_file2_copy, "subdir/file2.txt", file2_data
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
    data_section = b"\x99\x88\x77garbage" + build_data_entry(de_copy, "a.txt", fdata)

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
    stream = de + build_data_entry(de_copy, "bad.txt", fdata)
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
        + build_data_entry(de_copy, "f.txt", fdata)
        + b"LTLT"
        + b"\x01\x00\x00\x00multi-cartridge-junk"
    )
    fileset = extract(stream, _vtbl())
    by_path = {f.path: f for f in fileset.files}
    assert by_path["f.txt"].data == fdata


def test_extended_os_detection():
    import struct

    raw = bytearray(build_vtbl_entry(start_seg=2, end_seg=3, flags=0x01, os_type=0))
    struct.pack_into("<H", raw, 58, 113)
    struct.pack_into("<H", raw, 60, 7)
    from tapewyrm.codec.volume import _parse_vtbl_entry

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
    stream = de + build_data_entry(de_copy, "c.txt", fdata)
    vtbl = _vtbl(compressed=True)
    fileset = extract(stream, vtbl)
    assert fileset.compressed is True

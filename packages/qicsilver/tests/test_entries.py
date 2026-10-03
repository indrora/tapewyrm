"""qicsilver.entries: the one directory walk that tar and inspect both use."""

from __future__ import annotations

import tarfile

import pytest
from conftest import EXT_MTIME

from qicsilver.entries import entry_data, read_volume, tar_name
from qicsilver.tar import write_tar


def test_tar_names_drop_the_drive_colon():
    assert tar_name("C:/WINDOWS/WIN.INI") == "C/WINDOWS/WIN.INI"
    assert tar_name("C:") == "C"
    assert tar_name("/x") == "root/x"


def test_extended_entries_have_paths_sizes_modes_and_damage(extended_volume):
    listing = read_volume(extended_volume)
    assert listing.extended and listing.format_name == "Extended"
    by_path = {e.path: e for e in listing.entries}
    assert list(by_path) == [
        "C:",
        "C:/DOCS",
        "C:/EXAMPLE.TXT",
        "C:/BROKEN.DAT",
        "C:/DOCS/NOTES.TXT",
    ]
    root, example, broken, notes = (
        by_path["C:"],
        by_path["C:/EXAMPLE.TXT"],
        by_path["C:/BROKEN.DAT"],
        by_path["C:/DOCS/NOTES.TXT"],
    )
    assert root.is_dir and root.tar_name == "C" and root.mode_string == "drwxr-xr-x"
    assert example.size == 12 and example.mtime == EXT_MTIME and example.attrs == 0x01
    assert example.mode_string == "-r--r--r--" and example.damage is None
    assert entry_data(listing.volume, example) == b"hello world\n"
    assert broken.mode_string == "-rw-r--r--"
    assert (broken.missing, broken.error, broken.damage) == (50, False, "lost")
    assert (notes.missing, notes.error, notes.damage) == (0, True, "error")
    assert (listing.files, listing.dirs, listing.damaged) == (3, 2, 2)
    assert listing.missing_bytes == 50 and listing.lost_segments == [4]
    assert listing.tape_name == "EXAMPLE TAPE" and listing.description == "EXAMPLE BACKUP"
    assert listing.date == "1998-12-23 05:13:20"


def test_basic_dos_entries_list_found_and_lost_files(basic_volume):
    listing = read_volume(basic_volume)
    assert not listing.extended and listing.format_name == "Basic-DOS"
    by_path = {e.path: e for e in listing.entries}
    assert by_path["DOCS"].is_dir
    example, gone = by_path["EXAMPLE.TXT"], by_path["GONE.TXT"]
    assert example.mode_string == "-rw-r--r--" and example.damage is None
    assert entry_data(listing.volume, example) == b"hello"
    assert (gone.size, gone.missing, gone.damage) == (9, 9, "lost")
    assert entry_data(listing.volume, gone) == bytes(9)
    assert listing.damaged == 1


@pytest.mark.parametrize("fixture", ["extended_volume", "basic_volume"])
def test_tar_and_the_entry_walk_agree(fixture, request, tmp_path):
    """Every tar member is an entry with the same name, size, mtime and mode, and
    tar's damage list is exactly the damaged entries."""
    volume = request.getfixturevalue(fixture)
    listing = read_volume(volume)
    res = write_tar(volume, tmp_path / "out.tar")
    with tarfile.open(tmp_path / "out.tar") as tar:
        members = [(m.name, m.isdir(), m.size, m.mtime, m.mode) for m in tar.getmembers()]
    expected = [
        (e.tar_name, e.is_dir, 0 if e.is_dir else e.size, e.mtime or 0, e.mode)
        for e in listing.entries
    ]
    assert members == expected
    assert (res.files, res.dirs) == (listing.files, listing.dirs)
    assert res.damaged == [
        (e.path, e.missing, e.size, e.error) for e in listing.entries if e.damage
    ]

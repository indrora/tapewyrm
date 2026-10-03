"""qicsilver.tar: TWVL volume -> tar."""

import pytest
from tapewyrm_archive.twvl import Volume

from qicsilver.tar import _tar_name, default_report_path, write_tar


def test_tar_names_drop_the_drive_colon():
    assert _tar_name("C:/WINDOWS/WIN.INI") == "C/WINDOWS/WIN.INI"
    assert _tar_name("C:") == "C"
    assert _tar_name("/x") == "root/x"


def test_report_sits_next_to_the_tar(tmp_path):
    assert default_report_path(tmp_path / "a.tar") == tmp_path / "a.tar.damaged.txt"


def _extended_vtbl_raw() -> str:
    """A VTBL record that reads as QIC-113 extended: vendor bit + signature 113 rev 7."""
    rec = bytearray(128)
    rec[0:4] = b"VTBL"
    rec[56] = 0x01
    rec[58:60] = (113).to_bytes(2, "little")
    rec[60:62] = (7).to_bytes(2, "little")
    return rec.hex()


def test_volume_without_section_sizes_is_refused(tmp_path):
    vol = tmp_path / "v.twvl"
    Volume(
        header={
            "format": "TWVL",
            "holes": [],
            "data_section_size": None,
            "dir_section_size": None,
            "volume_size": 0,
            "vtbl": {"raw": _extended_vtbl_raw()},
        },
        data=b"",
    ).save(vol)
    with pytest.raises(ValueError, match="no QIC-113 section sizes"):
        write_tar(vol, tmp_path / "out.tar")


def test_basic_dos_volume_tars_found_and_lost_files(tmp_path):
    """A Basic-DOS volume: one file with data, one listed but lost, one directory."""
    import tarfile

    from qiclib.testing.builders import build_data_entry, build_dir_entry

    r, sub, last, end = 0x03, 0x20, 0x40, 0x80
    data = b"hello"
    # sizes count the data header (QIC-113 §7.1.3); builders rewrite the copy's
    table = (
        build_dir_entry("D", attrs=r | sub, data_entry_size=0)
        + build_dir_entry("a.txt", attrs=r, data_entry_size=4 + 17 + 1 + len(data))
        + build_dir_entry("gone.txt", attrs=r | last, data_entry_size=4 + 20 + 1 + 9)
        + build_dir_entry("in.txt", attrs=r | last | end, data_entry_size=0)
    )
    stream = table + build_data_entry(build_dir_entry("a.txt", attrs=r), "", data)
    vtbl = bytearray(128)
    vtbl[0:4] = b"VTBL"
    vol = tmp_path / "b.twvl"
    Volume(
        header={
            "format": "TWVL",
            "holes": [],
            "tape_name": "T",
            "lost_segments": [],
            "volume_size": len(stream),
            "vtbl": {"raw": vtbl.hex(), "description": ""},
        },
        data=stream,
    ).save(vol)
    res = write_tar(vol, tmp_path / "b.tar")
    with tarfile.open(tmp_path / "b.tar") as t:
        names = {m.name: m for m in t.getmembers()}
        assert t.extractfile("a.txt").read() == data
    assert names["D"].isdir() and "gone.txt" in names
    assert [d[0] for d in res.damaged] == ["gone.txt"]

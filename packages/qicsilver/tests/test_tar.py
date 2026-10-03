"""qicsilver.tar: TWVL volume -> tar."""

import pytest
from tapewyrm_archive.twvl import Volume

from qicsilver.tar import default_report_path, write_tar


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
            "directory_offset": None,
            "volume_size": 0,
            "vtbl": {"raw": _extended_vtbl_raw(), "flags": 0x01, "compressed": None},
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
            "data_section_size": None,
            "dir_section_size": None,
            "directory_offset": None,
            "volume_size": len(stream),
            "vtbl": {"raw": vtbl.hex(), "description": "", "flags": 0, "compressed": False},
        },
        data=stream,
    ).save(vol)
    res = write_tar(vol, tmp_path / "b.tar")
    with tarfile.open(tmp_path / "b.tar") as t:
        names = {m.name: m for m in t.getmembers()}
        assert t.extractfile("a.txt").read() == data
    assert names["D"].isdir() and "gone.txt" in names
    assert [d[0] for d in res.damaged] == ["gone.txt"]


def test_basic_dos_directory_located_from_header_sizes_not_raw_vtbl(tmp_path):
    """TWS-3 6.2 rule 9: section sizes come from the header, not VTBL bytes 57+.

    A compressed Directory-Last volume with no ``directory_offset``: the
    directory sits at exactly ``data_section_size``. The last file's data entry
    fell in a hole, so walking the data section stops short of it; only the
    header's size finds the directory. The raw record's bytes 96-103 hold
    nonsense, as they do when another program's layout put a label there.
    """
    import tarfile

    from qiclib.testing.builders import build_data_entry, build_dir_entry

    r, last, end = 0x03, 0x40, 0x80
    data = b"hello"
    present = build_data_entry(build_dir_entry("a.txt", attrs=r), "", data)
    lost_len = 4 + 17 + 1 + 9  # b.txt's data entry, all in the hole
    directory = build_dir_entry("a.txt", attrs=r, data_entry_size=len(present)) + build_dir_entry(
        "b.txt", attrs=r | last | end, data_entry_size=lost_len
    )
    data_size = len(present) + lost_len
    stream = present + bytes(lost_len) + directory
    raw = bytearray(128)
    raw[0:4] = b"VTBL"
    raw[56] = 0x20  # Directory-Last, not vendor specific: Basic-DOS
    raw[96:104] = b"\xff" * 8  # a size no reader may take from here
    vol = tmp_path / "c.twvl"
    Volume(
        header={
            "format": "TWVL",
            "holes": [[len(present), data_size]],
            "tape_name": "T",
            "lost_segments": [5],
            "data_section_size": data_size,
            "dir_section_size": len(directory),
            "directory_offset": None,
            "volume_size": len(stream),
            "vtbl": {
                "raw": raw.hex(),
                "description": "",
                "flags": 0x20,
                "compressed": True,
                "data_section_size": data_size,
                "dir_section_size": len(directory),
            },
        },
        data=stream,
    ).save(vol)
    res = write_tar(vol, tmp_path / "c.tar")
    with tarfile.open(tmp_path / "c.tar") as t:
        assert sorted(t.getnames()) == ["a.txt", "b.txt"]
        assert t.extractfile("a.txt").read() == data
    assert [d[0] for d in res.damaged] == ["b.txt"]


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        (lambda h: h.pop("data_section_size"), "data_section_size"),
        (lambda h: h.update(dir_section_size="12"), "dir_section_size"),
        (lambda h: h.update(directory_offset=-1), "directory_offset"),
        (lambda h: h["vtbl"].pop("flags"), "vtbl.flags"),
        (lambda h: h["vtbl"].update(compressed=1), "vtbl.compressed"),
        (lambda h: h["vtbl"].update(raw="00"), "vtbl.raw"),
        (lambda h: h.update(vtbl=[]), "vtbl"),
    ],
)
def test_malformed_volume_header_is_refused(tmp_path, change, problem):
    """A header member tar sizes or places a section with must be there and well typed."""
    from tapewyrm_archive.errors import MalformedFileError

    header = {
        "format": "TWVL",
        "holes": [],
        "data_section_size": 0,
        "dir_section_size": 0,
        "directory_offset": None,
        "volume_size": 0,
        "vtbl": {"raw": _extended_vtbl_raw(), "flags": 0x01, "compressed": None},
    }
    change(header)
    vol = tmp_path / "m.twvl"
    Volume(header=header, data=b"").save(vol)
    with pytest.raises(MalformedFileError, match=problem):
        write_tar(vol, tmp_path / "out.tar")

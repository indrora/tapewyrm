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


def test_volume_without_section_sizes_is_refused(tmp_path):
    vol = tmp_path / "v.twvl"
    Volume(
        header={"format": "TWVL", "holes": [], "data_section_size": None, "dir_section_size": None},
        data=b"",
    ).save(vol)
    with pytest.raises(ValueError, match="no QIC-113 section sizes"):
        write_tar(vol, tmp_path / "out.tar")

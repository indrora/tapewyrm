"""provenance_path: what a header records for an input file's path."""

from __future__ import annotations

from pathlib import Path, PureWindowsPath

import pytest

from tapewyrm_archive.provenance import provenance_path


@pytest.mark.parametrize(
    ("given", "recorded"),
    [
        ("/Users/x/tapes/jc.twtz", "tapes/jc.twtz"),
        ("/Users/x/captures/jc-1998/track-00.twrf", "jc-1998/track-00.twrf"),
        ("jc.twtz", "jc.twtz"),
        ("./jc.twtz", "jc.twtz"),
        ("../jc.twtz", "jc.twtz"),
        ("../../tapes/jc.twtz", "tapes/jc.twtz"),
        ("a/b/c/jc.twtz", "c/jc.twtz"),
        ("/jc.twtz", "jc.twtz"),
        (Path("/home/someone/tapes/jc.twtz"), "tapes/jc.twtz"),
        (PureWindowsPath(r"C:\Users\x\tapes\jc.twtz"), "tapes/jc.twtz"),
    ],
)
def test_provenance_path_keeps_only_the_directory_name_and_file_name(given, recorded):
    assert provenance_path(given) == recorded


def test_provenance_path_never_records_an_absolute_path():
    assert not provenance_path("/Users/x/tapes/jc.twtz").startswith("/")
    assert "Users" not in provenance_path("/Users/x/tapes/jc.twtz")

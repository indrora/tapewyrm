"""`qicsilver inspect`: a tar-tv style listing of a TWVL volume, or of one volume of an image."""

from __future__ import annotations

import json

from click.testing import CliRunner
from conftest import error_text

from qicsilver.cli import cli


def _run(*args: str):
    result = CliRunner().invoke(cli, ["inspect", *args])
    assert result.exit_code == 0, result.output
    return result.stdout


def _listing_lines(output: str) -> list[str]:
    """The entry lines: everything that starts with a type+mode string."""
    return [
        line
        for line in output.splitlines()
        if len(line) > 10 and line[0] in "d-" and line[1:10].strip("rwx-") == ""
    ]


def test_extended_summary_and_one_line_per_entry(extended_volume):
    out = _run(str(extended_volume))
    assert "EXAMPLE TAPE" in out and "EXAMPLE BACKUP" in out and "1998-12-23 05:13:20" in out
    assert "Extended" in out
    assert "3 files, 2 directories, 2 damaged" in out
    assert "50 bytes missing" in out and "1 lost segment" in out
    lines = _listing_lines(out)
    assert len(lines) == 5
    example = next(line for line in lines if line.endswith("C:/EXAMPLE.TXT"))
    assert example.startswith("-r--r--r--") and " 12 " in example
    assert "1998-12-23 05:13:20" in example
    assert "lost 50" in next(line for line in lines if line.endswith("C:/BROKEN.DAT"))
    assert "error" in next(line for line in lines if line.endswith("C:/DOCS/NOTES.TXT"))


def test_basic_dos_volume(basic_volume):
    out = _run(str(basic_volume))
    assert "Basic-DOS" in out
    gone = next(line for line in _listing_lines(out) if line.endswith("GONE.TXT"))
    assert "lost 9" in gone


def test_damaged_shows_only_damaged_entries(extended_volume):
    lines = _listing_lines(_run("--damaged", str(extended_volume)))
    assert [line.split()[-1] for line in lines] == ["C:/BROKEN.DAT", "C:/DOCS/NOTES.TXT"]


def test_paths_filter_by_glob_on_the_full_path(extended_volume):
    lines = _listing_lines(_run(str(extended_volume), "*.TXT"))
    assert [line.split()[-1] for line in lines] == ["C:/EXAMPLE.TXT", "C:/DOCS/NOTES.TXT"]
    lines = _listing_lines(_run(str(extended_volume), "C:/DOCS*", "*.DAT"))
    assert [line.split()[-1] for line in lines] == [
        "C:/DOCS",
        "C:/BROKEN.DAT",
        "C:/DOCS/NOTES.TXT",
    ]


def test_json_document(extended_volume):
    doc = json.loads(_run("--json", str(extended_volume)))
    assert set(doc) == {"summary", "entries"}
    summary = doc["summary"]
    assert summary["tape_name"] == "EXAMPLE TAPE"
    assert summary["description"] == "EXAMPLE BACKUP"
    assert summary["date"] == "1998-12-23 05:13:20"
    assert summary["directory_format"] == "Extended"
    assert (summary["files"], summary["directories"], summary["damaged"]) == (3, 2, 2)
    assert summary["missing_bytes"] == 50 and summary["lost_segments"] == [4]
    by_path = {e["path"]: e for e in doc["entries"]}
    assert by_path["C:/EXAMPLE.TXT"] == {
        "path": "C:/EXAMPLE.TXT",
        "tar_name": "C/EXAMPLE.TXT",
        "type": "file",
        "mode": "-r--r--r--",
        "size": 12,
        "mtime": 914_390_000,
        "attributes": 1,
        "data_offset": by_path["C:/EXAMPLE.TXT"]["data_offset"],
        "missing_bytes": 0,
        "error": False,
        "damage": None,
    }
    assert by_path["C:/BROKEN.DAT"]["damage"] == "lost"
    assert by_path["C:/DOCS/NOTES.TXT"]["damage"] == "error"
    damaged = json.loads(_run("--json", "--damaged", str(extended_volume)))
    assert [e["path"] for e in damaged["entries"]] == ["C:/BROKEN.DAT", "C:/DOCS/NOTES.TXT"]
    assert damaged["summary"]["files"] == 3  # the summary is always the whole volume


def test_inspect_counts_match_tar(extended_volume, basic_volume, tmp_path):
    for volume in (extended_volume, basic_volume):
        doc = json.loads(_run("--json", str(volume)))["summary"]
        tar = CliRunner().invoke(cli, ["tar", str(volume), str(tmp_path / "x.tar")])
        assert tar.exit_code == 0, tar.output
        assert (
            f"{doc['files']} files, {doc['directories']} directories, {doc['damaged']} damaged"
            in tar.output
        )


# ---------------------------------------------------------------------------
# inspect IMAGE: the volume is read out of the tape image, nothing written
# ---------------------------------------------------------------------------


def _shape(lines: list[str]) -> list[tuple[str, str, str]]:
    """(mode, size, path) of each entry line: the columns an intact copy shares with a holed one."""
    return [(line[:10], line.split()[1], line.split()[-1]) for line in lines]


def test_an_image_with_one_volume_lists_it_without_a_number(extended_volume, extended_image):
    """inspect IMAGE reads the tape's only volume as inspect reads its .twvl."""
    from_image = _run(str(extended_image))
    from_file = _run(str(extended_volume))
    assert "tape name     EXAMPLE TAPE" in from_image
    assert "volume        EXAMPLE BACKUP 0" in from_image
    assert "missing       0 bytes missing" in from_image
    # The same entries in the same order; only BROKEN.DAT's damage column
    # differs (intact in the image, half in a hole in the .twvl fixture).
    assert _shape(_listing_lines(from_image)) == _shape(_listing_lines(from_file))
    [notes] = [line for line in _listing_lines(from_image) if line.endswith("NOTES.TXT")]
    assert notes.split()[-2] == "error"


def test_an_image_listing_names_the_file_and_the_volume_number(extended_image):
    doc = json.loads(_run(str(extended_image), "--json"))
    assert doc["summary"]["volume"] == str(extended_image)
    assert doc["summary"]["volume_index"] == 0
    assert (doc["summary"]["files"], doc["summary"]["directories"]) == (3, 2)


def test_several_volumes_need_dash_dash_volume(three_volume_image):
    """Never a silent first volume: the error says how many there are and how to pick."""
    result = CliRunner().invoke(cli, ["inspect", str(three_volume_image)])
    assert result.exit_code == 1, result.output
    assert "3 volumes" in error_text(result) and "--volume N" in error_text(result)
    chosen = _run(str(three_volume_image), "--volume", "2")
    assert "volume        EXAMPLE BACKUP 2" in chosen
    doc = json.loads(_run(str(three_volume_image), "--volume", "1", "--json"))
    assert doc["summary"]["volume_index"] == 1


def test_a_volume_the_tape_lacks_is_a_clean_error(three_volume_image):
    result = CliRunner().invoke(cli, ["inspect", str(three_volume_image), "--volume", "7"])
    assert result.exit_code == 1, result.output
    assert "no volume 7" in error_text(result) and "0-2" in error_text(result)


def test_dash_dash_volume_with_a_twvl_file_is_a_usage_error(extended_volume):
    result = CliRunner().invoke(cli, ["inspect", str(extended_volume), "--volume", "0"])
    assert result.exit_code == 2, result.output
    assert "--volume" in error_text(result) and "already one volume" in error_text(result)


def test_files_are_told_apart_by_magic_not_by_name(tmp_path):
    capture = tmp_path / "lies.twvl"  # TWRF magic under a .twvl name
    capture.write_bytes(b"TWRF" + bytes(64))
    result = CliRunner().invoke(cli, ["inspect", str(capture)])
    assert result.exit_code == 1 and "tw convert" in error_text(result), result.output
    junk = tmp_path / "junk.twti"
    junk.write_bytes(b"nope nope")
    result = CliRunner().invoke(cli, ["inspect", str(junk)])
    assert result.exit_code == 1 and "not a TWVL volume" in error_text(result), result.output

"""File-set parsers (qiclib.volume.fileset): the registry, the probes, and the standard checks.

The point of a probe is the unk-co tape: a volume the wrong parser reads
without complaint (186 nameless zero-byte files dated 1970) must score
below the right one. The synthetic volumes here reproduce that in both
directions: an Extended volume under the Basic-DOS parser and a Basic-DOS
volume under the Extended parser both fail their probes.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from qiclib.testing.builders import (
    FSET_UP,
    ExtItem,
    FsetItem,
    build_data_entry,
    build_dir_entry,
    build_ext_volume,
    build_fset_header,
    build_fset_volume,
    build_twvl,
    build_vtbl_entry,
    make_short_date,
)
from qiclib.volume import fileset as fs
from qiclib.volume import qic113ext as q
from qiclib.volume.fileset.common import standard_checks
from qiclib.volume.fileset.entry import DIR_MODE, RO_MODE, RW_MODE, VolumeEntry
from qiclib.volume.verdict import Probe, VolumeWarning

NOW = datetime(2026, 11, 7, tzinfo=UTC)
MTIME = 914_390_000  # 1998-12-23 05:13:20 UTC
BACKUP_DATE = make_short_date(1998, 12, 23, 5, 13, 20)
SOURCE = Path("synthetic.twvl")


def _extended_volume():
    """A two-file Extended volume, directory last, with the QIC-113 signature in its record."""
    items = [
        ExtItem("C:", q.T_ROOT | q.T_DIR | q.T_LAST_IN_DIR),
        ExtItem("A.TXT", 0, b"hello world\n", MTIME, attrs=0x01),
        ExtItem("B.DAT", q.T_LAST_IN_DIR | q.T_LAST_IN_SET, b"x" * 50, MTIME),
    ]
    data_section, directory = build_ext_volume(items)
    record = bytearray(
        build_vtbl_entry(start_seg=3, end_seg=4, description="EXT", flags=0x01, date=BACKUP_DATE)
    )
    record[58:60] = (113).to_bytes(2, "little")
    record[60:62] = (7).to_bytes(2, "little")
    return build_twvl(
        data_section + directory,
        bytes(record),
        data_section_size=len(data_section),
        dir_section_size=len(directory),
    )


def _basic_volume():
    """A Basic-DOS (Directory-First) volume: one directory, one file with data, one lost."""
    read, write, subdir, last, end = 0x01, 0x02, 0x20, 0x40, 0x80
    data = b"hello"
    table = (
        build_dir_entry("DOCS", attrs=read | write | subdir, data_entry_size=0)
        + build_dir_entry("EXAMPLE.TXT", attrs=read | write, data_entry_size=4 + 23 + 1 + 5)
        + build_dir_entry("GONE.TXT", attrs=read | write | last, data_entry_size=4 + 20 + 1 + 9)
        + build_dir_entry("INNER.TXT", attrs=read | last | end, data_entry_size=0)
    )
    stream = table + build_data_entry(build_dir_entry("EXAMPLE.TXT", attrs=read | write), "", data)
    record = build_vtbl_entry(start_seg=3, end_seg=4, description="BASIC", date=BACKUP_DATE)
    return build_twvl(stream, record)


def _vtbl(vol):
    return fs.header_vtbl(SOURCE, vol.header)


def test_registry_names_the_builtin_parsers():
    assert {"none", "qic113-basic", "qic113-extended", "qicstream-fset"} <= set(fs.names())
    assert [p.name for p in fs.listable()] == [
        "qic113-basic",
        "qic113-extended",
        "qicstream-fset",
    ]
    with pytest.raises(ValueError, match="qic113-basic"):
        fs.get("no-such-parser")


def test_extended_parser_passes_on_an_extended_volume():
    vol = _extended_volume()
    probe = fs.get("qic113-extended").probe(vol, _vtbl(vol), SOURCE, now=NOW)
    assert probe.passed and probe.failures == [] and probe.entries == 3
    assert probe.warnings == set()
    entries = fs.get("qic113-extended").entries(vol, _vtbl(vol), SOURCE)
    assert [e.path for e in entries] == ["C:", "C:/A.TXT", "C:/B.DAT"]
    assert entries[1].mode == RO_MODE and entries[1].size == 12 and entries[1].mtime == MTIME


def test_basic_parser_fails_on_an_extended_volume():
    """The unk-co misroute: the wrong parser must not pass, however quietly it 'succeeds'."""
    vol = _extended_volume()
    probe = fs.get("qic113-basic").probe(vol, _vtbl(vol), SOURCE, now=NOW)
    assert not probe.passed


def test_basic_parser_passes_on_a_basic_volume():
    vol = _basic_volume()
    probe = fs.get("qic113-basic").probe(vol, _vtbl(vol), SOURCE, now=NOW)
    assert probe.passed and probe.entries == 4
    assert [c.name for c in probe.checks if c.name == "data entries found"] == [
        "data entries found"
    ]


def test_extended_parser_fails_on_a_basic_volume():
    """No section sizes in the table entry: the Extended parser cannot even place the directory."""
    vol = _basic_volume()
    probe = fs.get("qic113-extended").probe(vol, _vtbl(vol), SOURCE, now=NOW)
    assert probe.fatal is not None and not probe.passed


def test_probe_all_ranks_the_right_parser_first():
    ext, basic, fset = _extended_volume(), _basic_volume(), _fset_volume()
    assert fs.probe_all(ext, _vtbl(ext), SOURCE, now=NOW)[0][0].name == "qic113-extended"
    assert fs.probe_all(basic, _vtbl(basic), SOURCE, now=NOW)[0][0].name == "qic113-basic"
    assert fs.probe_all(fset, _vtbl(fset), SOURCE, now=NOW)[0][0].name == "qicstream-fset"


def test_none_parser_is_always_fatal_and_says_why():
    vol = _basic_volume()
    probe = fs.get("none").probe(vol, _vtbl(vol), SOURCE)
    assert probe.fatal and probe.warnings == {VolumeWarning.NO_PARSER}
    with pytest.raises(ValueError, match="no file-set parser"):
        fs.get("none").entries(vol, _vtbl(vol), SOURCE)


def test_damaged_directory_raises_the_warning():
    good = _extended_volume()
    directory_at = good.header["data_section_size"]
    damaged = build_twvl(
        good.data,
        bytes.fromhex(good.header["vtbl"]["raw"]),
        holes=[[directory_at, directory_at + 4]],
        data_section_size=good.header["data_section_size"],
        dir_section_size=good.header["dir_section_size"],
    )
    probe = fs.get("qic113-extended").probe(damaged, _vtbl(damaged), SOURCE, now=NOW)
    assert VolumeWarning.DIRECTORY_DAMAGED in probe.warnings


def test_standard_checks_reject_a_garbage_listing():
    """What the Basic parser made of unk-co: nameless, empty, dated 1970; and what they add to."""
    vol = _basic_volume()
    garbage = [
        VolumeEntry("", "", False, 0, 132, 0, RO_MODE, None, 0, False, lost=True) for _ in range(5)
    ]
    checks = standard_checks(garbage, vol, _vtbl(vol), now=NOW)
    failed = {c.name for c in checks if not c.ok}
    assert {"names are text", "files have data", "dates in era"} <= failed
    assert sum(c.points for c in checks) < 0


def test_probe_round_trips_through_its_dict():
    vol = _extended_volume()
    probe = fs.get("qic113-extended").probe(vol, _vtbl(vol), SOURCE, now=NOW)
    again = Probe.from_dict(probe.to_dict())
    assert again.score == probe.score and again.passed and again.entries == probe.entries
    assert [c.name for c in again.checks] == [c.name for c in probe.checks]


# ---------------------------------------------------------------------------
# QICStream v2 "FSET" (qicstream-fset): synthetic volumes only, no bench tape
# ---------------------------------------------------------------------------

FSET_ITEMS = [
    FsetItem("DOCS", is_dir=True, mtime=MTIME, attrs=0x10),
    FsetItem("A.TXT", data=b"hello world\n", mtime=MTIME, attrs=0x01),
    FsetItem("OLD", is_dir=True, mtime=MTIME, attrs=0x10),
    FsetItem("B.DAT", data=b"x" * 50, mtime=MTIME),
    FSET_UP,
    FSET_UP,
]


def _fset_volume(items=FSET_ITEMS, **kwargs):
    """A QICStream v2 volume: a directory, a file, a nested directory with a file, two pops."""
    record = build_vtbl_entry(start_seg=3, end_seg=4, description="FSET", date=BACKUP_DATE)
    return build_twvl(build_fset_volume(items, **kwargs), record)


def test_fset_parser_passes_on_an_fset_volume():
    vol = _fset_volume()
    parser = fs.get("qicstream-fset")
    probe = parser.probe(vol, _vtbl(vol), SOURCE, now=NOW)
    assert probe.passed and probe.failures == [] and probe.entries == 4
    assert probe.warnings == set()
    assert {"FSET magic", "directory stack balanced"} <= {c.name for c in probe.checks}
    entries = parser.entries(vol, _vtbl(vol), SOURCE)
    assert [e.path for e in entries] == ["DOCS", "DOCS/A.TXT", "DOCS/OLD", "DOCS/OLD/B.DAT"]
    assert [e.tar_name for e in entries] == [e.path for e in entries]
    docs, a, _old, b = entries
    assert docs.is_dir and docs.mode == DIR_MODE and docs.attrs == 0x10 and docs.mtime == MTIME
    assert a.mode == RO_MODE and a.size == 12 and a.mtime == MTIME and a.attrs == 0x01
    assert vol.data[a.data_offset : a.data_offset + a.size] == b"hello world\n"
    assert b.mode == RW_MODE and b.missing == 0 and b.damage is None
    assert vol.data[b.data_offset : b.data_offset + b.size] == b"x" * 50


def test_fset_file_is_the_sum_of_its_chunks_and_short_chunks_are_missing():
    """A 512-byte file in 100-byte chunks sums whole; a header claiming more than its chunks is lost."""
    items = [
        FsetItem("BIG.BIN", data=bytes(range(256)) * 2, chunk=100, mtime=MTIME),
        FsetItem("CUT.TXT", data=b"only this", size=20, mtime=MTIME),
    ]
    vol = _fset_volume(items)
    big, cut = fs.get("qicstream-fset").entries(vol, _vtbl(vol), SOURCE)
    assert big.size == 512 and big.missing == 0
    assert vol.data[big.data_offset : big.data_offset + 100] == bytes(range(100))
    assert cut.size == 20 and cut.missing == 11 and cut.damage == "lost"
    assert vol.data[cut.data_offset : cut.data_offset + 9] == b"only this"


def test_fset_parser_reports_an_unbalanced_directory_stack():
    """Every push must be popped by the end of the stream; one left open fails the check."""
    vol = _fset_volume(FSET_ITEMS[:-1])
    probe = fs.get("qicstream-fset").probe(vol, _vtbl(vol), SOURCE, now=NOW)
    assert [c.name for c in probe.failures] == ["directory stack balanced"]
    assert "1 directories left open" in probe.failures[0].detail


def test_fset_header_in_a_hole_warns():
    good = _fset_volume()
    first_header = good.data.index(bytes([6, 1])) + 1  # the first directory's prefix
    damaged = build_twvl(
        good.data,
        bytes.fromhex(good.header["vtbl"]["raw"]),
        holes=[[first_header, first_header + 3]],
    )
    probe = fs.get("qicstream-fset").probe(damaged, _vtbl(damaged), SOURCE, now=NOW)
    assert VolumeWarning.DIRECTORY_DAMAGED in probe.warnings


def test_qic113_parsers_fail_on_an_fset_volume():
    vol = _fset_volume()
    for name in ("qic113-basic", "qic113-extended"):
        assert not fs.get(name).probe(vol, _vtbl(vol), SOURCE, now=NOW).passed, name


def test_fset_parser_fails_on_the_qic113_volumes():
    for vol in (_extended_volume(), _basic_volume()):
        probe = fs.get("qicstream-fset").probe(vol, _vtbl(vol), SOURCE, now=NOW)
        assert not probe.passed, probe


def test_fset_header_past_the_buffer_is_fatal_not_a_crash():
    """A structLen that runs past the volume is the wrong layout: a fatal probe, no exception."""
    stream = build_fset_volume([FsetItem("DOCS", is_dir=True)], catalog=False, terminate=False)
    stream += bytes([5]) + build_fset_header("CUT.TXT", struct_len=0x4000)
    record = build_vtbl_entry(start_seg=3, end_seg=4, description="FSET", date=BACKUP_DATE)
    vol = build_twvl(stream, record)
    probe = fs.get("qicstream-fset").probe(vol, _vtbl(vol), SOURCE, now=NOW)
    assert probe.fatal is not None and "header" in probe.fatal and not probe.passed

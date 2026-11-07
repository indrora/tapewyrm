"""File-set parsers (qiclib.fileset): the registry, the probes, and the standard checks.

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

from qiclib import fileset as fs
from qiclib import qic113ext as q
from qiclib.fileset.common import standard_checks
from qiclib.fileset.entry import RO_MODE, VolumeEntry
from qiclib.testing.builders import (
    ExtItem,
    build_data_entry,
    build_dir_entry,
    build_ext_volume,
    build_twvl,
    build_vtbl_entry,
    make_short_date,
)
from qiclib.verdict import Probe, VolumeWarning

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
    assert {"none", "qic113-basic", "qic113-extended"} <= set(fs.names())
    assert [p.name for p in fs.listable()] == ["qic113-basic", "qic113-extended"]
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
    ext, basic = _extended_volume(), _basic_volume()
    assert fs.probe_all(ext, _vtbl(ext), SOURCE, now=NOW)[0][0].name == "qic113-extended"
    assert fs.probe_all(basic, _vtbl(basic), SOURCE, now=NOW)[0][0].name == "qic113-basic"


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

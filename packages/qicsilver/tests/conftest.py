"""Synthetic TWVL volumes, and TWTI images holding them, shared by the tar, entries and inspect tests.

All are built with ``qiclib.testing.builders`` and invented names only (the
bench tapes are strangers' backups; their listings stay out of the repo).
:func:`error_text` reads a failed CLI run's message out of rich-click's
panel; the inspect and tar tests check the same refusals with it.
"""

from __future__ import annotations

import re
import struct
from pathlib import Path

import click
import pytest
from click.testing import Result
from qiclib import segment as seg_mod
from qiclib.testing.builders import (
    ExtItem,
    build_data_entry,
    build_dir_entry,
    build_ext_volume,
    build_header_segment,
    build_twvl,
    build_volume_table_segment,
    build_vtbl_entry,
    make_short_date,
)
from qiclib.volume import qic113ext as q
from tapewyrm_archive.twti import SEGMENT_STRIDE, SegmentEntry, SegmentState, TapeImage

#: 1998-12-23 05:13:20 UTC, the mtime of every synthetic extended file.
EXT_MTIME = 914_390_000
BACKUP_DATE = make_short_date(1998, 12, 23, 5, 13, 20)

ROOT = q.T_ROOT | q.T_DIR | q.T_LAST_IN_DIR
SEGMENT_BYTES = 29 * 1024
TAPE_NAME = "EXAMPLE TAPE"

#: The Extended-OS backup every extended fixture holds, one of each kind of entry:
#:
#:   C:                      root
#:   C:/DOCS                 directory
#:   C:/EXAMPLE.TXT          read-only, intact (12 bytes)
#:   C:/BROKEN.DAT           100 bytes (the .twvl fixture puts 50 in a hole -> "lost 50")
#:   C:/DOCS/NOTES.TXT       the backup software's error flag -> "error"
EXT_ITEMS = [
    ExtItem("C:", ROOT),
    ExtItem("DOCS", q.T_DIR, mtime=EXT_MTIME),
    ExtItem("EXAMPLE.TXT", 0, b"hello world\n", EXT_MTIME, attrs=0x01),
    ExtItem("BROKEN.DAT", q.T_LAST_IN_DIR, b"x" * 100, EXT_MTIME, attrs=0x20),
    ExtItem("NOTES.TXT", q.T_LAST_IN_DIR | q.T_LAST_IN_SET | q.T_ERROR, b"note", EXT_MTIME),
]


def _ext_record(start_seg: int, end_seg: int, description: str, **kw) -> bytearray:
    """A VTBL record for an Extended-OS volume: flag byte 0x01, QIC-113 Rev G signature."""
    record = bytearray(
        build_vtbl_entry(
            start_seg=start_seg,
            end_seg=end_seg,
            description=description,
            flags=0x01,
            date=BACKUP_DATE,
            **kw,
        )
    )
    record[58:60] = (113).to_bytes(2, "little")  # QIC-113 signature, Rev G (7)
    record[60:62] = (7).to_bytes(2, "little")
    return record


@pytest.fixture
def extended_volume(tmp_path: Path) -> Path:
    """EXT_ITEMS as a TWVL file, with BROKEN.DAT half in a hole and segment 4 lost."""
    data_section, directory = build_ext_volume(EXT_ITEMS)
    broken = list(q.layout(q.parse_directory(directory)))[3]
    assert broken.data_offset is not None
    hole = [broken.data_offset + 10, broken.data_offset + 60]
    record = _ext_record(3, 4, "EXAMPLE BACKUP")
    path = tmp_path / "ext.twvl"
    build_twvl(
        data_section + directory,
        bytes(record),
        holes=[hole],
        lost_segments=[4],
        data_section_size=len(data_section),
        dir_section_size=len(directory),
    ).save(path)
    return path


def build_extended_image(path: Path, count: int = 1) -> Path:
    """A TWTI holding ``count`` copies of the EXT_ITEMS backup, one uncompressed segment each.

    Volume k is segment 3 + k, labelled "EXAMPLE BACKUP k", and intact (no
    holes, no lost segments), so against ``extended_volume`` only BROKEN.DAT's
    damage differs. The section sizes sit where the Rev N layout keeps them
    (data_section_size as a quadword at byte 96), the layout the volume
    profile guess settles on for such a record.
    """
    data_section, directory = build_ext_volume(EXT_ITEMS)
    records = []
    for k in range(count):
        rec = _ext_record(3 + k, 3 + k, f"EXAMPLE BACKUP {k}", dir_section_size=len(directory))
        struct.pack_into("<Q", rec, 96, len(data_section))
        records.append(bytes(rec))
    segments = {
        0: seg_mod.segment_data(build_header_segment(0, tape_name=TAPE_NAME)),
        1: seg_mod.segment_data(build_header_segment(1, tape_name=TAPE_NAME)),
        2: seg_mod.segment_data(build_volume_table_segment(2, records)),
        **{3 + k: (data_section + directory).ljust(SEGMENT_BYTES, b"\x00") for k in range(count)},
    }
    entries = [SegmentEntry(SegmentState.CLEAN, 0, len(segments[n])) for n in range(3 + count)]
    header = {
        "format": "TWTI",
        "segment_count": 3 + count,
        "segment_stride": SEGMENT_STRIDE,
        "qic80_header": {
            "header_seg": 0,
            "dup_header_seg": 1,
            "first_data_seg": 2,
            "tape_name": TAPE_NAME,
        },
    }
    TapeImage(header=header, entries=entries).save(path, lambda n: segments[n])
    return path


@pytest.fixture
def extended_image(tmp_path: Path) -> Path:
    """The EXT_ITEMS backup as the one volume of a tape image."""
    return build_extended_image(tmp_path / "ext.twti")


@pytest.fixture
def three_volume_image(tmp_path: Path) -> Path:
    """Three copies of the EXT_ITEMS backup, volumes 0-2 of one tape image."""
    return build_extended_image(tmp_path / "three.twti", count=3)


@pytest.fixture
def basic_volume(tmp_path: Path) -> Path:
    """A Basic-DOS (Directory-First) volume.

    DOCS                    directory
    EXAMPLE.TXT             writable, intact (5 bytes)
    GONE.TXT                listed, but its data entry never made it -> lost
    DOCS/INNER.TXT          read-only, size unknown from an empty entry
    """
    read, write, subdir, last, end = 0x01, 0x02, 0x20, 0x40, 0x80
    data = b"hello"
    # Data Entry sizes count the data header (QIC-113 7.1.3).
    table = (
        build_dir_entry("DOCS", attrs=read | write | subdir, data_entry_size=0)
        + build_dir_entry("EXAMPLE.TXT", attrs=read | write, data_entry_size=4 + 23 + 1 + 5)
        + build_dir_entry("GONE.TXT", attrs=read | write | last, data_entry_size=4 + 20 + 1 + 9)
        + build_dir_entry("INNER.TXT", attrs=read | last | end, data_entry_size=0)
    )
    stream = table + build_data_entry(build_dir_entry("EXAMPLE.TXT", attrs=read | write), "", data)
    record = build_vtbl_entry(start_seg=3, end_seg=4, description="BASIC BACKUP", date=BACKUP_DATE)
    path = tmp_path / "basic.twvl"
    build_twvl(stream, record).save(path)
    return path


# ---------------------------------------------------------------------------
# CLI runs
# ---------------------------------------------------------------------------


def error_text(result: Result) -> str:
    """A failed run's output with rich-click's panel (borders, wrapping) undone, for substring checks.

    rich-click draws the message in a box of U+2500-range line characters
    and wraps it to the terminal width, so it is compared as one line of
    words. A traceback is never acceptable: every refusal is a click
    exception, so the run must have failed cleanly.

    ANSI escapes are stripped first (``click.unstyle``). rich-click ignores
    CliRunner's ``color=False`` and forces a colour terminal whenever
    ``FORCE_COLOR``, ``PY_COLORS`` or ``GITHUB_ACTIONS`` is set
    (``rich_click.rich_help_configuration.force_terminal_default``). Under
    GitHub Actions its highlighter then wraps numbers and ``--options`` in
    SGR codes -- ``\\x1b[1;36m3\\x1b[0m volumes``, ``-\\x1b[..m-volume`` -- and
    a plain substring check like ``"3 volumes"`` misses. Locally (no such
    env var, not a tty) the output is plain, so this only ever bit in CI.
    """
    assert result.exit_code != 0
    assert "Traceback" not in result.output, result.output
    plain = click.unstyle(result.output)
    return " ".join(re.sub(r"[\u2500-\u257f]", " ", plain).split())

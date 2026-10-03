"""Synthetic TWVL volumes shared by the tar, entries and inspect tests.

Both are built with ``qiclib.testing.builders`` and invented names only (the
bench tapes are strangers' backups; their listings stay out of the repo).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from qiclib import qic113ext as q
from qiclib.testing.builders import (
    ExtItem,
    build_data_entry,
    build_dir_entry,
    build_ext_volume,
    build_twvl,
    build_vtbl_entry,
    make_short_date,
)

#: 1998-12-23 05:13:20 UTC, the mtime of every synthetic extended file.
EXT_MTIME = 914_390_000
BACKUP_DATE = make_short_date(1998, 12, 23, 5, 13, 20)

ROOT = q.T_ROOT | q.T_DIR | q.T_LAST_IN_DIR


@pytest.fixture
def extended_volume(tmp_path: Path) -> Path:
    """An Extended-OS volume with one of each kind of entry.

    C:                      root
    C:/DOCS                 directory
    C:/EXAMPLE.TXT          read-only, intact (12 bytes)
    C:/BROKEN.DAT           100 bytes, 50 of them in a hole -> "lost 50"
    C:/DOCS/NOTES.TXT       the backup software's error flag -> "error"
    """
    data_section, directory = build_ext_volume(
        [
            ExtItem("C:", ROOT),
            ExtItem("DOCS", q.T_DIR, mtime=EXT_MTIME),
            ExtItem("EXAMPLE.TXT", 0, b"hello world\n", EXT_MTIME, attrs=0x01),
            ExtItem("BROKEN.DAT", q.T_LAST_IN_DIR, b"x" * 100, EXT_MTIME, attrs=0x20),
            ExtItem("NOTES.TXT", q.T_LAST_IN_DIR | q.T_LAST_IN_SET | q.T_ERROR, b"note", EXT_MTIME),
        ]
    )
    broken = list(q.layout(q.parse_directory(directory)))[3]
    assert broken.data_offset is not None
    hole = [broken.data_offset + 10, broken.data_offset + 60]
    record = bytearray(
        build_vtbl_entry(
            start_seg=3, end_seg=4, description="EXAMPLE BACKUP", flags=0x01, date=BACKUP_DATE
        )
    )
    record[58:60] = (113).to_bytes(2, "little")  # QIC-113 signature, Rev G (7)
    record[60:62] = (7).to_bytes(2, "little")
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

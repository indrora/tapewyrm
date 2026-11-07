"""The ``qic113-basic`` parser: QIC-113 Rev G section 7 Basic-DOS volumes.

What Conner Backup Basics, QICStream v1 and the software behind the "MTN"
tapes wrote (QICStreamReader's connerbackup and qicstreamv1 readers are
hand-rolled versions of this layout): a directory section of fixed-portion
+ name entries, and a data section of ``33CC33CC``-signed data entries,
each a copy of its directory entry plus a path plus the bytes.
:func:`qiclib.qic113.extract` does the walk; this module turns its file set
into :class:`~qiclib.fileset.entry.VolumeEntry` records and adds the one
check the walk makes possible: were the data entries actually found?

The attribute byte holds the QIC-113 bits (bit 1 "write access allowed",
Rev G 7.1.3). Files the directory lists whose data never made it off the
tape are entries too, ``lost``, with every byte missing. Tar member names
are the paths as they are (no drive root).

The 9-byte fixed portion the MTN tapes write (no extra-info byte) is
detected from the size byte by :func:`qiclib.qic113._parse_dir_entry`, so
this parser takes no parameters.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from tapewyrm_archive.twvl import Volume

from qiclib import qic113
from qiclib.fileset.base import FileSetParser, Reading, register
from qiclib.fileset.common import directory_offset
from qiclib.fileset.entry import DIR_MODE, RO_MODE, RW_MODE, VolumeEntry
from qiclib.verdict import Check
from qiclib.volume import VtblEntry

log = logging.getLogger(__name__)


class Qic113BasicParser(FileSetParser):
    name = "qic113-basic"
    description = "QIC-113 Basic-DOS directory (Conner Backup Basics, QICStream v1, the MTN tapes)"

    def read(
        self, vol: Volume, vtbl: VtblEntry, source: Path, params: Mapping[str, Any]
    ) -> Reading:
        """``qiclib.qic113.extract`` lists every file, found or lost.

        A found file's missing bytes come from the volume's hole map (its
        data ``offset`` is into the same byte stream); a lost one is all
        missing. ``vtbl`` is the header's (:func:`~qiclib.fileset.common.header_vtbl`):
        its section sizes and compression flag, which place a Directory-Last
        directory when the header's ``directory_offset`` is null, are the
        header's.
        """
        dir_at = directory_offset(source, vol.header)
        log.debug("extracting the Basic-DOS file set (%d volume bytes)", len(vol.data))
        fileset = qic113.extract(vol.data, vtbl, dir_offset=dir_at)
        log.info(
            f"tape {vol.header.get('tape_name')!r}: {len(fileset.files)} entries "
            f"({sum(f.lost for f in fileset.files)} lost); "
            f"{vol.missing(0, len(vol.data)):,} of {len(vol.data):,} volume bytes missing"
        )
        entries: list[VolumeEntry] = []
        files = found = 0
        for f in fileset.files:
            if f.is_dir:
                entries.append(
                    VolumeEntry(
                        f.path,
                        f.path,
                        True,
                        0,
                        f.mtime,
                        f.attrs,
                        DIR_MODE,
                        None,
                        0,
                        f.unreadable_at_backup,
                    )  # fmt: skip
                )
                continue
            files += 1
            lost = f.lost or f.offset is None
            if lost:
                log.debug("%s: lost (no data entry); all %d bytes missing", f.path, f.size)
                missing = f.size
            else:
                assert f.offset is not None  # for mypy: lost covers None
                found += 1
                missing = vol.missing(f.offset, f.size)
            if missing or f.unreadable_at_backup:
                log.debug(
                    "%s: damaged (%d of %d bytes missing, unreadable at backup %s)",
                    f.path,
                    missing,
                    f.size,
                    f.unreadable_at_backup,
                )
            # QIC-113 attribute bit 1 is "write access allowed" (§7.1.3).
            mode = RW_MODE if f.attrs & qic113.ATTR_WRITE else RO_MODE
            entries.append(
                VolumeEntry(
                    f.path,
                    f.path,
                    False,
                    f.size,
                    f.mtime,
                    f.attrs,
                    mode,
                    None if lost else f.offset,
                    missing,
                    f.unreadable_at_backup,
                    lost=lost,
                )
            )
        checks: list[Check] = []
        if files:
            # The directory names files; the data section must hold their
            # data entries (33CC33CC + a copy of the entry). Half is the bar
            # because on a badly damaged tape most headers fall in holes --
            # but a directory read from the wrong bytes finds none at all.
            checks.append(
                Check(
                    "data entries found",
                    found >= max(1, files // 2),
                    2,
                    f"{found} of {files} files have a data entry",
                )
            )
        return Reading(entries, checks)


register(Qic113BasicParser())

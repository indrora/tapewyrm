"""The ``qic113-extended`` parser: QIC-113 Rev G section 8 extended-OS volumes.

What Colorado/HP CMS backup, Windows 95 Backup and Arcada Backup wrote
(QICStreamReader's qicstream95, qicstream95a, arcadabackup1 and
arcadabackup2 are four hand-rolled walks of this one layout, differing by a
byte in the fixed block they assume; the spec-driven
:mod:`qiclib.volume.qic113ext` reads the sizes instead). The directory section is
last, at ``data_section_size``; each entry's data entry starts at the
running sum of the data-entry sizes before it, and begins with the
``33CC33CC`` signature, which is the check this parser adds: are the
signatures where the directory says?

The attribute byte is the DOS one (bit 0 read-only); the root, e.g.
``C:``, becomes the top-level tar directory ``C``.

TODO: a volume whose directory section was lost (a Directory-Last volume
with its last segments unrecovered) could still be listed by scanning the
data section for the signatures, as the C# readers do without any
directory. That would be a second mode of this parser, raising
``directory-reconstructed``.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from tapewyrm_archive.qic80date import format_short_date
from tapewyrm_archive.twvl import Volume

from qiclib.volume import qic113ext as q
from qiclib.volume.fileset.base import FileSetParser, Reading, register
from qiclib.volume.fileset.common import directory_offset, volume_name
from qiclib.volume.fileset.entry import DIR_MODE, RO_MODE, RW_MODE, VolumeEntry, tar_name
from qiclib.volume.table import VtblEntry
from qiclib.volume.verdict import Check, VolumeWarning

log = logging.getLogger(__name__)

_DOS_READ_ONLY = 0x01


class Qic113ExtendedParser(FileSetParser):
    name = "qic113-extended"
    description = (
        "QIC-113 extended-OS directory (Colorado/HP CMS backup, Windows 95 Backup, Arcada)"
    )

    def read(
        self, vol: Volume, vtbl: VtblEntry, source: Path, params: Mapping[str, Any]
    ) -> Reading:
        """Extended-format volume: directory section last, at ``data_section_size``.

        ``vtbl`` is the header's, so both sizes are the header's. A volume
        with no section sizes in its table entry cannot be placed and is a
        ``ValueError`` (a fatal probe).
        """
        vol_name = volume_name(vol, source)
        dir_at = directory_offset(source, vol.header)
        hdr = vol.header
        data_size, dir_size = vtbl.data_section_size, vtbl.dir_section_size
        if data_size is None or dir_size is None:
            log.debug(
                "volume %s: data_section_size %r, dir_section_size %r; refusing",
                vol_name,
                data_size,
                dir_size,
            )
            raise ValueError(
                f"{vol_name}: the volume table entry has no QIC-113 section sizes; "
                "not supported yet"
            )
        # An uncompressed Directory-Last volume records the directory's exact
        # start (after the Segment Gap); otherwise it sits right after the data
        # (compressed volumes, whose layout has no gap: the bench "jc" tape).
        if dir_at is None:
            log.debug("no directory_offset in the header; directory follows the data section")
            dir_at = data_size
        log.debug("reading the directory section: %d bytes at offset %d", dir_size, dir_at)
        dir_bytes, dir_missing = vol.read(dir_at, dir_size)
        warnings: set[VolumeWarning] = set()
        if dir_missing:
            log.warning(
                "%s bytes of the directory are missing; the listing may be cut short",
                f"{dir_missing:,}",
            )
            warnings.add(VolumeWarning.DIRECTORY_DAMAGED)
        log.debug("parsing the QIC-113 directory")
        dir_entries = q.parse_directory(dir_bytes)
        paths = q.directory_paths(dir_entries)
        log.info(
            f"tape {hdr.get('tape_name')!r}, volume {vtbl.description!r} "
            f"({format_short_date(vtbl.date)}): {len(dir_entries)} entries; "
            f"{vol.missing(0, len(vol.data)):,} of {len(vol.data):,} volume bytes missing"
        )
        entries: list[VolumeEntry] = []
        placed = hits = 0
        for lay in q.layout(dir_entries):
            e, path = dir_entries[lay.index], paths[lay.index]
            # Every entry, directory or file, has a Data Entry in the data
            # section that starts with the signature (8.1.1.1). A hole
            # reads as zeros and so counts as a miss, which is the right
            # answer for a damaged tape too: half found is the bar.
            if lay.entry_offset + len(q.DATA_ENTRY_SIG) <= len(vol.data):
                placed += 1
                sig, _ = vol.read(lay.entry_offset, len(q.DATA_ENTRY_SIG))
                hits += sig == q.DATA_ENTRY_SIG
            mt = e.mtime
            if mt is None:
                log.debug("%s: no modification time in the directory", path)
            mtime = int(mt.timestamp()) if mt is not None else None
            if e.is_dir:
                entries.append(
                    VolumeEntry(
                        path, tar_name(path), True, 0, mtime, e.attributes, DIR_MODE, None, 0, False
                    )
                )
                continue
            missing = 0
            if lay.data_offset is not None and lay.data_size:
                missing = vol.missing(lay.data_offset, lay.data_size)
            else:
                log.debug(
                    "%s: no data (offset %r, size %r); an empty file",
                    path,
                    lay.data_offset,
                    lay.data_size,
                )
            flagged = bool(e.traversal & q.T_ERROR)
            if missing or flagged:
                log.debug(
                    "%s: damaged (%d of %d bytes missing, backup error flag %s)",
                    path,
                    missing,
                    lay.data_size,
                    flagged,
                )
            mode = RO_MODE if e.attributes & _DOS_READ_ONLY else RW_MODE
            entries.append(
                VolumeEntry(
                    path,
                    tar_name(path),
                    False,
                    lay.data_size,
                    mtime,
                    e.attributes,
                    mode,
                    lay.data_offset,
                    missing,
                    flagged,
                )
            )
        checks: list[Check] = []
        if placed:
            checks.append(
                Check(
                    "data entry signatures",
                    hits >= max(1, placed // 2),
                    2,
                    f"{hits} of {placed} data entries start with 33CC33CC",
                )
            )
        return Reading(entries, checks, warnings)


register(Qic113ExtendedParser())

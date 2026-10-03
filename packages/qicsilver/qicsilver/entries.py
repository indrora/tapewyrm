"""The entries of a TWVL backup volume: the one directory walk tar and inspect share.

``qicsilver tar`` writes these entries into a tar and ``qicsilver inspect``
lists them, so both read the volume through :func:`read_volume` and can never
disagree about which entries there are, how big they are, or which are
damaged. One :class:`VolumeEntry` per directory or file holds what both need:
the backup's own path and the tar member name, size, modification time, the
backup's attribute byte and the mode derived from it, where the file's bytes
start in the volume, how many of them fell in holes, and the backup
software's own error flag.

Both QIC-113 directory formats are read, chosen from the volume table entry
(the flag byte, VTBL byte 56, and the QIC-113 signature at 58-61, which every
tape profile reads the same way):

* **Extended** (Colorado/HP backup software): ``qiclib.qic113ext``. The
  attribute byte is the DOS one (bit 0 read-only); the root, e.g. ``C:``,
  becomes the top-level tar directory ``C``.
* **Basic-DOS** (e.g. the "MTN" tapes): ``qiclib.qic113``. The attribute byte
  holds the QIC-113 bits (bit 1 "write access allowed", Rev G 7.1.3). Files
  the directory lists whose data never made it off the tape are entries too,
  ``lost``, with every byte missing.

Where the sections are comes from the TWVL header, never from the raw VTBL
record (TWS-3 section 6.2, rule 9): ``data_section_size``,
``dir_section_size``, ``directory_offset``, ``vtbl.flags`` and
``vtbl.compressed`` were decoded by ``qicsilver extract`` through the volume
profile that fits the tape, while bytes 57-127 of the raw record mean
different things to different backup programs (an MTN tape keeps part of its
label where Rev N keeps the data section size). :func:`_header_vtbl` builds
the one :class:`~qiclib.volume.VtblEntry` both paths use from those members;
only the QIC-113 signature at bytes 58-61, which the header does not carry,
is read from ``vtbl.raw``.

Library code (STYLE.md §2.5): it logs, raises ``ValueError`` /
``MalformedFileError``, and prints nothing.
"""

from __future__ import annotations

import logging
import stat
from collections.abc import Iterator
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from qiclib import qic113
from qiclib import qic113ext as q
from qiclib.volume import VTBL_ENTRY_LEN, VtblEntry, parse_vtbl_base
from tapewyrm_archive.errors import MalformedFileError
from tapewyrm_archive.qic80date import format_short_date
from tapewyrm_archive.twvl import Volume

log = logging.getLogger(__name__)

# Permission bits each entry gets in a tar (and shows in a listing). The
# backup records no owner permissions, only "read-only" (Extended: DOS bit 0)
# or "write access allowed" (Basic-DOS: QIC-113 bit 1), so files are 0444 or
# 0644 and directories always 0755.
_DIR_MODE, _RW_MODE, _RO_MODE = 0o755, 0o644, 0o444
_DOS_READ_ONLY = 0x01


def tar_name(path: str) -> str:
    """'C:/WINDOWS/x' -> 'C/WINDOWS/x' (no ':' or leading '/' in tar names)."""
    root, _, rest = path.partition("/")
    root = root.rstrip(":").replace(":", "") or "root"
    return f"{root}/{rest}" if rest else root


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VolumeEntry:
    """One directory or file of a backup volume, as tar writes it and inspect lists it."""

    path: str  # the backup's own path ("C:/WINDOWS/WIN.INI"); damage reports use it
    tar_name: str  # the tar member name (Extended: drive colon dropped)
    is_dir: bool
    size: int  # bytes the backup recorded for the file (0 for a directory)
    mtime: int | None  # seconds since 1970 UTC; None = not recorded
    attrs: int  # the backup's attribute byte (DOS or QIC-113, see the module docstring)
    mode: int  # permission bits derived from attrs (0o755 / 0o644 / 0o444)
    data_offset: int | None  # where the file's bytes start in the volume; None = nowhere
    missing: int  # bytes of the file that fell in holes (zero-filled in a tar)
    error: bool  # the backup software's own "could not read this file" flag
    # Basic-DOS only: listed in the directory but its data entry was never
    # found, so all ``size`` bytes are missing and read back as zeros.
    lost: bool = False

    @property
    def damage(self) -> str | None:
        """'lost' (bytes in holes), 'error' (backup software flag only), or None.

        Directories are never damaged: tar only reports files, and inspect
        must count exactly what tar counts. A file both flagged and short of
        bytes is 'lost', the damage report's rule.
        """
        if self.is_dir or not (self.missing or self.error):
            return None
        return "error" if self.error and not self.missing else "lost"

    @property
    def mode_string(self) -> str:
        """``ls -l`` / ``tar tv`` type and mode, e.g. ``-r--r--r--``, ``drwxr-xr-x``."""
        kind = stat.S_IFDIR if self.is_dir else stat.S_IFREG
        return stat.filemode(kind | self.mode)


@dataclass
class VolumeListing:
    """A TWVL volume and every entry of its backup, in directory order."""

    path: Path
    volume: Volume
    vtbl: VtblEntry
    extended: bool
    entries: list[VolumeEntry]

    @property
    def format_name(self) -> str:
        """The QIC-113 directory format: 'Extended' or 'Basic-DOS'."""
        return "Extended" if self.extended else "Basic-DOS"

    @property
    def attribute_key(self) -> str:
        """The pax header tar keeps the attribute byte in."""
        return "TAPEWYRM.dos_attributes" if self.extended else "TAPEWYRM.qic113_attributes"

    # Display-only header members use .get: a header that lacks one still reads.
    @property
    def tape_name(self) -> str | None:
        return self.volume.header.get("tape_name")

    @property
    def description(self) -> str:
        return self.vtbl.description

    @property
    def date(self) -> str | None:
        """The volume's backup date, ``YYYY-MM-DD HH:MM:SS``, or None when undefined."""
        return format_short_date(self.vtbl.date)

    @property
    def lost_segments(self) -> list[int]:
        return list(self.volume.header.get("lost_segments") or [])

    @property
    def volume_bytes(self) -> int:
        return len(self.volume.data)

    @property
    def missing_bytes(self) -> int:
        """Volume bytes in holes (the whole volume, not only file data)."""
        return self.volume.missing(0, len(self.volume.data))

    @property
    def files(self) -> int:
        return sum(not e.is_dir for e in self.entries)

    @property
    def dirs(self) -> int:
        return sum(e.is_dir for e in self.entries)

    @property
    def damaged(self) -> int:
        return sum(e.damage is not None for e in self.entries)


# ---------------------------------------------------------------------------
# The volume table entry, from the TWVL header (TWS-3 sections 3, 6.2)
# ---------------------------------------------------------------------------

# What a member read by _member must hold, worded for the error message. The
# check uses type(), not isinstance: bool is an int subclass, and an integer
# member must not accept true/false.
_INT, _BOOL = "a non-negative integer", "true or false"


def _member(volume_path: Path, obj: dict, where: str, name: str, kind: str, nullable: bool) -> Any:
    """``obj[name]``, refused as malformed when absent or of the wrong JSON type.

    Every member read here is REQUIRED of a TWVL writer (TWS-3 section 3.1),
    so absent is malformed, not "unknown"; "unknown" is an explicit null,
    allowed only where the spec says "or null". Integers must be
    non-negative: they are sizes, offsets and flag bytes.
    """
    label = f"{where}{name}"
    if name not in obj:
        log.debug("%s: header lacks %s; refusing", volume_path, label)
        raise MalformedFileError(volume_path, "TWVL", f"the header lacks the member {label}")
    value = obj[name]
    if value is None and nullable:
        return None
    ok = (type(value) is int and value >= 0) if kind == _INT else type(value) is bool
    if not ok:
        log.debug("%s: %s is %r, not %s; refusing", volume_path, label, value, kind)
        expected = f"{kind} or null" if nullable else kind
        raise MalformedFileError(
            volume_path, "TWVL", f"{label} is {value!r}; it must be {expected}"
        )
    return value


def _header_vtbl(volume_path: Path, hdr: dict) -> VtblEntry:
    """The volume's VTBL entry as the TWVL header decoded it (TWS-3 6.2 rule 9).

    Bytes 0-56 of ``vtbl.raw`` are universal, so they seed the entry
    (:func:`parse_vtbl_base`), and the raw record stays on it for the QIC-113
    signature check in :func:`qiclib.qic113.is_extended_os`. Everything that
    sizes or places a section -- the flag byte, the compression flag and both
    section sizes -- is then taken from the header, which ``qicsilver
    extract`` filled through the right volume profile. Re-decoding bytes 57+
    of the raw record would apply one fixed layout to every tape, which is
    exactly the misreading volume profiles exist to avoid.
    """
    vtbl = hdr.get("vtbl")
    if not isinstance(vtbl, dict):
        log.debug("%s: vtbl is %r, not an object; refusing", volume_path, type(vtbl).__name__)
        raise MalformedFileError(volume_path, "TWVL", "the header's vtbl member is not an object")
    raw_hex = vtbl.get("raw")
    try:
        raw = bytes.fromhex(raw_hex) if isinstance(raw_hex, str) else None
    except ValueError:
        raw = None
    if raw is None or len(raw) != VTBL_ENTRY_LEN:
        log.debug("%s: vtbl.raw is %r; refusing", volume_path, raw_hex)
        raise MalformedFileError(
            volume_path,
            "TWVL",
            f"vtbl.raw must be the {VTBL_ENTRY_LEN}-byte record as "
            f"{2 * VTBL_ENTRY_LEN} hexadecimal digits",
        )
    flags = _member(volume_path, vtbl, "vtbl.", "flags", _INT, nullable=False)
    if flags > 0xFF:
        log.debug("%s: vtbl.flags %d is not a byte; refusing", volume_path, flags)
        raise MalformedFileError(volume_path, "TWVL", f"vtbl.flags is {flags}; it is one byte")
    entry = replace(
        parse_vtbl_base(raw),
        flags=flags,
        compressed=_member(volume_path, vtbl, "vtbl.", "compressed", _BOOL, nullable=True),
        # The top-level copies, which TWS-3 3.2 has writers keep equal to
        # vtbl's; rule 9 names these.
        data_section_size=_member(volume_path, hdr, "", "data_section_size", _INT, nullable=True),
        dir_section_size=_member(volume_path, hdr, "", "dir_section_size", _INT, nullable=True),
    )
    if entry.flags != raw[56]:
        log.debug(
            "%s: header flags 0x%02x differ from raw byte 56 0x%02x; using the header's",
            volume_path,
            entry.flags,
            raw[56],
        )
    log.debug(
        "%s: vtbl from header: flags 0x%02x, compressed %r, data %r, directory %r bytes",
        volume_path,
        entry.flags,
        entry.compressed,
        entry.data_section_size,
        entry.dir_section_size,
    )
    return entry


# ---------------------------------------------------------------------------
# Reading a volume
# ---------------------------------------------------------------------------


def read_volume(volume_path: Path) -> VolumeListing:
    """Load a TWVL volume and list every entry of its backup.

    Raises ``ValueError`` for a volume this cannot read (an Extended volume
    with no QIC-113 section sizes in its table entry), and
    ``MalformedFileError`` (a ``ValueError``) for a header member that is
    missing or mistyped.
    """
    log.info("reading volume %s...", volume_path)
    vol = Volume.load(volume_path)
    hdr = vol.header
    vtbl = _header_vtbl(volume_path, hdr)
    # null = the writer did not know it exactly; consumers then locate the
    # directory themselves (TWS-3 4.4, 6.2 rule 8).
    dir_at = _member(volume_path, hdr, "", "directory_offset", _INT, nullable=True)
    extended = qic113.is_extended_os(vtbl)
    if extended:
        log.info("QIC-113 extended directory format")
        entries = list(_extended_entries(vol, vtbl, dir_at, str(volume_path)))
    else:
        log.info("QIC-113 Basic-DOS directory format")
        entries = list(_basic_entries(vol, vtbl, dir_at))
    return VolumeListing(volume_path, vol, vtbl, extended, entries)


def entry_data(vol: Volume, entry: VolumeEntry) -> bytes:
    """The file's bytes as tar stores them: holes and lost files zero-filled.

    An Extended entry with no data area (or none of size) is empty, whatever
    its recorded size; a lost Basic-DOS file is ``size`` zeros.
    """
    if entry.lost:
        return bytes(entry.size)
    if entry.data_offset is None or not entry.size:
        return b""
    data, _ = vol.read(entry.data_offset, entry.size)
    return data


def _extended_entries(
    vol: Volume, vtbl: VtblEntry, dir_at: int | None, vol_name: str
) -> Iterator[VolumeEntry]:
    """Extended-format volume: directory section last, at ``data_section_size``.

    ``vtbl`` is :func:`_header_vtbl`'s, so both sizes are the header's.
    """
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
            f"{vol_name}: the volume table entry has no QIC-113 section sizes; not supported yet"
        )
    # An uncompressed Directory-Last volume records the directory's exact
    # start (after the Segment Gap); otherwise it sits right after the data
    # (compressed volumes, whose layout has no gap: the bench "jc" tape).
    if dir_at is None:
        log.debug("no directory_offset in the header; directory follows the data section")
        dir_at = data_size
    log.debug("reading the directory section: %d bytes at offset %d", dir_size, dir_at)
    dir_bytes, dir_missing = vol.read(dir_at, dir_size)
    if dir_missing:
        log.warning(
            "%s bytes of the directory are missing; the listing may be cut short",
            f"{dir_missing:,}",
        )
    log.debug("parsing the QIC-113 directory")
    entries = q.parse_directory(dir_bytes)
    paths = q.directory_paths(entries)
    log.info(
        f"tape {hdr.get('tape_name')!r}, volume {vtbl.description!r} "
        f"({format_short_date(vtbl.date)}): {len(entries)} entries; "
        f"{vol.missing(0, len(vol.data)):,} of {len(vol.data):,} volume bytes missing"
    )
    for lay in q.layout(entries):
        e, path = entries[lay.index], paths[lay.index]
        mt = e.mtime
        if mt is None:
            log.debug("%s: no modification time in the directory", path)
        mtime = int(mt.timestamp()) if mt is not None else None
        if e.is_dir:
            yield VolumeEntry(
                path, tar_name(path), True, 0, mtime, e.attributes, _DIR_MODE, None, 0, False
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
        mode = _RO_MODE if e.attributes & _DOS_READ_ONLY else _RW_MODE
        yield VolumeEntry(
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


def _basic_entries(vol: Volume, vtbl: VtblEntry, dir_at: int | None) -> Iterator[VolumeEntry]:
    """Basic-DOS volume: ``qiclib.qic113.extract`` lists every file, found or lost.

    A found file's missing bytes come from the volume's hole map (its data
    ``offset`` is into the same byte stream); a lost one is all missing.
    ``vtbl`` is :func:`_header_vtbl`'s: its section sizes and compression
    flag, which place a Directory-Last directory when ``dir_at`` is null, are
    the header's. Tar member names are the paths as they are (no drive root).
    """
    log.debug("extracting the Basic-DOS file set (%d volume bytes)", len(vol.data))
    fileset = qic113.extract(vol.data, vtbl, dir_offset=dir_at)
    log.info(
        f"tape {vol.header.get('tape_name')!r}: {len(fileset.files)} entries "
        f"({sum(f.lost for f in fileset.files)} lost); "
        f"{vol.missing(0, len(vol.data)):,} of {len(vol.data):,} volume bytes missing"
    )
    for f in fileset.files:
        if f.is_dir:
            yield VolumeEntry(
                f.path, f.path, True, 0, f.mtime, f.attrs, _DIR_MODE, None, 0,
                f.unreadable_at_backup,
            )  # fmt: skip
            continue
        lost = f.lost or f.offset is None
        if lost:
            log.debug("%s: lost (no data entry); all %d bytes missing", f.path, f.size)
            missing = f.size
        else:
            assert f.offset is not None  # for mypy: lost covers None
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
        mode = _RW_MODE if f.attrs & qic113.ATTR_WRITE else _RO_MODE
        yield VolumeEntry(
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

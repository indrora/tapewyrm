"""QIC-113 Rev G section 8: extended-OS volumes (directory + data entries).

Written from docs/qic-standards/qic113g.pdf and checked against the bench tape "jc" (a CMS
backup of a Windows 95 C: drive, QIC-113 Rev F, 5,474 entries).

Layout (section 8)::

    Volume = File Set Data Section + Segment Gap + File Set Directory Section

    Directory Entry (8.2.0.1)
      0, 2   size of the rest of the entry
      2, 8   Data Entry size  (bytes of this object's Data Entry, see below)
      10, 2  Path Entry size
      12, 2  native file system (a Data Description ID)
      14, 1  traversal byte (see the T_* flags)
      15...  Data Description Entries, each:
               0,2 ID   2,8 Data Area size   10,2 struct size n   12,n struct
               12+n,2 name size m   14+n,m name (UTF-16LE)

    Data Entry (8.1.1.1), concatenated in directory order with no gaps
      0, 4   signature 0x33CC33CC
      4, d   copy of the Directory Entry (its size fields may be all ones)
      4+d, p Path Entry: [u16 fs id, UTF-16LE component] separated by u16 0
      ...    Data Areas: 0x66996699, u16 ID, data (size from the Directory Entry)

The copies inside the data section may carry unknown (all-ones) sizes -- the
spec allows it and the bench tape does it -- so the Directory Section is the
authority: entry k starts at the sum of the Data Entry sizes before it. On the
bench tape those sums total the VTBL's data-section size exactly.
"""

from __future__ import annotations

import logging
import struct
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime

log = logging.getLogger(__name__)

DATA_ENTRY_SIG = b"\xcc\x33\xcc\x33"  # 0x33CC33CC little endian
DATA_AREA_SIG = b"\x99\x66\x99\x66"  # 0x66996699 little endian

# Data Description IDs (8.2.0.1.1.4).
DD_UNIX, DD_DOS, DD_NOVELL3, DD_OS2, DD_NT, DD_AFP, DD_DATA = 1, 2, 3, 4, 5, 6, 7
DD_NOVELL2, DD_NOVELL4, DD_WIN95 = 8, 9, 10
# Null data areas take no space on the media at all (8.1.1.3.1).
NULL_AREA_IDS = frozenset({DD_UNIX, DD_DOS})

# Traversal byte (8.2.0.1.1.1).
T_DIR, T_EMPTY, T_ERROR, T_LAST_IN_DIR, T_LAST_ON_MEDIA, T_LAST_IN_SET, T_ROOT = (
    1 << i for i in range(7)
)

_UNKNOWN_DATE = 0xFFFFFFFF


@dataclass(frozen=True)
class DataDescription:
    id: int
    area_size: int  # bytes of the matching Data Area (excluding sig + ID)
    struct: bytes
    name: str


@dataclass
class DirEntry:
    data_entry_size: int
    path_entry_size: int
    native_fs: int
    traversal: int
    descriptions: list[DataDescription] = field(default_factory=list)

    def _dd(self, *ids: int) -> DataDescription | None:
        for want in ids:
            for d in self.descriptions:
                if d.id == want:
                    return d
        return None

    @property
    def name(self) -> str:
        """Long name (Windows 95 / NT / OS/2), else the DOS 8.3 name."""
        for want in (DD_WIN95, DD_NT, DD_OS2, DD_DOS, DD_UNIX):
            d = self._dd(want)
            if d is not None and d.name:
                return d.name
        log.debug("entry: no OS-specific name, falling back to first named description")
        return next((d.name for d in self.descriptions if d.name), "")

    @property
    def is_dir(self) -> bool:
        return bool(self.traversal & T_DIR)

    @property
    def size(self) -> int:
        d = self._dd(DD_DATA)
        return d.area_size if d else 0

    @property
    def attributes(self) -> int:
        """Byte 0 of the Win95/NT/OS2 attributes (bit 0 read-only, 1 hidden,
        2 system, 4 directory, 5 archive), else the DOS attribute byte."""
        d = self._dd(DD_WIN95, DD_NT, DD_OS2, DD_DOS)
        return d.struct[0] if d and d.struct else 0

    @property
    def mtime(self) -> datetime | None:
        """Modify time: Win95/NT/OS2 struct offset 20, DOS offset 1 (8.2.0.1.1.4).

        Section 2.2: seconds since 1970 GMT (+ a tz/us word we ignore).
        """
        d = self._dd(DD_WIN95, DD_NT, DD_OS2)
        off = 20
        if d is None:
            log.debug("entry %r: no Win95/NT/OS2 description, trying DOS for mtime", self.name)
            d, off = self._dd(DD_DOS), 1
        if d is None or len(d.struct) < off + 4:
            log.debug(
                "entry %r: date struct length %s < %d bytes (None = no description); mtime unknown",
                self.name,
                None if d is None else len(d.struct),
                off + 4,
            )
            return None
        (secs,) = struct.unpack_from("<I", d.struct, off)
        if secs == _UNKNOWN_DATE:
            log.debug("entry %r: mtime is the unknown-date marker; returning None", self.name)
        return None if secs == _UNKNOWN_DATE else datetime.fromtimestamp(secs, UTC)


def parse_entry(buf: bytes, off: int) -> tuple[DirEntry, int]:
    """Parse one Directory Entry at ``off``; returns (entry, offset after it)."""
    (rest,) = struct.unpack_from("<H", buf, off)
    end = off + 2 + rest
    if end > len(buf) or rest < 13:
        log.debug(
            "dir entry at %d: size %d (end %d) vs buffer %d / minimum 13; raising",
            off,
            rest,
            end,
            len(buf),
        )
        raise ValueError(f"directory entry at {off} runs past the buffer")
    e = DirEntry(*struct.unpack_from("<QHHB", buf, off + 2))
    p = off + 15
    while p + 14 <= end:
        ddid, area, ssize = struct.unpack_from("<HQH", buf, p)
        st = bytes(buf[p + 12 : p + 12 + ssize])
        (nsize,) = struct.unpack_from("<H", buf, p + 12 + ssize)
        name = bytes(buf[p + 14 + ssize : p + 14 + ssize + nsize]).decode("utf-16-le", "replace")
        e.descriptions.append(DataDescription(ddid, area, st, name))
        p += 14 + ssize + nsize
    return e, end


def parse_directory(buf: bytes) -> list[DirEntry]:
    """All entries of a File Set Directory Section, up to 'last in set'."""
    entries: list[DirEntry] = []
    off = 0
    log.debug("directory: parsing %d bytes", len(buf))
    while off + 2 <= len(buf):
        (rest,) = struct.unpack_from("<H", buf, off)
        if rest == 0:
            log.debug("directory: zero-size entry at %d; end of section", off)
            break
        e, off = parse_entry(buf, off)
        entries.append(e)
        if e.traversal & T_LAST_IN_SET:
            log.debug(
                "directory: entry %d %r is last in set; stopping at %d",
                len(entries) - 1,
                e.name,
                off,
            )
            break
    log.debug("directory: %d entries", len(entries))
    return entries


def directory_paths(entries: list[DirEntry]) -> list[str]:
    """Full path of each entry from the directory's ordering alone (8.2.1).

    Entries come one whole directory level at a time, in preorder: the root
    entry, the root's children up to 'last in directory', then the level of the
    first child directory, and so on depth first. Components are joined with
    '/'; the root entry's own name (e.g. "C:") is the first component.
    """
    if not entries:
        log.debug("directory_paths: no entries; returning []")
        return []
    paths = [entries[0].name]
    pending = [entries[0].name]  # directories whose level is still to come
    i = 1
    while i < len(entries) and pending:
        parent = pending.pop(0)
        subdirs: list[str] = []
        while i < len(entries):
            e = entries[i]
            full = f"{parent}/{e.name}"
            paths.append(full)
            if e.is_dir and not e.traversal & T_EMPTY:
                subdirs.append(full)
            i += 1
            if e.traversal & T_LAST_IN_DIR:
                break
        pending[:0] = subdirs
    if len(paths) < len(entries):
        log.debug(
            "directory_paths: traversal placed %d of %d entries; keeping bare names for the rest",
            len(paths),
            len(entries),
        )
    while len(paths) < len(entries):  # malformed tail: keep the names at least
        paths.append(entries[len(paths)].name)
    return paths


def decode_path_entry(raw: bytes) -> list[str]:
    """Path Entry -> components ([u16 fs id][UTF-16LE] joined by u16 0)."""
    comps: list[str] = []
    i = 0
    while i + 2 <= len(raw):
        i += 2  # native file system ID of this component
        j = i
        while j + 2 <= len(raw) and raw[j : j + 2] != b"\x00\x00":
            j += 2
        comps.append(raw[i:j].decode("utf-16-le", "replace"))
        i = j + 2
    return [c for c in comps if c]


@dataclass(frozen=True)
class EntryLayout:
    """Where an entry's Data Entry, and its file bytes, sit in the data section."""

    index: int
    entry_offset: int  # start of the Data Entry (its signature)
    data_offset: int | None  # start of the DATA area's bytes, if it has one
    data_size: int


def layout(entries: list[DirEntry], volume: bytes | None = None) -> Iterator[EntryLayout]:
    """Locate every entry's file bytes in the data section.

    Entry offsets are running sums of the directory's Data Entry sizes, so a
    damaged entry never shifts the ones after it. Within an entry, the Data
    Areas follow the copied Directory Entry and the Path Entry, one per Data
    Description in directory order (Null types -- DOS, UNIX -- take no space).
    The copy's own 2-byte size field is read from ``volume`` when given;
    otherwise we use the directory's (they agree on the bench tape).
    """
    off = 0
    log.debug(
        "layout: placing %d entries (volume %s)",
        len(entries),
        "given" if volume is not None else "absent",
    )
    for k, e in enumerate(entries):
        copy_size = None
        if (
            volume is not None
            and off + 6 <= len(volume)
            and volume[off : off + 4] == DATA_ENTRY_SIG
        ):
            (copy_size,) = struct.unpack_from("<H", volume, off + 4)
        if copy_size is None and volume is not None:
            # Only interesting when we had a volume to read: without one every
            # entry takes this path by design.
            log.debug(
                "layout: entry %d at %d has no Data Entry signature; using directory size", k, off
            )
        if copy_size is None:
            copy_size = _entry_size(e)
        p = off + 4 + 2 + copy_size + e.path_entry_size
        data_offset = None
        for d in e.descriptions:
            if d.id in NULL_AREA_IDS:
                continue
            if d.id == DD_DATA:
                data_offset = p + 6
            p += 6 + d.area_size
        yield EntryLayout(k, off, data_offset, e.size)
        off += e.data_entry_size


def _entry_size(e: DirEntry) -> int:
    """Re-derive a Directory Entry's size field (bytes after the field itself)."""
    return 13 + sum(14 + len(d.struct) + 2 * len(d.name) for d in e.descriptions)

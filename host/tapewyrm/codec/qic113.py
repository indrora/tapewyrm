"""QIC-113 file-set extraction (DESIGN.md §7.5).

Consumes a Volume Data Area byte stream + its :class:`VtblEntry` and yields a
:class:`FileSet` (directory tree + file bytes).

Basic-DOS (§7) is implemented fully:
  * the *directory section* is concatenated variable-length Directory Entries in
    breadth-first preorder, terminated by ``last-in-dir`` / ``last-in-table``
    attribute bits, from which the tree is reconstructed;
  * the *data section* is ``0x33CC33CC``-anchored Data Entries (signature + a copy
    of the directory entry + a Path Entry + the file bytes), using the signature
    as a resync anchor.

Extended-OS (§8) is implemented to the framing level: ``0x33CC33CC`` + directory
entry + path + Data Areas (``0x66996699`` + 2-byte Data-Area-ID; ID 7 = primary
file bytes). Per-OS attribute structs are summarized / ``TODO``.

The multi-cartridge ``LTLT`` Link Sub-Section is recognized and skipped.
Compressed volumes (VTBL byte 124 bit 7) route through :func:`maybe_decompress`,
a clear ``TODO(bench)`` drop-in for STAC LZS / DCLZ (DESIGN.md §7.5, §9 item 7).
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

from tapewyrm.codec.volume import VtblEntry, decode_short_date
from tapewyrm.types import FileEntry, FileSet

# Signatures (DESIGN.md §7.5) ------------------------------------------------
#: Directory/Data Entry signature 0x33CC33CC, little-endian on tape -> CC 33 CC 33.
SIG_DATA_ENTRY = b"\xcc\x33\xcc\x33"
#: Extended-OS Data Area signature 0x66996699 -> 99 66 99 66 little-endian.
SIG_DATA_AREA = b"\x99\x66\x99\x66"
#: Multi-cartridge link sub-section.
SIG_LTLT = b"LTLT"

# Directory entry attribute bits (Basic-DOS, DESIGN.md §7.5).
ATTR_READ = 0x01
ATTR_WRITE = 0x02
ATTR_EXEC = 0x04
ATTR_HIDDEN = 0x08
ATTR_SYSTEM = 0x10
ATTR_SUBDIR = 0x20
ATTR_LAST_IN_DIR = 0x40
ATTR_LAST_IN_TABLE = 0x80

# Extra-info field: bits 0..5 == 2 => unreadable-at-backup.
EXTRA_UNREADABLE = 2

# Extended-OS Data-Area-IDs (DESIGN.md §7.5).
DATA_AREA_ID_PRIMARY = 7  # primary file bytes
DATA_AREA_ID_AFP_RESOURCE = 6


# ---------------------------------------------------------------------------
# Basic-DOS directory entry
# ---------------------------------------------------------------------------


@dataclass
class DirEntry:
    """A parsed Basic-DOS Directory Entry (DESIGN.md §7.5 Fixed + Name portions)."""

    attrs: int
    modify_date: int  # packed short-date
    data_entry_size: int
    extra_info: int
    name: str
    is_subdir: bool
    last_in_dir: bool
    last_in_table: bool
    unreadable: bool

    @property
    def mtime_epoch(self) -> int | None:
        decoded = decode_short_date(self.modify_date)
        if decoded is None:
            return None
        import calendar

        year, mo, dy, hr, mn, sc = decoded
        try:
            # mo/dy are 0-based in the packed encoding; clamp to valid calendar.
            return calendar.timegm((year, mo + 1, dy + 1, hr, mn, sc, 0, 0, 0))
        except (ValueError, OverflowError):
            return None


def _parse_dir_entry(stream: bytes, off: int) -> tuple[DirEntry, int] | None:
    """Parse one Directory Entry starting at ``off``; return (entry, next_off).

    Fixed Portion: 1B fixed+vendor size, 1B attrs, 4B modify short-date,
    4B data-entry-size, 1B extra-info; optional vendor portion if size > 10;
    Name Portion: 1B name size + ASCII name.
    """
    if off + 11 > len(stream):
        return None
    fixed_vendor_size = stream[off]
    attrs = stream[off + 1]
    modify_date = int.from_bytes(stream[off + 2 : off + 6], "little")
    data_entry_size = int.from_bytes(stream[off + 6 : off + 10], "little")
    extra_info = stream[off + 10]

    # Fixed portion is 11 bytes; if fixed+vendor size > 10 there is a vendor blob
    # following the fixed portion. The size counts from the start of the entry,
    # excluding the size byte itself, so vendor bytes = (fixed_vendor_size - 10).
    cursor = off + 11
    if fixed_vendor_size > 10:
        vendor_len = fixed_vendor_size - 10
        cursor += vendor_len

    if cursor >= len(stream):
        return None
    name_size = stream[cursor]
    cursor += 1
    name = stream[cursor : cursor + name_size].decode("ascii", errors="replace")
    cursor += name_size

    entry = DirEntry(
        attrs=attrs,
        modify_date=modify_date,
        data_entry_size=data_entry_size,
        extra_info=extra_info,
        name=name,
        is_subdir=bool(attrs & ATTR_SUBDIR),
        last_in_dir=bool(attrs & ATTR_LAST_IN_DIR),
        last_in_table=bool(attrs & ATTR_LAST_IN_TABLE),
        unreadable=(extra_info & 0x3F) == EXTRA_UNREADABLE,
    )
    return entry, cursor


# ---------------------------------------------------------------------------
# Directory tree reconstruction (breadth-first preorder)
# ---------------------------------------------------------------------------


@dataclass
class _TreeNode:
    entry: DirEntry
    path: str
    children: list[_TreeNode] = field(default_factory=list)


def _parse_directory_section(stream: bytes) -> tuple[list[DirEntry], int]:
    """Parse all Directory Entries until last-in-table; return (entries, next_off).

    Returns the flat ordered list (breadth-first preorder as written) plus the
    byte offset just past the directory section (the start of the data section in
    Directory-First layout).
    """
    entries: list[DirEntry] = []
    off = 0
    while off < len(stream):
        parsed = _parse_dir_entry(stream, off)
        if parsed is None:
            break
        entry, off = parsed
        entries.append(entry)
        if entry.last_in_table:
            break
    return entries, off


def _build_tree(entries: list[DirEntry]) -> list[_TreeNode]:
    """Reconstruct the directory tree from breadth-first-preorder dir entries.

    The QIC-113 ordering is: the entries of one directory level appear
    consecutively, terminated by ``last_in_dir``; subdirectories are then
    expanded left->right in the same order. We process levels as a queue: each
    subdirectory node, when dequeued, consumes the next run of entries (up to and
    including the next ``last_in_dir``) as its children, building full paths.
    """
    pos = 0

    def consume_run(parent_path: str) -> list[_TreeNode]:
        nonlocal pos
        nodes: list[_TreeNode] = []
        while pos < len(entries):
            entry = entries[pos]
            pos += 1
            full = entry.name if not parent_path else f"{parent_path}/{entry.name}"
            nodes.append(_TreeNode(entry=entry, path=full))
            if entry.last_in_dir or entry.last_in_table:
                break
        return nodes

    # Root level first.
    roots = consume_run("")
    # Breadth-first expansion of subdirectories.
    queue = [n for n in roots if n.entry.is_subdir]
    while queue:
        node = queue.pop(0)
        node.children = consume_run(node.path)
        queue.extend(c for c in node.children if c.entry.is_subdir)
    return roots


def _flatten(nodes: list[_TreeNode]) -> list[_TreeNode]:
    out: list[_TreeNode] = []
    for n in nodes:
        out.append(n)
        out.extend(_flatten(n.children))
    return out


# ---------------------------------------------------------------------------
# Data section (Basic-DOS)
# ---------------------------------------------------------------------------


@dataclass
class DataEntry:
    path: str
    data: bytes
    dir_entry: DirEntry


def _parse_path_entry(stream: bytes, off: int) -> tuple[str, int] | None:
    """Parse a Path Entry: 1B size + null-separated ASCII path."""
    if off >= len(stream):
        return None
    size = stream[off]
    off += 1
    raw = stream[off : off + size]
    off += size
    parts = [p.decode("ascii", errors="replace") for p in raw.split(b"\x00") if p]
    return "/".join(parts), off


def _parse_basic_data_section(stream: bytes, start: int) -> list[DataEntry]:
    """Walk ``0x33CC33CC``-anchored Data Entries from ``start`` (Basic-DOS)."""
    entries: list[DataEntry] = []
    off = start
    n = len(stream)
    while off < n:
        sig_at = stream.find(SIG_DATA_ENTRY, off)
        if sig_at < 0:
            break
        cursor = sig_at + 4
        parsed = _parse_dir_entry(stream, cursor)
        if parsed is None:
            break
        dir_entry, cursor = parsed
        path_parsed = _parse_path_entry(stream, cursor)
        if path_parsed is None:
            break
        path, cursor = path_parsed
        size = dir_entry.data_entry_size
        data = stream[cursor : cursor + size]
        cursor += size
        entries.append(DataEntry(path=path, data=data, dir_entry=dir_entry))
        off = cursor
    return entries


# ---------------------------------------------------------------------------
# Extended-OS (framing-level)
# ---------------------------------------------------------------------------


def _parse_extended_data_section(stream: bytes, start: int) -> list[DataEntry]:
    """Walk Extended-OS Data Entries to the framing level (DESIGN.md §7.5 §8).

    Each entry: ``0x33CC33CC`` + Directory Entry + Path Entry + 0..n Data Areas;
    each Data Area = ``0x66996699`` + 2B Data-Area-ID + data. ID 7 (primary file
    bytes) is collected as the file payload; other IDs are skipped at this level.

    Per-OS attribute structs are summarized: we parse the Basic-style fixed
    portion for naming/sizing and treat the Path Entry as authoritative for the
    file path. TODO: full Extended-OS per-OS attribute decode (DESIGN.md §9 item 7).
    """
    entries: list[DataEntry] = []
    off = start
    n = len(stream)
    while off < n:
        sig_at = stream.find(SIG_DATA_ENTRY, off)
        if sig_at < 0:
            break
        cursor = sig_at + 4
        parsed = _parse_dir_entry(stream, cursor)
        if parsed is None:
            break
        dir_entry, cursor = parsed
        path_parsed = _parse_path_entry(stream, cursor)
        if path_parsed is None:
            break
        path, cursor = path_parsed

        # Collect Data Areas until the next Data Entry signature (or EOF).
        next_entry = stream.find(SIG_DATA_ENTRY, cursor)
        area_end = next_entry if next_entry >= 0 else n
        primary = b""
        acursor = cursor
        while acursor < area_end:
            area_sig = stream.find(SIG_DATA_AREA, acursor, area_end)
            if area_sig < 0:
                break
            apos = area_sig + 4
            if apos + 2 > area_end:
                break
            (area_id,) = struct.unpack_from("<H", stream, apos)
            apos += 2
            # Data Area length is not framed at this level (per-OS struct);
            # for the primary blob we take the remaining bytes up to the next
            # area/entry boundary. TODO: exact per-ID length fields (§8).
            next_area = stream.find(SIG_DATA_AREA, apos, area_end)
            blob_end = next_area if next_area >= 0 else area_end
            blob = stream[apos:blob_end]
            if area_id == DATA_AREA_ID_PRIMARY:
                primary = blob
            acursor = blob_end

        entries.append(DataEntry(path=path, data=primary, dir_entry=dir_entry))
        off = area_end
    return entries


# ---------------------------------------------------------------------------
# OS detection (DESIGN.md §7.5)
# ---------------------------------------------------------------------------


def is_extended_os(vtbl: VtblEntry) -> bool:
    """Detect Extended-OS vs Basic-DOS from the VTBL entry (DESIGN.md §7.5).

    Extended-OS if byte 56 bit 0 (vendor-specific) is set **and** the vendor
    extension words at offsets 58/60 read 113 / 7; Basic-DOS if byte 125 == 1
    (Format & OS Type = DOS) or otherwise.
    """
    raw = vtbl.raw
    if len(raw) >= 62 and (vtbl.flags & 0x01):
        ext1 = int.from_bytes(raw[58:60], "little")
        ext2 = int.from_bytes(raw[60:62], "little")
        if ext1 == 113 and ext2 == 7:
            return True
    if len(raw) >= 126 and raw[125] == 1:
        return False  # explicit Basic-DOS
    return False


# ---------------------------------------------------------------------------
# Decompression hook (TODO(bench) drop-in)
# ---------------------------------------------------------------------------


def maybe_decompress(stream: bytes, vtbl: VtblEntry) -> bytes:
    """Decompress the Volume Data Area if the VTBL flags compression.

    TODO(bench), DESIGN.md §7.5 / §9 item 7: compressed volumes (VTBL byte 124
    bit 7) frame STAC LZS (compression code 1) or DCLZ/ALDC (QIC-122/130/154)
    Compression Frames. The framing is described in §7.5 but the LZS/DCLZ codec
    itself is a drop-in not specified here. This hook is the single integration
    point: parse the Frame headers and call the codec. Until that drop-in exists
    we pass the stream through unchanged and rely on the caller noting
    ``FileSet.compressed`` so the user knows the bytes are still compressed.
    """
    if not vtbl.compressed:
        return stream
    # TODO(bench): parse Compression Extents/Frames and invoke STAC LZS / DCLZ.
    return stream


# ---------------------------------------------------------------------------
# Top-level extraction
# ---------------------------------------------------------------------------


def _strip_link_subsection(stream: bytes) -> bytes:
    """Recognize and drop a trailing multi-cartridge ``LTLT`` sub-section."""
    idx = stream.find(SIG_LTLT)
    if idx >= 0:
        return stream[:idx]
    return stream


def extract(stream: bytes, vtbl: VtblEntry) -> FileSet:
    """Extract a :class:`FileSet` from a Volume Data Area byte stream.

    Handles Directory-First vs Directory-Last layout (VTBL byte 56 bit 5),
    Basic-DOS vs Extended-OS, the ``LTLT`` skip, and the compression hook.
    """
    extended = is_extended_os(vtbl)
    fileset = FileSet(
        name=vtbl.description or ("C:" if not extended else "volume"),
        compressed=vtbl.compressed,
        extended_os=extended,
    )

    stream = maybe_decompress(stream, vtbl)
    stream = _strip_link_subsection(stream)

    if vtbl.directory_last:
        # Directory-Last: [Data Section][gap][Directory Section]. Locate the
        # directory by subtracting Directory Section Size (rounded up to whole
        # segments) from the end. At the framing level we scan from there.
        dir_start = _directory_last_offset(stream, vtbl)
        dir_entries, _ = _parse_directory_section(stream[dir_start:])
        data_section_start = 0
    else:
        dir_entries, dir_end = _parse_directory_section(stream)
        data_section_start = dir_end

    # Build the tree (gives directory nodes + full paths) and add directories.
    tree = _build_tree(dir_entries)
    flat = _flatten(tree)
    dirs_by_path: dict[str, _TreeNode] = {n.path: n for n in flat}
    for node in flat:
        if node.entry.is_subdir:
            fileset.files.append(
                FileEntry(
                    path=node.path,
                    size=0,
                    attrs=node.entry.attrs,
                    mtime=node.entry.mtime_epoch,
                    is_dir=True,
                    unreadable_at_backup=node.entry.unreadable,
                )
            )

    # Walk the data section for file bytes.
    if extended:
        data_entries = _parse_extended_data_section(stream, data_section_start)
    else:
        data_entries = _parse_basic_data_section(stream, data_section_start)

    for de in data_entries:
        # Prefer the tree node's metadata if the path matches; fall back to the
        # data entry's own copy of the directory entry.
        match = dirs_by_path.get(de.path)
        meta = match.entry if match is not None else de.dir_entry
        fileset.files.append(
            FileEntry(
                path=de.path,
                size=len(de.data),
                attrs=meta.attrs,
                mtime=meta.mtime_epoch,
                data=de.data,
                is_dir=False,
                unreadable_at_backup=meta.unreadable,
            )
        )

    return fileset


def _directory_last_offset(stream: bytes, vtbl: VtblEntry) -> int:
    """Best-effort start offset of a Directory-Last directory section.

    Exact location is ``Ending Segment - ceil(Directory Section Size / segment)``
    in segment space (DESIGN.md §7.5); at the framing level we fall back to
    locating the first plausible directory entry by scanning for the data-entry
    signature's *absence* — i.e. we search backward from the end for a run that
    parses as directory entries. As a robust default we look for the last
    occurrence region after the data section. If the directory size is known and
    fits, subtract it from the stream length.
    """
    size = vtbl.dir_section_size
    if 0 < size <= len(stream):
        return len(stream) - size
    # Fallback: assume the directory begins right after the final data entry.
    last_sig = stream.rfind(SIG_DATA_ENTRY)
    if last_sig < 0:
        return 0
    # Skip past the last data entry's anchor; the directory follows the gap.
    return last_sig

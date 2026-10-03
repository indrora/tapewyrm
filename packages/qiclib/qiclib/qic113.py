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

import logging
import struct
from dataclasses import dataclass, field

from qiclib.tape_profile import SEGMENT_DATA_BYTES
from qiclib.types import FileEntry, FileSet
from qiclib.volume import VtblEntry, decode_short_date

log = logging.getLogger(__name__)

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
    entry_len: int = 0  # bytes this entry occupies (size byte through name)

    @property
    def mtime_epoch(self) -> int | None:
        decoded = decode_short_date(self.modify_date)
        if decoded is None:
            log.debug("entry %r: no modify date (0x%08x); mtime None", self.name, self.modify_date)
            return None
        import calendar

        year, mo, dy, hr, mn, sc = decoded
        try:
            # decode_short_date already returns a 1-based month and day.
            return calendar.timegm((year, mo, dy, hr, mn, sc, 0, 0, 0))
        except (ValueError, OverflowError):
            log.debug("entry %r: date %r is out of range; mtime None", self.name, decoded)
            return None


def _parse_dir_entry(stream: bytes, off: int) -> tuple[DirEntry, int] | None:
    """Parse one Directory Entry starting at ``off``; return (entry, next_off).

    Fixed Portion: 1B fixed+vendor size, 1B attrs, 4B modify short-date,
    4B data-entry-size, 1B extra-info; optional vendor portion if size > 10;
    Name Portion: 1B name size + ASCII name.

    The size byte is what places the Name Portion: it always starts at
    ``off + 1 + size``. A size of **9** means a fixed portion without the
    extra-info byte: the "MTN" tapes write that (QIC-113 Basic-DOS otherwise;
    captures/old-connor.twti, 1998). Their extra-info reads as 0 (readable).
    """
    if off + 10 > len(stream):
        log.debug(
            "dir entry at %d: fixed portion runs past %d-byte stream; stopping", off, len(stream)
        )
        return None
    fixed_vendor_size = stream[off]
    attrs = stream[off + 1]
    modify_date = int.from_bytes(stream[off + 2 : off + 6], "little")
    data_entry_size = int.from_bytes(stream[off + 6 : off + 10], "little")
    if fixed_vendor_size < 10:
        # No extra-info byte (size 9): byte off+10 is already the name size.
        log.debug(
            "dir entry at %d: fixed size %d < 10; no extra-info byte (MTN-style), reading 0",
            off,
            fixed_vendor_size,
        )
        extra_info = 0
    else:
        extra_info = stream[off + 10]

    # The size counts the fixed portion (and any vendor blob after it) excluding
    # the size byte itself: 10 for the spec's fixed portion, more with a vendor
    # blob, 9 without extra-info. The Name Portion follows all of it.
    cursor = off + 1 + fixed_vendor_size

    if cursor >= len(stream):
        log.debug(
            "dir entry at %d: name portion at %d is past %d-byte stream (vendor size %d); stopping",
            off,
            cursor,
            len(stream),
            fixed_vendor_size,
        )
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
        entry_len=cursor - off,
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
    log.debug("directory: parsing section from %d-byte stream", len(stream))
    while off < len(stream):
        if stream[off] == 0:
            # A size byte of 0 is no entry at all: zero padding after the last
            # one. MTN tapes end their directory that way, without setting
            # last-in-table, and pad the section to a whole number of segments.
            log.debug(
                "directory: zero size byte at %d (padding); section ends with %d entries",
                off,
                len(entries),
            )
            break
        parsed = _parse_dir_entry(stream, off)
        if parsed is None:
            log.debug(
                "directory: unparseable entry at %d; stopping with %d entries", off, len(entries)
            )
            break
        entry, off = parsed
        entries.append(entry)
        if entry.last_in_table:
            log.debug("directory: %r is last in table; section ends at %d", entry.name, off)
            break
    log.debug("directory: %d entries, section ends at %d", len(entries), off)
    return entries, off


def _build_tree(entries: list[DirEntry]) -> list[_TreeNode]:
    """Reconstruct the directory tree from the directory table's entry order.

    QIC-113 Rev G §7.1.4: each directory *level* (all of its entries) appears
    as one run ending in ``last_in_dir``; after a level come its subdirectories'
    levels, **each fully expanded, left to right, before the next sibling**.
    The spec calls this "breadth-first, preorder", but its worked example
    (ROOT, COMEXE, STUFF, LANGUAGE, APL, C, BASIC, TEXT) is depth-first over
    levels, and so are real tapes (old-connor's data section follows the same
    order). This used to expand levels with a queue, which mis-parents every
    entry below the first two levels.

    An **empty directory has no level** at all: its entry's Data Entry size is
    non-zero (the size of a data header) where a non-empty one's is 0 (§7.1.3),
    so it consumes no run. Missing that shifts every later level by one.
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

    n_empty = 0

    def expand(nodes: list[_TreeNode]) -> None:
        nonlocal n_empty
        for node in nodes:
            if not node.entry.is_subdir:
                continue
            if node.entry.data_entry_size != 0:
                n_empty += 1  # an empty directory: no level follows for it
                continue
            if pos >= len(entries):
                log.debug("tree: directory %r has no level left in the table", node.path)
                continue
            node.children = consume_run(node.path)
            expand(node.children)

    roots = consume_run("")
    expand(roots)
    if pos < len(entries):
        log.debug(
            "tree: %d of %d entries left after the last level", len(entries) - pos, len(entries)
        )
    log.debug("tree: %d entries, %d empty directories", pos, n_empty)
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
    # Where ``data`` starts in the stream that was walked, so a caller holding
    # the volume's hole map can tell which files lost bytes. None = unknown.
    offset: int | None = None


def _parse_path_entry(stream: bytes, off: int) -> tuple[str, int] | None:
    """Parse a Path Entry: 1B size + null-separated ASCII path."""
    if off >= len(stream):
        log.debug("path entry at %d is past %d-byte stream; stopping", off, len(stream))
        return None
    size = stream[off]
    off += 1
    raw = stream[off : off + size]
    off += size
    parts = [p.decode("ascii", errors="replace") for p in raw.split(b"\x00") if p]
    return "/".join(parts), off


# QIC-113 Rev G §2.5: a Data Entry size that was not known when the entry was
# written (the copy in the data section can precede the end of the file).
_SIZE_UNKNOWN = 0xFFFFFFFF


def _plausible_entry(stream: bytes, sig_at: int, entry: DirEntry, data_len: int | None) -> bool:
    """Is the Data Entry at ``sig_at`` real, or the signature bytes inside file data?

    The 4-byte signature can occur in file contents (old-connor's has one in a
    bitmap); trusting it there reads a binary "name" and a multi-GB size, and
    the walk jumps past every real entry behind it. A real entry has a sane
    size byte (9 = MTN, 10 = spec, more = vendor blob), a printable name, and
    a data length that is not negative (``None`` = unknown, checked later).
    """
    size_byte = stream[sig_at + 4]
    return (
        9 <= size_byte <= 64
        and 0 < len(entry.name)
        and all(0x20 <= ord(c) < 0x7F for c in entry.name)
        and (data_len is None or data_len >= 0)
    )


def _parse_basic_data_section(stream: bytes, start: int) -> list[DataEntry]:
    """Walk ``0x33CC33CC``-anchored Data Entries from ``start`` (Basic-DOS).

    Per QIC-113 Rev G §7.1.3 / §7.2 (the "MTN" tapes are the first real
    Basic-DOS volumes this has met, and they follow the spec):

    * the Data Entry **size counts the data header too** -- signature,
      directory-entry copy and path entry -- plus the file's bytes; a size of
      0xFFFFFFFF means unknown, and the data then runs to the next signature;
    * the **Path Entry is the directory** the item is in (empty for the root);
      the item's own name comes from the directory-entry copy;
    * **empty directories** appear here as a header with no data.

    Implausible signature hits (see :func:`_plausible_entry`) are skipped and
    the search resumes one byte later, so a false match costs nothing.
    Returned paths are full paths (directory + name).
    """
    entries: list[DataEntry] = []
    off = start
    n = len(stream)
    n_false = 0  # false signature hits: counted, logged once after the walk
    log.debug("basic data: walking data entries from %d of %d bytes", start, n)
    while off < n:
        sig_at = stream.find(SIG_DATA_ENTRY, off)
        if sig_at < 0:
            log.debug("basic data: no data entry signature after %d; done", off)
            break
        cursor = sig_at + 4
        parsed = _parse_dir_entry(stream, cursor)
        if parsed is None:
            log.debug("basic data: bad directory entry copy at %d; stopping", cursor)
            break
        dir_entry, cursor = parsed
        path_parsed = _parse_path_entry(stream, cursor)
        if path_parsed is None:
            log.debug("basic data: no path entry for %r at %d; stopping", dir_entry.name, cursor)
            break
        dir_path, cursor = path_parsed
        header_len = cursor - sig_at
        if dir_entry.data_entry_size == _SIZE_UNKNOWN:
            data_len = None
        elif dir_entry.is_subdir:
            data_len = 0  # an empty directory: data header only
        else:
            data_len = dir_entry.data_entry_size - header_len
        if not _plausible_entry(stream, sig_at, dir_entry, data_len):
            n_false += 1
            off = sig_at + 1
            continue
        if data_len is None:
            nxt = stream.find(SIG_DATA_ENTRY, cursor)
            data_len = (nxt if nxt >= 0 else n) - cursor
            log.debug(
                "basic data: %r size unknown (0xFFFFFFFF); taking %d bytes to the next signature",
                dir_entry.name,
                data_len,
            )
        if cursor + data_len > n:
            log.debug(
                "basic data: %r wants %d bytes at %d, stream has %d; truncating",
                dir_entry.name,
                data_len,
                cursor,
                n,
            )
            data_len = n - cursor
        path = f"{dir_path}/{dir_entry.name}" if dir_path else dir_entry.name
        if dir_entry.is_subdir:
            log.debug("basic data: empty directory %r (header only)", path)
        else:
            data = stream[cursor : cursor + data_len]
            entries.append(DataEntry(path=path, data=data, dir_entry=dir_entry, offset=cursor))
        off = cursor + data_len
    log.debug("basic data: %d data entries, %d false signature hits skipped", len(entries), n_false)
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
    log.debug("extended data: walking data entries from %d of %d bytes", start, n)
    while off < n:
        sig_at = stream.find(SIG_DATA_ENTRY, off)
        if sig_at < 0:
            log.debug("extended data: no data entry signature after %d; done", off)
            break
        cursor = sig_at + 4
        parsed = _parse_dir_entry(stream, cursor)
        if parsed is None:
            log.debug("extended data: bad directory entry copy at %d; stopping", cursor)
            break
        dir_entry, cursor = parsed
        path_parsed = _parse_path_entry(stream, cursor)
        if path_parsed is None:
            log.debug("extended data: no path entry for %r at %d; stopping", dir_entry.name, cursor)
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
                log.debug(
                    "extended data: %r area at %d has no room for its ID; ending entry",
                    path,
                    area_sig,
                )
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
    log.debug("extended data: %d data entries", len(entries))
    return entries


# ---------------------------------------------------------------------------
# OS detection (DESIGN.md §7.5)
# ---------------------------------------------------------------------------


def is_extended_os(vtbl: VtblEntry) -> bool:
    """Detect Extended-OS vs Basic-DOS from the VTBL entry (DESIGN.md §7.5).

    Extended-OS if byte 56 bit 0 (vendor-specific) is set **and** the vendor
    extension words at offsets 58/60 read 113 / a revision; Basic-DOS otherwise.

    Byte 125 (Format & OS Type) is deliberately *not* a marker either way. On
    QIC-40/80 tapes QIC-113 Rev G §6 makes the vendor bit plus the 113
    signature the only mark of an extended volume, and the bench tape's
    extended volume has OS type 6 only *behind* that mark. Reading "type != 1"
    as Extended would misroute any plain QIC-80 volume with an odd byte 125,
    and "type == 1" changes nothing (it is the default). This used to have a
    separate byte-125 branch returning False -- dead code, now just logged.
    """
    raw = vtbl.raw
    if len(raw) >= 62 and (vtbl.flags & 0x01):
        ext1 = int.from_bytes(raw[58:60], "little")
        ext2 = int.from_bytes(raw[60:62], "little")
        # 58/59 = 113 marks QIC-113; 60/61 is its revision (QIC-113 Rev G
        # section 6: F = 6, G = 7). Any revision counts -- this used to demand
        # exactly 7 and so misread the bench tape's Rev F volume as Basic DOS.
        if ext1 == 113 and ext2 >= 1:
            log.debug(
                "vtbl %r: QIC-113 signature %d rev %d; Extended-OS", vtbl.description, ext1, ext2
            )
            return True
        log.debug(
            "vtbl %r: vendor bit set but ext words %d/%d; not Extended-OS",
            vtbl.description,
            ext1,
            ext2,
        )
    log.debug(
        "vtbl %r: no Extended-OS marker (OS type byte %r); Basic-DOS",
        vtbl.description,
        vtbl.os_type,
    )
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
        log.debug(
            "vtbl %r: compressed=%s; passing stream through", vtbl.description, vtbl.compressed
        )
        return stream
    # TODO(bench): parse Compression Extents/Frames and invoke STAC LZS / DCLZ.
    log.debug(
        "vtbl %r: compressed (code %s) but no codec hooked in; passing %d bytes through as-is",
        vtbl.description,
        vtbl.compression_code,
        len(stream),
    )
    return stream


# ---------------------------------------------------------------------------
# Top-level extraction
# ---------------------------------------------------------------------------


# A Link Section is 'LTLT', a media sequence number and the media ending
# offsets (QIC-113 Rev G §7, layout summary): a few dozen bytes. Anything
# further from the end than this is the four letters inside a file.
_LINK_SECTION_MAX = 1024


def _strip_link_subsection(stream: bytes) -> bytes:
    """Recognize and drop a trailing multi-cartridge ``LTLT`` sub-section.

    Only an ``LTLT`` near the end of the volume (ignoring zero padding) counts.
    This used to cut at the *first* ``LTLT`` anywhere, and old-connor's volume
    has those bytes in a file 46 MB in: a third of the volume vanished.
    """
    end = len(stream.rstrip(b"\x00"))
    idx = stream.rfind(SIG_LTLT, max(0, end - _LINK_SECTION_MAX), end)
    if idx >= 0:
        log.debug("LTLT link sub-section at %d of %d; truncating", idx, len(stream))
        return stream[:idx]
    log.debug(
        "no LTLT link sub-section in the last %d bytes; keeping the stream", _LINK_SECTION_MAX
    )
    return stream


def extract(stream: bytes, vtbl: VtblEntry, *, dir_offset: int | None = None) -> FileSet:
    """Extract a :class:`FileSet` from a Volume Data Area byte stream.

    Handles Directory-First vs Directory-Last layout (VTBL byte 56 bit 5),
    Basic-DOS vs Extended-OS, the ``LTLT`` skip, and the compression hook.
    """
    log.debug("extract: %r, %d-byte volume data area", vtbl.description, len(stream))
    extended = is_extended_os(vtbl)
    fileset = FileSet(
        name=vtbl.description or ("C:" if not extended else "volume"),
        compressed=vtbl.compressed is True,  # None (vendor-specific) = not known
        extended_os=extended,
    )

    stream = maybe_decompress(stream, vtbl)
    stream = _strip_link_subsection(stream)

    if vtbl.directory_last:
        # Directory-Last: [Data Section][Segment Gap][Directory Section]
        # (QIC-113 Rev G §7); see _directory_last_offset for how it is found.
        # ``dir_offset`` is the exact start when the caller knows it (an
        # uncompressed TWVL records it from the segment boundaries; see
        # qiclib.extract); otherwise, or if it does not parse, locate it.
        if dir_offset is not None and _plausible_dir_entry(stream, dir_offset):
            dir_start = dir_offset
        else:
            if dir_offset is not None:
                log.debug("extract: given directory offset %d does not parse; locating", dir_offset)
            dir_start = _directory_last_offset(stream, vtbl)
        log.debug("extract: directory-last layout, directory at %d", dir_start)
        dir_entries, _ = _parse_directory_section(stream[dir_start:])
        data_section_start = 0
        # The data walk stops at the directory: an entry of unknown size
        # (0xFFFFFFFF) would otherwise run on through the gap and directory.
        data_stream = stream[:dir_start]
    else:
        log.debug("extract: directory-first layout")
        dir_entries, dir_end = _parse_directory_section(stream)
        data_section_start = dir_end
        data_stream = stream

    # Build the tree (gives directory nodes + full paths) and add directories.
    log.debug("extract: building tree from %d directory entries", len(dir_entries))
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
        data_entries = _parse_extended_data_section(data_stream, data_section_start)
    else:
        data_entries = _parse_basic_data_section(data_stream, data_section_start)

    for de in data_entries:
        # Prefer the tree node's metadata if the path matches; fall back to the
        # data entry's own copy of the directory entry.
        match = dirs_by_path.get(de.path)
        if match is None:
            log.debug(
                "extract: %r not in the directory tree; using its data entry's metadata", de.path
            )
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
                offset=de.offset,
            )
        )

    # Files the directory lists but whose data entry was never found -- its
    # header fell in a lost segment. Keep them, marked lost, so a listing or a
    # tar still shows every file the backup had (Basic-DOS only: the extended
    # walk here is framing-level, see _parse_extended_data_section).
    if not extended:
        found = {de.path for de in data_entries}
        n_lost = 0
        for node in flat:
            if node.entry.is_subdir or node.path in found:
                continue
            n_lost += 1
            fileset.files.append(
                FileEntry(
                    path=node.path,
                    size=_file_bytes(node.entry, node.path),
                    attrs=node.entry.attrs,
                    mtime=node.entry.mtime_epoch,
                    is_dir=False,
                    unreadable_at_backup=node.entry.unreadable,
                    lost=True,
                )
            )
        log.debug("extract: %d directory files have no data entry; listed as lost", n_lost)

    return fileset


def _file_bytes(entry: DirEntry, path: str) -> int:
    """A file's byte count from its directory entry, whose size includes the data header.

    QIC-113 Rev G §7.1.3: header = signature (4) + the directory entry copy
    (size byte + fixed/vendor + name size byte + name) + the path entry (size
    byte + directory path, separators as NULs: same length as with '/').
    """
    if entry.data_entry_size == _SIZE_UNKNOWN:
        log.debug("%r: size unknown (0xFFFFFFFF); reporting 0 bytes", path)
        return 0
    directory = path.rpartition("/")[0]
    header = 4 + entry.entry_len + 1 + len(directory)
    return max(0, entry.data_entry_size - header)


def _plausible_dir_entry(stream: bytes, off: int) -> bool:
    """Does a Basic-DOS Directory Entry plausibly start at ``off``?

    Same test as :func:`_plausible_entry` applies to a data-entry header: a
    sane size byte (9 = MTN, 10 = spec, more = vendor blob) and a non-empty
    printable name. Zero gap padding fails on the size byte; a data-entry
    signature fails too (its first byte, 0xCC, is no size byte).
    """
    if not 0 <= off < len(stream) or not 9 <= stream[off] <= 64:
        return False
    parsed = _parse_dir_entry(stream, off)
    if parsed is None:
        return False
    name = parsed[0].name
    return 0 < len(name) and all(0x20 <= ord(c) < 0x7F for c in name)


def _round_up_to_segment(off: int) -> int:
    """``off`` rounded up to the next whole segment of volume bytes."""
    return -(-off // SEGMENT_DATA_BYTES) * SEGMENT_DATA_BYTES


def _directory_last_offset(stream: bytes, vtbl: VtblEntry) -> int:
    """Start offset of a Directory-Last directory section in ``stream``.

    QIC-113 Rev G §7 / §8: a Directory-Last volume is
    ``File Set Data Section + Segment Gap + File Set Directory Section``, and
    §7.1 / §3.26 put the directory on a segment boundary. §7.1.1 locates it
    in *segment* space, counting back from the VTBL's Ending Segment by the
    Directory Section Size rounded up to whole segments. ``stream`` does not
    end at the Ending Segment (the caller sizes it from the VTBL's section
    sizes, and a truncated capture ends anywhere), so we count forward from
    the other end instead: the data section starts at 0 and is
    ``data_section_size`` bytes long (VTBL 96-103), and the directory is the
    first segment boundary at or after it.

    That holds for uncompressed volumes, whose segments are laid end to end
    so the gap is really in the stream. A compressed volume's stream is built
    from the extents' uncompressed offsets, where the gap takes no room: on
    the bench tape ("jc", CMS, Rev F) the directory sits at exactly
    ``data_section_size``. So both positions are candidates, the likelier one
    first, and the first that parses as a Directory Entry wins.

    The old code took ``len(stream) - dir_section_size``, which is only right
    when the stream ends exactly at the end of the directory section, and fell
    back to the *start* of the last data entry's signature, which parses as
    garbage (size byte 0xCC). Without usable sizes we now walk the data
    section (:func:`_parse_basic_data_section`, which honours each entry's
    size) and take the end of the last data entry, then the segment boundary
    after it.

    Note: §7.1.2 also describes a 4-byte "Directory Section Ending Offset"
    ahead of the directory; jc's directory starts straight with entries, so
    that is only read as part of the linked-tape structure, which is not
    supported here.
    """
    data_size = vtbl.data_section_size
    candidates: list[int] = []
    if data_size:
        exact, aligned = data_size, _round_up_to_segment(data_size)
        # Uncompressed: the gap is physically in the stream (see above).
        candidates += [aligned, exact] if vtbl.compressed is False else [exact, aligned]
        log.debug(
            "directory-last: data section %d bytes; trying directory at %s",
            data_size,
            candidates,
        )
    else:
        log.debug(
            "directory-last: data section size %r unusable; walking the data section",
            data_size,
        )
        walked = _parse_basic_data_section(stream, 0)
        if walked:
            last = walked[-1]
            end = (last.offset or 0) + len(last.data)
            candidates += [end, _round_up_to_segment(end)]
            log.debug(
                "directory-last: last data entry %r ends at %d; trying directory at %s",
                last.path,
                end,
                candidates,
            )
        else:
            log.debug("directory-last: no data entries found by the walk")
    for off in candidates:
        if _plausible_dir_entry(stream, off):
            log.debug("directory-last: directory entry found at %d", off)
            return off
        log.debug("directory-last: no plausible directory entry at %d", off)
    fallback = candidates[0] if candidates else len(stream)
    log.debug(
        "directory-last: no candidate parsed; assuming directory at %d of %d bytes",
        fallback,
        len(stream),
    )
    return fallback

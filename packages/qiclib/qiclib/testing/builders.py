"""Synthesize qiclib test fixtures: header segments, volume tables, QIC-113 volumes.

These mirror the on-tape structures (DESIGN.md §7.3, §7.5) closely enough to
exercise the parsers end-to-end with no hardware.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, replace

from tapewyrm_archive.twvl import Volume

from qiclib import rs
from qiclib.geometry import FTK_PER_SIDE, seg_to_coord
from qiclib.types import RawSector, Segment
from qiclib.volume import FPR_SIGNATURE, SIG_VTBL, VTBL_ENTRY_LEN

SECTOR = 1024


def make_segment_from_sectors(
    tpt: int, tps: int, seg_abs: int, sector_datas: list[bytes]
) -> Segment:
    """Build a Segment whose data rows hold the given 29 sector payloads.

    Pads to 29 data sectors + 3 zero parity sectors (all CRC-good). Each payload
    is padded/truncated to 1024 bytes.
    """
    seg = Segment(tpt=tpt, tps=tps, seg=seg_abs)
    for slot in range(32):
        if slot < len(sector_datas):
            payload = sector_datas[slot]
        else:
            payload = b""
        payload = payload.ljust(SECTOR, b"\x00")[:SECTOR]
        seg.sectors[slot] = RawSector(
            fsd=0,
            ftk=0,
            fsc=slot + 1,
            data=payload,
            id_crc_ok=True,
            data_crc_ok=True,
            deleted=False,
        )
    return seg


def make_short_date(year, mo, dy, hr, mn, sc) -> int:
    """Pack a calendar date (month 1-12, day 1-31) as Rev N stores it (0-based)."""
    rest = sc + 60 * (mn + 60 * (hr + 24 * ((dy - 1) + 31 * (mo - 1))))
    return ((year - 1970) << 25) | (rest & 0x01FFFFFF)


def build_format_parameter_record(
    *,
    segments_per_track: int = 207,
    tracks: int = 28,
    max_fsd: int = 5,
    max_ftk: int = 254,
    max_fsc: int = 128,
    tape_name: str = "TESTTAPE",
    format_date: int = 0,
    bad_lsns: list[int] | None = None,
    bad_segments: list[int] | None = None,
    header_seg: int = 0,
    dup_header_seg: int = 1,
    first_data_seg: int = 2,
) -> bytes:
    """Build sector 0 of the header segment: FPR + bad-sector map.

    The BSM proper begins at offset 256 (after the 256-byte format parameter
    record, QIC-80-MC Rev N §7.1), as
    3-byte ascending 1-based LSN entries, ``0`` terminated; high bit of MSB set
    => whole segment bad.
    """
    sec = bytearray(SECTOR)
    sec[0:4] = FPR_SIGNATURE
    sec[4] = 0x04  # variable-length format code
    struct.pack_into("<H", sec, 24, segments_per_track)
    sec[26] = tracks
    sec[27] = max_fsd
    sec[28] = max_ftk
    sec[29] = max_fsc
    name = tape_name.encode("ascii")[:44]
    sec[30 : 30 + len(name)] = name
    struct.pack_into("<I", sec, 14, format_date)  # Rev N: most recent format
    struct.pack_into("<HHH", sec, 6, header_seg, dup_header_seg, first_data_seg)  # 6-11

    # Bad-sector map at offset 256.
    off = 256
    entries: list[tuple[int, bool]] = []
    for lsn0 in bad_lsns or []:
        entries.append((lsn0 + 1, False))  # store 1-based
    for seg_abs in bad_segments or []:
        # A whole-bad segment is encoded by any LSN in it with the seg-flag set.
        lsn1 = seg_abs * 32 + 1
        entries.append((lsn1, True))
    entries.sort()
    for lsn1, seg_flag in entries:
        b0 = lsn1 & 0xFF
        b1 = (lsn1 >> 8) & 0xFF
        b2 = (lsn1 >> 16) & 0x7F
        if seg_flag:
            b2 |= 0x80
        sec[off : off + 3] = bytes([b0, b1, b2])
        off += 3
    # terminator already zero.
    return bytes(sec)


def build_header_segment(seg_abs: int = 0, **fpr_kwargs) -> Segment:
    """A header segment whose sector 0 is the format parameter record."""
    fpr = build_format_parameter_record(**fpr_kwargs)
    return make_segment_from_sectors(0, 0, seg_abs, [fpr])


def build_vtbl_entry(
    *,
    signature: bytes = SIG_VTBL,
    start_seg: int,
    end_seg: int,
    description: str = "C:",
    flags: int = 0,
    os_type: int = 1,
    compressed: bool = False,
    dir_section_size: int = 0,
    date: int = 0,
) -> bytes:
    """Build one 128-byte VTBL entry (``date`` is a packed short date, bytes 52-55)."""
    rec = bytearray(VTBL_ENTRY_LEN)
    rec[0:4] = signature
    struct.pack_into("<H", rec, 4, start_seg)  # Rev N §8: words
    struct.pack_into("<H", rec, 6, end_seg)
    desc = description.encode("ascii")[:44]
    rec[8 : 8 + len(desc)] = desc
    struct.pack_into("<I", rec, 52, date)
    rec[56] = flags
    struct.pack_into("<I", rec, 92, dir_section_size)
    if compressed:
        rec[124] |= 0x80
    rec[125] = os_type
    return bytes(rec)


def build_volume_table_segment(seg_abs: int, vtbl_entries: list[bytes]) -> Segment:
    """A volume-table segment: its data area begins with VTBL entries."""
    blob = b"".join(vtbl_entries)
    # Spread across data sectors (the parser reads the concatenated data area).
    sectors: list[bytes] = []
    for i in range(0, len(blob), SECTOR):
        sectors.append(blob[i : i + SECTOR])
    if not sectors:
        sectors = [b""]
    tpt = seg_abs // 207
    tps = seg_abs % 207
    return make_segment_from_sectors(tpt, tps, seg_abs, sectors)


def with_parity(seg: Segment) -> Segment:
    """Give a segment real RS parity in slots 29-31 (no BSM exclusions).

    Encoding a systematic code is solving for 3 erased parity symbols, so the
    column decoder does it (same trick as tests/test_rs_segment.py). Builders
    above leave the parity sectors zero, which only works while nothing needs
    correcting.
    """
    rows = [sec.data if sec is not None else bytes(SECTOR) for sec in seg.sectors]
    n = Segment.SECTORS
    cols = [[rows[r][c] for r in range(n)] for c in range(SECTOR)]
    cols = [rs.correct_codeword(col, [n - 3, n - 2, n - 1], n) for col in cols]
    for r in range(n - 3, n):
        seg.sectors[r] = RawSector(
            fsd=0, ftk=0, fsc=r + 1, data=bytes(cols[c][r] for c in range(SECTOR)),
            id_crc_ok=True, data_crc_ok=True, deleted=False,
        )  # fmt: skip
    return seg


def segment_raw_sectors(seg: Segment, ftk_per_side: int = FTK_PER_SIDE) -> list[RawSector]:
    """The segment's sectors as a capture would yield them: each stamped with
    the (FSD, FTK, FSC) ID that places it back at ``seg.seg``.

    Lets tests feed ``place``/``locate_header`` exactly as decoded flux would.
    """
    fsd, ftk, fsc0 = seg_to_coord(seg.seg, ftk_per_side)
    return [
        replace(sec, fsd=fsd, ftk=ftk, fsc=fsc0 + slot)
        for slot, sec in enumerate(seg.sectors)
        if sec is not None
    ]


# ---------------------------------------------------------------------------
# QIC-113 Basic-DOS volume byte stream
# ---------------------------------------------------------------------------


def build_dir_entry(
    name: str,
    *,
    attrs: int,
    modify_date: int = 0,
    data_entry_size: int = 0,
    extra_info: int = 0,
    vendor: bytes = b"",
) -> bytes:
    """Build a Basic-DOS Directory Entry (Fixed + optional vendor + Name)."""
    name_b = name.encode("ascii")
    fixed_vendor_size = 10 + len(vendor)  # size counts fixed(10) + vendor
    out = bytearray()
    out.append(fixed_vendor_size & 0xFF)
    out.append(attrs & 0xFF)
    out += struct.pack("<I", modify_date)
    out += struct.pack("<I", data_entry_size)
    out.append(extra_info & 0xFF)
    out += vendor
    out.append(len(name_b) & 0xFF)
    out += name_b
    return bytes(out)


def build_data_entry(dir_entry: bytes, path: str, data: bytes) -> bytes:
    """Build a Basic-DOS Data Entry: signature + dir entry + path entry + data.

    QIC-113 Rev G §7.2: ``path`` is the **directory** the item is in ("" for
    the root, "a/b" for nested; written null-separated), and the copy's Data
    Entry size is rewritten to header + data, as the spec defines it.
    """
    sig = b"\xcc\x33\xcc\x33"
    path_b = path.replace("/", "\x00").encode("ascii")
    path_entry = bytes([len(path_b)]) + path_b
    header_len = len(sig) + len(dir_entry) + len(path_entry)
    copy = bytearray(dir_entry)
    struct.pack_into("<I", copy, 6, header_len + len(data))
    return sig + bytes(copy) + path_entry + data


# ---------------------------------------------------------------------------
# QIC-113 Extended-OS volume (Rev G section 8)
# ---------------------------------------------------------------------------

# Data Description IDs and signatures, copied from qiclib.qic113ext so a
# builder bug cannot hide behind the parser's own constants.
_DD_DOS, _DD_DATA, _DD_WIN95 = 2, 7, 10
_EXT_DATA_ENTRY_SIG = b"\xcc\x33\xcc\x33"
_EXT_DATA_AREA_SIG = b"\x99\x66\x99\x66"


def _ext_description(ddid: int, area: int, struct_bytes: bytes = b"", name: str = "") -> bytes:
    """One Data Description Entry: ID, Data Area size, struct, UTF-16LE name."""
    raw_name = name.encode("utf-16-le")
    return (
        struct.pack("<HQH", ddid, area, len(struct_bytes))
        + struct_bytes
        + struct.pack("<H", len(raw_name))
        + raw_name
    )


def build_ext_dir_entry(
    name: str,
    traversal: int,
    *,
    size: int = 0,
    mtime: int = 0,
    attrs: int = 0,
    data_entry_size: int = 0,
    path_entry_size: int = 0,
) -> bytes:
    """An Extended-OS Directory Entry with DATA, Windows 95 and DOS descriptions.

    ``attrs`` is the DOS attribute byte (bit 0 read-only) kept in the Win95
    struct, ``mtime`` its modify time in seconds since 1970 (struct offset
    20), ``size`` the DATA area's size. This is the shape the bench "jc" tape
    uses (QIC-113 Rev G 8.2.0.1).
    """
    win95 = struct.pack("<I", attrs) + b"\xff" * 16 + struct.pack("<II", mtime, 0)
    descriptions = (
        _ext_description(_DD_DATA, size)
        + _ext_description(_DD_WIN95, 0, win95, name)
        + _ext_description(_DD_DOS, 0, b"\x00" * 9, name)
    )
    body = (
        struct.pack("<QHHB", data_entry_size, path_entry_size, _DD_WIN95, traversal) + descriptions
    )
    return struct.pack("<H", len(body)) + body


@dataclass(frozen=True)
class ExtItem:
    """One entry of a synthetic Extended-OS volume, in directory order.

    ``traversal`` uses the qiclib.qic113ext T_* bits; the caller orders the
    items one directory level at a time, as QIC-113 Rev G 8.2.1 does.
    """

    name: str
    traversal: int
    data: bytes = b""
    mtime: int = 0
    attrs: int = 0


def build_ext_volume(items: list[ExtItem]) -> tuple[bytes, bytes]:
    """(File Set Data Section, Directory Section) for ``items``.

    Every item, directories included, gets a Data Entry: signature, a copy of
    its Directory Entry, an empty Path Entry, then a DATA area holding
    ``data`` and an empty Windows 95 area (the DOS area is a Null type and
    takes no space). Each Directory Entry records its Data Entry's size, so
    ``qic113ext.layout`` places the file bytes from the directory alone.
    """
    data_section = b""
    directory = b""
    for item in items:
        copy = build_ext_dir_entry(
            item.name, item.traversal, size=len(item.data), mtime=item.mtime, attrs=item.attrs
        )
        data_entry = (
            _EXT_DATA_ENTRY_SIG
            + copy
            + _EXT_DATA_AREA_SIG
            + struct.pack("<H", _DD_DATA)
            + item.data
            + _EXT_DATA_AREA_SIG
            + struct.pack("<H", _DD_WIN95)
        )
        data_section += data_entry
        directory += build_ext_dir_entry(
            item.name,
            item.traversal,
            size=len(item.data),
            mtime=item.mtime,
            attrs=item.attrs,
            data_entry_size=len(data_entry),
        )
    return data_section, directory


# ---------------------------------------------------------------------------
# TWVL volume (TWS-3)
# ---------------------------------------------------------------------------


def build_twvl(
    stream: bytes,
    vtbl_record: bytes,
    *,
    tape_name: str = "EXAMPLE TAPE",
    holes: list[list[int]] | None = None,
    lost_segments: list[int] | None = None,
    data_section_size: int | None = None,
    dir_section_size: int | None = None,
    directory_offset: int | None = None,
    compressed: bool | None = False,
) -> Volume:
    """A TWVL :class:`~tapewyrm_archive.twvl.Volume` around ``stream``.

    The header carries what ``qicsilver tar`` and ``qicsilver inspect`` read
    (TWS-3 section 3.1): the raw VTBL record (``vtbl_record``, e.g. from
    :func:`build_vtbl_entry`), its flag byte, the section sizes, holes and
    lost segments. Save it with ``.save(path)``.
    """
    return Volume(
        header={
            "format": "TWVL",
            "tape_name": tape_name,
            "holes": holes or [],
            "lost_segments": lost_segments or [],
            "data_section_size": data_section_size,
            "dir_section_size": dir_section_size,
            "directory_offset": directory_offset,
            "volume_size": len(stream),
            "vtbl": {
                "raw": vtbl_record.hex(),
                "flags": vtbl_record[56],
                "compressed": compressed,
                "data_section_size": data_section_size,
                "dir_section_size": dir_section_size,
            },
        },
        data=stream,
    )

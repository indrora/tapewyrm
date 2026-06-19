"""Synthesize codec fixtures: header segments, volume tables, QIC-113 volumes.

These mirror the on-tape structures (DESIGN.md §7.3, §7.5) closely enough to
exercise the parsers end-to-end with no hardware.
"""

from __future__ import annotations

import struct

from tapewyrm.codec.volume import FPR_SIGNATURE, SIG_VTBL, VTBL_ENTRY_LEN
from tapewyrm.types import RawSector, Segment

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
    """Pack a short date the way decode_short_date expects (0-based mo/dy)."""
    rest = sc + 60 * (mn + 60 * (hr + 24 * (dy + 31 * mo)))
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
) -> bytes:
    """Build sector 0 of the header segment: FPR + bad-sector map.

    The BSM proper begins at offset 128 (after the format parameter record), as
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
    struct.pack_into("<I", sec, 74, format_date)

    # Bad-sector map at offset 128.
    off = 128
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
) -> bytes:
    """Build one 128-byte VTBL entry."""
    rec = bytearray(VTBL_ENTRY_LEN)
    rec[0:4] = signature
    struct.pack_into("<I", rec, 4, start_seg)
    struct.pack_into("<I", rec, 8, end_seg)
    desc = description.encode("ascii")[:44]
    rec[12 : 12 + len(desc)] = desc
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
    """Build a Basic-DOS Data Entry: signature + dir entry + path entry + data."""
    sig = b"\xcc\x33\xcc\x33"
    path_b = path.encode("ascii")
    path_entry = bytes([len(path_b)]) + path_b
    return sig + dir_entry + path_entry + data

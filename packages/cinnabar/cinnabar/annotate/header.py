"""Spans for the header segment: the format parameter record and the bad-sector map.

Offsets are those of QIC-80-MC Rev N section 7.1 (docs/qic-standards/qic80n.pdf),
the same ones :class:`qiclib.volume.VolumeInfo` documents field by field, and
of the bad-sector map in DESIGN.md 7.3 / :func:`qiclib.volume._parse_bsm`.
Positions are byte offsets into the segment's data (sector 0 at 0).

Three levels come out, which is what the sketch's stacked "Fields" lanes
show for this segment:

    FPR | BSM                            (level 0: the two regions)
    sig | code | rev | hdr | ... | lot   (level 1: each FPR field; each BSM entry)
"""

from __future__ import annotations

import struct

from qiclib import volume as volume_mod
from tapewyrm_archive.qic80date import decode_short_date

from cinnabar.model import Shape, Span

# (offset, length, key, label) for the format parameter record, QIC-80-MC
# Rev N section 7.1. Keys name the VolumeInfo field holding the decoded value.
_FPR_FIELDS: tuple[tuple[int, int, str, str], ...] = (
    (0, 4, "signature", "55 AA 55 AA"),
    (4, 1, "format_code", "fmt"),
    (5, 1, "revision", "rev"),
    (6, 2, "header_seg", "hdr seg"),
    (8, 2, "dup_header_seg", "dup hdr"),
    (10, 2, "first_data_seg", "first data"),
    (12, 2, "last_data_seg", "last data"),
    (14, 4, "format_date", "format date"),
    (18, 4, "write_date", "write date"),
    (24, 2, "segments_per_track", "seg/trk"),
    (26, 1, "tracks", "tracks"),
    (27, 1, "max_fsd", "max fsd"),
    (28, 1, "max_ftk", "max ftk"),
    (29, 1, "max_fsc", "max fsc"),
    (30, 44, "tape_name", "tape name"),
    (74, 4, "name_date", "name date"),
    (128, 1, "reformat_error", "reformat err"),
    (130, 4, "segments_written", "segs written"),
    (138, 4, "initial_format_date", "first format"),
    (142, 2, "format_count", "formats"),
    (146, 44, "manufacturer", "manufacturer"),
    (190, 44, "lot_code", "lot"),
)

_DATES = {"format_date", "write_date", "name_date", "initial_format_date"}


def _date(packed: int) -> str:
    decoded = decode_short_date(packed)
    if decoded is None:
        return f"0x{packed:08x} (no date)"
    y, mo, d, h, mi, s = decoded
    return f"{y:04d}-{mo:02d}-{d:02d} {h:02d}:{mi:02d}:{s:02d}"


def _value(data: bytes, off: int, length: int, key: str) -> str:
    raw = data[off : off + length]
    if len(raw) < length:
        return "(short)"
    if key in _DATES:
        return _date(int.from_bytes(raw, "little"))
    if key in ("tape_name", "manufacturer", "lot_code"):
        return raw.decode("ascii", "replace").rstrip(" \x00") or "(empty)"
    if key == "signature":
        return raw.hex(" ")
    if key == "reformat_error":
        return "yes" if raw[0] == 0xFF else "no"
    return str(int.from_bytes(raw, "little"))


def fpr_spans(data: bytes) -> Span:
    """The format parameter record (bytes 0-255 of sector 0) as one span with its fields."""
    fields = []
    for off, length, key, label in _FPR_FIELDS:
        value = _value(data, off, length, key)
        fields.append(
            Span(
                off,
                off + length,
                label,
                value if length >= 4 else "",
                "fpr-field",
                Shape.HEX if length <= 2 else Shape.BLOCK,
                (("field", key), ("offset", f"{off}-{off + length - 1}"), ("value", value)),
            )
        )
    name = _value(data, 30, 44, "tape_name")
    return Span(
        0,
        256,
        "Format Parameter Record",
        name,
        "fpr",
        Shape.BLOCK,
        (("tape name", name), ("format code", _value(data, 4, 1, "format_code"))),
        tuple(fields),
    )


def bsm_spans(data: bytes, format_code: int) -> Span:
    """The bad-sector map as one span with an entry child per bad sector or segment.

    Format code 4 (QIC-80 variable format) keeps ascending 3-byte 1-based LSN
    entries from offset 256, high bit of the third byte flagging a whole bad
    segment, ``00 00 00`` ending the list. The fixed formats (codes 2, 3, 5)
    keep one 32-bit mask per segment from offset 2048. Both layouts per
    :func:`qiclib.volume._parse_bsm`.
    """
    entries: list[Span] = []
    if format_code != 4:
        off = volume_mod.FIXED_BSM_OFFSET
        end = min(len(data), 29 * 1024)
        seg_abs = 0
        while off + 4 <= end:
            (mask,) = struct.unpack_from("<I", data, off)
            if mask:
                label = "bad seg" if mask == 0xFFFFFFFF else f"{bin(mask).count('1')} bad"
                entries.append(
                    Span(
                        off,
                        off + 4,
                        label,
                        "",
                        "bsm-entry",
                        Shape.HEX,
                        (("segment", str(seg_abs)), ("mask", f"0x{mask:08x}")),
                    )
                )
            off += 4
            seg_abs += 1
        return Span(
            volume_mod.FIXED_BSM_OFFSET,
            end,
            "Bad Sector Map",
            f"{len(entries)} entries, fixed layout",
            "bsm",
            Shape.BLOCK,
            (),
            tuple(entries),
        )

    off = volume_mod.BSM_OFFSET
    while off + 3 <= len(data):
        b0, b1, b2 = data[off], data[off + 1], data[off + 2]
        if b0 == 0 and b1 == 0 and b2 == 0:
            entries.append(Span(off, off + 3, "end", "", "bsm-end", Shape.HEX))
            off += 3
            break
        lsn = b0 | (b1 << 8) | ((b2 & 0x7F) << 16)
        whole = bool(b2 & 0x80)
        entries.append(
            Span(
                off,
                off + 3,
                f"seg {(lsn - 1) // 32}" if whole else f"lsn {lsn - 1}",
                "",
                "bsm-entry",
                Shape.HEX,
                (("lsn (1-based)", str(lsn)), ("whole segment", "yes" if whole else "no")),
            )
        )
        off += 3
    return Span(
        volume_mod.BSM_OFFSET,
        max(off, volume_mod.BSM_OFFSET + 3),
        "Bad Sector Map",
        f"{max(len(entries) - 1, 0)} entries",
        "bsm",
        Shape.BLOCK,
        (),
        tuple(entries),
    )


def header_spans(data: bytes) -> list[Span]:
    """Level-0 spans of a header segment: the FPR, then the bad-sector map."""
    if len(data) < 256:
        return [Span(0, max(len(data), 1), "Header (short)", "", "unknown")]
    format_code = data[4]
    return [fpr_spans(data), bsm_spans(data, format_code)]

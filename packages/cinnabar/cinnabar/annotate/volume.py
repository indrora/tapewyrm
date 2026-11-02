"""Spans for the volume table and for each volume's data area (QIC-113 Rev G).

Two kinds of position come out of here:

- :func:`vtbl_spans` positions in **segment data offsets** of the volume
  table segment (one 128-byte record per entry, QIC-113 section 5).
- :func:`volume_spans` positions in **volume byte stream offsets**: the
  concatenated data areas of the volume's segments, BSM-bad segments
  skipped, exactly as :func:`qiclib.volume.volume_streams` builds it. The
  loader knows which stream offset each segment starts at and clips.
- :func:`extent_spans` positions in segment data offsets, because a
  compressed volume's structure (QIC-113 section 9) is per segment: one
  Compression Extent per segment, frames inside it, and only the
  *decompressed* bytes carry directory and data entries, which have no
  position on tape to draw.

Levels, as the sketch stacks them:

    Volume Table                         Directory Section | Data Section
      VTBL | VTBL | XTBL | end             entry | entry ...   Data Entry | Data Entry
        sig | segs | desc | date ...                             sig | entry | path | data

The parsers are qiclib's own (``qiclib.volume``, ``qiclib.qic113``,
``qiclib.qic113ext``), so what cinnabar labels is what ``qicsilver extract``
would read. Private helpers of those modules are used on purpose: they are
the walkers that honour every quirk the bench tapes taught the project
(MTN's 9-byte fixed portion, unknown sizes), and a second copy would drift.
"""

from __future__ import annotations

import struct

from qiclib import qic113, qic113ext
from qiclib import volume as volume_mod
from qiclib.volume import VtblEntry
from tapewyrm_archive.qic80date import decode_short_date

from cinnabar.model import Shape, Span, assign_rows

VTBL_RECORD = 128
VTBL_ROWS = ("QIC-113: Volume table", "QIC-113: Volumes", "QIC-113: Volume fields")
VOLUME_ROWS = ("QIC-113: Sections", "QIC-113: Entries", "QIC-113: Entry parts")
EXTENT_ROWS = ("QIC-113: Extent", "QIC-122: Frames", "QIC-122: Frame fields")

# (offset, length, label, key) of a VTBL entry, QIC-113 Rev G section 5 (and
# section 6 for the QIC-40/80 vendor-specific layout, same offsets).
_VTBL_FIELDS: tuple[tuple[int, int, str, str], ...] = (
    (0, 4, "sig", "signature"),
    (4, 4, "segs", "segments"),
    (8, 44, "description", "description"),
    (52, 4, "date", "date"),
    (56, 1, "flags", "flags"),
    (57, 1, "seq", "multi-cartridge sequence"),
    (58, 2, "113", "major revision"),
    (60, 2, "rev", "minor revision"),
    (62, 14, "vendor", "vendor extension"),
    (76, 4, "qfa start", "starting block (QFA)"),
    (80, 4, "qfa end", "ending block (QFA)"),
    (84, 8, "password", "password"),
    (92, 4, "dir size", "directory section size"),
    (96, 8, "data size", "data section size"),
    (104, 2, "os ver", "OS version"),
    (106, 16, "label", "source volume label"),
    (122, 1, "dev", "logical device"),
    (123, 1, "", "reserved"),
    (124, 1, "cmp", "compression"),
    (125, 1, "os", "format & OS type"),
    (126, 2, "", "reserved"),
)

_OS_TYPES = {
    0: "extended (unknown OS)",
    1: "DOS basic",
    2: "Unix",
    3: "OS/2",
    4: "NetWare",
    5: "Windows NT",
    6: "DOS extended",
    7: "Windows 95",
}


def _date(packed: int) -> str:
    decoded = decode_short_date(packed)
    if decoded is None:
        return f"0x{packed:08x}"
    y, mo, d, h, mi, s = decoded
    return f"{y:04d}-{mo:02d}-{d:02d} {h:02d}:{mi:02d}:{s:02d}"


def _vtbl_value(rec: bytes, off: int, length: int, key: str) -> str:
    raw = rec[off : off + length]
    if key in ("signature",):
        return raw.decode("ascii", "replace")
    if key in ("description", "password", "source volume label"):
        return raw.decode("ascii", "replace").rstrip(" \x00") or "(empty)"
    if key == "date":
        return _date(int.from_bytes(raw, "little"))
    if key == "flags":
        bits = []
        for bit, name in (
            (0, "vendor"),
            (1, "multi-cart"),
            (2, "unverified"),
            (4, "seg-spanning"),
            (5, "dir-last"),
        ):
            if raw[0] >> bit & 1:
                bits.append(name)
        return f"0x{raw[0]:02x} " + (" ".join(bits) if bits else "(none)")
    if key == "compression":
        return f"0x{raw[0]:02x}" + (" compressed" if raw[0] & 0x80 else "")
    if key == "format & OS type":
        return f"{raw[0]} = {_OS_TYPES.get(raw[0], 'unknown')}"
    if key == "vendor extension":
        return raw.hex(" ")
    return str(int.from_bytes(raw, "little"))


def _vtbl_entry_span(rec: bytes, off: int, entry: VtblEntry | None) -> Span:
    sig = rec[:4]
    if sig == volume_mod.SIG_VTBL:
        style, label = "vtbl-entry", "Volume"
    elif sig == volume_mod.SIG_XTBL:
        style, label = "vtbl-ext", "XTBL"
    elif sig in (b"UTID", b"EXVT"):
        style, label = "vtbl-ext", sig.decode("ascii")
    else:
        return Span(off, off + VTBL_RECORD, "end", "", "vtbl-end", Shape.HEX)
    fields = []
    # The vendor-specific bit (byte 56 bit 0) ends the defined fields at
    # byte 56 for a non-QIC-113 vendor volume (QIC-80-MC section 8); past
    # that the bytes are shown but labelled as such.
    vendor_only = bool(rec[56] & 0x01) and entry is not None and not qic113.is_extended_os(entry)
    for f_off, length, short, key in _VTBL_FIELDS:
        if sig != volume_mod.SIG_VTBL and f_off >= 4:
            break
        value = _vtbl_value(rec, f_off, length, key)
        name = short if not (vendor_only and f_off > 56) else "vendor"
        fields.append(
            Span(
                off + f_off,
                off + f_off + length,
                name,
                value if length >= 8 else "",
                "vtbl-field" if not (vendor_only and f_off > 56) else "vendor",
                Shape.HEX if length <= 2 else Shape.BLOCK,
                (("field", key), ("offset", f"{f_off}-{f_off + length - 1}"), ("value", value)),
            )
        )
    desc = _vtbl_value(rec, 8, 44, "description") if sig == volume_mod.SIG_VTBL else ""
    detail: list[tuple[str, str]] = [("signature", sig.decode("ascii", "replace"))]
    if entry is not None:
        detail += [
            ("description", entry.description),
            ("segments", f"{entry.start_seg}-{entry.end_seg}"),
            ("date", _date(entry.date)),
            ("flags", _vtbl_value(rec, 56, 1, "flags")),
            ("compressed", str(entry.compressed)),
            ("extended OS", str(qic113.is_extended_os(entry))),
        ]
    return Span(
        off, off + VTBL_RECORD, label, desc, style, Shape.CHEVRON, tuple(detail), tuple(fields)
    )


def vtbl_spans(data: bytes, entries: list[VtblEntry]) -> list[Span]:
    """The volume table segment: one level-0 span with a child per 128-byte record.

    ``entries`` are the decoded VTBL entries in table order (from the volume
    profile that identified the tape), used for the hover rows; the raw
    records are re-read from ``data`` so every slot up to the terminator is
    shown, extensions included.
    """
    children: list[Span] = []
    used = 0
    off = 0
    entry_i = 0
    while off + VTBL_RECORD <= len(data):
        rec = data[off : off + VTBL_RECORD]
        entry = None
        if rec[:4] == volume_mod.SIG_VTBL and entry_i < len(entries):
            entry = entries[entry_i]
            entry_i += 1
        child = _vtbl_entry_span(rec, off, entry)
        children.append(child)
        off += VTBL_RECORD
        if child.style == "vtbl-end":
            break
        used += 1
    end = max(off, VTBL_RECORD)
    table = Span(
        0,
        end,
        "Volume Table",
        f"{used} entries",
        "vtbl",
        Shape.CHEVRON,
        (("entries", str(used)),),
        tuple(children),
    )
    return [assign_rows(table, VTBL_ROWS)]


# ---------------------------------------------------------------------------
# Volume data areas (uncompressed): directory and data sections
# ---------------------------------------------------------------------------


def _dir_entry_span(stream: bytes, off: int, entry: qic113.DirEntry) -> Span:
    end = off + entry.entry_len
    kind = "dir" if entry.is_subdir else "file"
    fixed = (
        Span(off, off + 1 + stream[off], "fixed", "", "dir-fixed", Shape.HEX),
        Span(off + 1 + stream[off], end, "name", entry.name, "dir-name", Shape.BLOCK),
    )
    flags = []
    if entry.last_in_dir:
        flags.append("last in dir")
    if entry.last_in_table:
        flags.append("last in table")
    if entry.unreadable:
        flags.append("unreadable")
    return Span(
        off,
        end,
        entry.name,
        kind,
        "dir-entry" if not entry.is_subdir else "dir-subdir",
        Shape.BLOCK,
        (
            ("name", entry.name),
            ("kind", kind),
            ("modified", _date(entry.modify_date)),
            ("data entry size", str(entry.data_entry_size)),
            ("attrs", f"0x{entry.attrs:02x} " + ", ".join(flags)),
        ),
        fixed,
    )


def _basic_directory(stream: bytes, start: int) -> tuple[list[Span], int]:
    """Directory entries from ``start`` to the 'last in table' one; returns (spans, end)."""
    spans: list[Span] = []
    off = start
    while off < len(stream):
        parsed = qic113._parse_dir_entry(stream, off)
        if parsed is None:
            break
        entry, nxt = parsed
        if not entry.name and entry.attrs == 0:
            break  # zero fill after the table
        spans.append(_dir_entry_span(stream, off, entry))
        off = nxt
        if entry.last_in_table:
            break
    return spans, off


def _basic_data(stream: bytes, start: int, end: int | None) -> list[Span]:
    """Data entries (signature, directory-entry copy, path, file bytes) from ``start``."""
    spans: list[Span] = []
    window = stream if end is None else stream[:end]
    for de in qic113._parse_basic_data_section(window, start):
        data_at = de.offset or 0
        name = de.dir_entry.name
        dir_len = max(len(de.path) - len(name) - 1, 0)
        path_at = data_at - 1 - dir_len
        entry_at = path_at - de.dir_entry.entry_len
        sig_at = entry_at - 4
        children = (
            Span(sig_at, sig_at + 4, "33CC33CC", "", "sig", Shape.HEX),
            _dir_entry_span(stream, entry_at, de.dir_entry),
            Span(path_at, data_at, "path", de.path[:dir_len], "path", Shape.HEX),
            Span(data_at, data_at + len(de.data), "data", f"{len(de.data):,} B", "filedata"),
        )
        spans.append(
            Span(
                sig_at,
                data_at + len(de.data),
                name,
                f"{len(de.data):,} B",
                "data-entry",
                Shape.CHEVRON,
                (("path", de.path), ("bytes", str(len(de.data))), ("header at", str(sig_at))),
                children,
            )
        )
    return spans


def _extended_data(stream: bytes, dir_at: int | None) -> list[Span]:
    """Extended-OS data entries, placed from the directory section (QIC-113 section 8).

    An Extended-OS volume's directory (at ``dir_at``, segment aligned at the
    end) is the authority on where each file's Data Entry sits: entry
    offsets are running sums of the directory's Data Entry sizes
    (:func:`qiclib.qic113ext.layout`), and the file's bytes are the DATA
    area inside it. Walking the data section by each entry's own size
    does not work: the root entry carries an "unknown" size. Names and
    paths come from the directory's preorder (:func:`qiclib.qic113ext.directory_paths`).
    """
    if dir_at is None or dir_at > len(stream):
        return []
    try:
        entries = qic113ext.parse_directory(stream[dir_at:])
    except ValueError:
        return []
    paths = qic113ext.directory_paths(entries)
    spans: list[Span] = []
    for place, entry, path in zip(qic113ext.layout(entries, stream), entries, paths, strict=False):
        off = place.entry_offset
        if off >= min(dir_at, len(stream)):
            break
        end = off + entry.data_entry_size if 0 < entry.data_entry_size < len(stream) else None
        children: list[Span] = [Span(off, off + 4, "33CC33CC", "", "sig", Shape.HEX)]
        if place.data_offset is not None and place.data_size:
            d0 = place.data_offset
            d1 = min(d0 + place.data_size, len(stream))
            children.append(Span(off + 4, d0, "entry", entry.name, "dir-entry", Shape.BLOCK))
            children.append(Span(d0, d1, "data", f"{place.data_size:,} B", "filedata"))
            if end is None:
                end = d1
        else:
            end = (
                end or off + 4 + 2 + entry.data_entry_size
                if entry.data_entry_size < len(stream)
                else off + 64
            )
            children.append(Span(off + 4, end, "entry", entry.name, "dir-entry", Shape.BLOCK))
        kind = "dir" if entry.is_dir else "file"
        detail = (
            ("path", path),
            ("kind", kind),
            ("bytes", str(place.data_size)),
            ("modified", str(entry.mtime) if entry.mtime else "(none)"),
        )
        spans.append(
            Span(
                off,
                max(end, off + 4),
                entry.name or "(entry)",
                f"{place.data_size:,} B" if not entry.is_dir else kind,
                "data-entry" if not entry.is_dir else "dir-subdir",
                Shape.CHEVRON,
                detail,
                tuple(children),
            )
        )
    return spans


def _extended_directory(stream: bytes, start: int) -> list[Span]:
    spans: list[Span] = []
    off = start
    while off + 2 <= len(stream):
        try:
            entry, nxt = qic113ext.parse_entry(stream, off)
        except ValueError:
            break
        name = next((d.name for d in entry.descriptions if d.name), "")
        spans.append(
            Span(
                off,
                nxt,
                name or "(entry)",
                "",
                "dir-entry",
                Shape.BLOCK,
                (("name", name), ("data entry size", str(entry.data_entry_size))),
            )
        )
        off = nxt
        if entry.traversal & qic113ext.T_LAST_IN_SET:
            break
    return spans


def _volume_spans(stream: bytes, vtbl: VtblEntry, *, decompressed: bool = False) -> list[Span]:
    """Level-0 spans of an uncompressed volume's byte stream: its directory and data sections.

    Basic-DOS, directory first (QIC-113 section 7):
    ``Directory Section + Data Section``. Directory last (flag byte 56 bit
    5) or Extended-OS (section 8): ``Data Section + gap + Directory Section``.
    A compressed volume has no structure in the stream (see
    :func:`extent_spans`), so this returns nothing for it.
    """
    if vtbl.compressed and not decompressed:
        return []
    extended = qic113.is_extended_os(vtbl)
    if extended or vtbl.directory_last:
        try:
            dir_at = qic113._directory_last_offset(stream, vtbl)
        except Exception:  # noqa: BLE001 -- a hole or garbage: no directory to show
            dir_at = len(stream)
        if extended:
            data = _extended_data(stream, dir_at)
            directory = _extended_directory(stream, dir_at)
        else:
            data = _basic_data(stream, 0, dir_at)
            directory, _ = _basic_directory(stream, dir_at)
        dir_end = directory[-1].end if directory else dir_at
        out = []
        if data:
            out.append(
                Span(
                    0,
                    max(data[-1].end, 1),
                    "Data Section",
                    f"{len(data)} entries",
                    "data-section",
                    Shape.CHEVRON,
                    (),
                    tuple(data),
                )
            )
        if directory:
            out.append(
                Span(
                    dir_at,
                    dir_end,
                    "Directory Section",
                    f"{len(directory)} entries",
                    "dir-section",
                    Shape.CHEVRON,
                    (),
                    tuple(directory),
                )
            )
        return out
    directory, dir_end = _basic_directory(stream, 0)
    data = _basic_data(stream, dir_end, None)
    out = []
    if directory:
        out.append(
            Span(
                0,
                dir_end,
                "Directory Section",
                f"{len(directory)} entries",
                "dir-section",
                Shape.CHEVRON,
                (),
                tuple(directory),
            )
        )
    if data:
        out.append(
            Span(
                dir_end,
                data[-1].end,
                "Data Section",
                f"{len(data)} entries",
                "data-section",
                Shape.CHEVRON,
                (),
                tuple(data),
            )
        )
    return out


def volume_spans(stream: bytes, vtbl: VtblEntry, *, decompressed: bool = False) -> list[Span]:
    """:func:`_volume_spans` with rows assigned: sections, entries, entry parts.

    ``decompressed`` says ``stream`` is a compressed volume's bytes *after*
    QIC-122 (``cinnabar.load.volumes.Volume.decompressed``), which have the
    same structure as an uncompressed volume's; without it a compressed
    entry's stream is taken as still-compressed and has no structure to show.
    """
    return [
        assign_rows(s, VOLUME_ROWS) for s in _volume_spans(stream, vtbl, decompressed=decompressed)
    ]


# ---------------------------------------------------------------------------
# Compressed volumes: extents and frames, per segment
# ---------------------------------------------------------------------------

_RAW_FRAME = 0x8000
_TAIL_FILL = 18  # QIC-113 section 9.1.4: a frame never starts in the last 18 bytes


def _extent_spans(data: bytes, *, spanning: bool, offset_bytes: int = 8) -> list[Span]:
    """One segment of a compressed volume: its Compression Extent and frames (QIC-113 section 9).

    With segment spanning the first two bytes are the Next Extent Offset: 2
    means an extent starts right after, 0 means the whole segment continues
    the previous frame, more means that many bytes of continuation first.
    ``offset_bytes`` is the width of the extent's Uncompressed Volume Byte
    Offset: 8 per Rev G, 4 on the MTN tapes (the volume profile knows).
    """
    out: list[Span] = []
    pos = 0
    if spanning:
        if len(data) < 2:
            return out
        (nxt,) = struct.unpack_from("<H", data, 0)
        out.append(
            Span(0, 2, "next", str(nxt), "extent-next", Shape.HEX, (("next extent", str(nxt)),))
        )
        if nxt == 0:
            out.append(Span(2, len(data), "frame (cont.)", "", "frame"))
            return out
        if nxt > 2:
            out.append(Span(2, nxt, "frame (cont.)", "", "frame"))
        pos = nxt
    if pos + offset_bytes + 2 > len(data):
        return out
    uoff = int.from_bytes(data[pos : pos + offset_bytes], "little")
    frames: list[Span] = [
        Span(
            pos,
            pos + offset_bytes,
            "offset",
            f"{uoff:,}",
            "extent-offset",
            Shape.HEX,
            (("uncompressed volume byte offset", f"{uoff:,}"),),
        )
    ]
    p = pos + offset_bytes
    n_frames = 0
    while len(data) - p >= _TAIL_FILL:
        (size,) = struct.unpack_from("<H", data, p)
        n = size & ~_RAW_FRAME
        if n == 0:
            frames.append(Span(p, len(data), "fill", "", "fill"))
            break
        raw = bool(size & _RAW_FRAME)
        end = min(p + 2 + n, len(data))
        frames.append(
            Span(
                p,
                end,
                "raw" if raw else "frame",
                f"{n:,} B",
                "frame-raw" if raw else "frame",
                Shape.CHEVRON,
                (("size", str(n)), ("raw", "yes" if raw else "no (QIC-122)")),
                (Span(p, p + 2, "size", "", "frame-size", Shape.HEX),),
            )
        )
        n_frames += 1
        p = end
    out.append(
        Span(
            pos,
            max(p, pos + offset_bytes),
            "Compression Extent",
            f"{n_frames} frames @ {uoff:,}",
            "extent",
            Shape.CHEVRON,
            (("frames", str(n_frames)), ("uncompressed offset", f"{uoff:,}")),
            tuple(frames),
        )
    )
    return out


def extent_spans(data: bytes, *, spanning: bool, offset_bytes: int = 8) -> list[Span]:
    """:func:`_extent_spans` with rows assigned: extent, frames, frame fields."""
    return [
        assign_rows(s, EXTENT_ROWS)
        for s in _extent_spans(data, spanning=spanning, offset_bytes=offset_bytes)
    ]

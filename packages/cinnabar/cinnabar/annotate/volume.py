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

Which layout a volume has is not decided here. The volume profile that
read the tape's table names a file-set parser (DESIGN.md 7.8,
``qiclib.fileset``: ``qic113-basic``, ``qic113-extended``, ``none``), and
the loader hands it over as a :class:`~cinnabar.model.VolumeReading`;
:func:`volume_spans` draws the sections that parser would list, and
:func:`vtbl_spans` draws the VTBL fields where that profile places them.
A stream with no verdict behind it (hand-built) is probed with every
parser (:func:`probe_parser`), as ``qicsilver inspect`` probes a TWVL
whose header names none; the record's own bytes are never the test, since
the unk-co tape's record says "QIC-113" with the vendor bit clear and
only the profile and its parser's probe settled what it was.
"""

from __future__ import annotations

import logging
import struct
from pathlib import Path

from qiclib import fileset as fs
from qiclib import qic113, qic113ext
from qiclib import volume as volume_mod
from qiclib.verdict import VolumeWarning, warning_names
from qiclib.volume import VtblEntry
from tapewyrm_archive.qic80date import decode_short_date
from tapewyrm_archive.twvl import Volume

from cinnabar.model import SEGMENT_BYTES, Shape, Span, VolumeReading, assign_rows

log = logging.getLogger(__name__)

VTBL_RECORD = 128
# The two QIC-113 parsers this module can draw, by their qiclib.fileset
# names (qicsilver.entries names the same two).
_BASIC = "qic113-basic"
_EXTENDED = "qic113-extended"
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

# A volume profile's ``[vtbl]`` field names (qiclib.volume_profile.FIELD_NAMES)
# as the lane labels them: the short label and the long key _vtbl_value
# renders by, the same two the fixed table uses, so a Rev N profile's
# fields look exactly as the spec table did.
_PROFILE_FIELDS: dict[str, tuple[str, str]] = {
    "multi_cartridge_seq": ("seq", "multi-cartridge sequence"),
    "dir_section_size": ("dir size", "directory section size"),
    "data_section_size": ("data size", "data section size"),
    "source_label": ("label", "source volume label"),
    "compression": ("cmp", "compression"),
    "os_type": ("os", "format & OS type"),
}

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
    if key in ("vendor extension", "vendor bytes", "fingerprint"):
        return raw.hex(" ")
    return str(int.from_bytes(raw, "little"))


def _entry_fields(
    rec: bytes, reading: VolumeReading | None
) -> list[tuple[int, int, str, str, str]]:
    """``(offset, length, label, key, style)`` of a VTBL record's fields, as the profile reads them.

    Bytes 0-56 are everyone's (QIC-80-MC Rev N section 8) and come from the
    fixed table. Past them the profile that read the table is the only
    authority on what sits where (DESIGN.md 7.8): its ``[vtbl]`` fields are
    drawn under their names, and the bytes its ``[match]`` gate compared
    are drawn as the fingerprint. The fixed table's Rev G offsets are not
    drawn under a profile, not even one that agrees with them: the MTN
    layout keeps its label where Rev G keeps the OS version, and a lane
    that labelled both would contradict itself. A profile that places
    nothing past byte 56 (``vendor-unknown``) leaves those bytes the
    vendor's. With no reading at all (a hand-built table) the fixed table
    stands, except that a record with the vendor-specific bit set has only
    bytes 0-56 defined (Rev N section 8), so the rest is the vendor's.
    """
    fixed = [(o, n, label, key, "vtbl-field") for o, n, label, key in _VTBL_FIELDS]
    base = [f for f in fixed if f[0] <= 56]
    vendor = (57, VTBL_RECORD - 57, "vendor", "vendor bytes", "vendor")
    if reading is None:
        if rec[56] & 0x01:
            log.debug("vtbl record: no profile reading and the vendor bit is set; bytes 57+ vendor")
            return base + [vendor]
        return fixed
    if not reading.fields:
        log.debug("vtbl record: profile %s places no field past byte 56", reading.profile)
        return base + [vendor]
    for name, off, length in reading.fields:
        if name == "match":
            base.append((off, length, "match", "fingerprint", "sig"))
        else:
            label, key = _PROFILE_FIELDS.get(name, (name, name))
            base.append((off, length, label, key, "vtbl-field"))
    return sorted(base)


def _vtbl_entry_span(
    rec: bytes, off: int, entry: VtblEntry | None, reading: VolumeReading | None = None
) -> Span:
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
    for f_off, length, short, key, f_style in _entry_fields(rec, reading):
        if sig != volume_mod.SIG_VTBL and f_off >= 4:
            break
        value = _vtbl_value(rec, f_off, length, key)
        fields.append(
            Span(
                off + f_off,
                off + f_off + length,
                short,
                value if length >= 8 and f_style != "vendor" else "",
                f_style,
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
        ]
        # The profile that decoded this entry, how sure it is, and the
        # parser it names for the volume: the verdict qicsilver extract
        # reads the tape with. An "extended OS" row used to be decided here
        # from the record's bytes; the parser is the answer now.
        if reading is not None:
            detail += list(reading.rows)
    return Span(
        off, off + VTBL_RECORD, label, desc, style, Shape.CHEVRON, tuple(detail), tuple(fields)
    )


def vtbl_spans(
    data: bytes, entries: list[VtblEntry], reading: VolumeReading | None = None
) -> list[Span]:
    """The volume table segment: one level-0 span with a child per 128-byte record.

    ``entries`` are the decoded VTBL entries in table order (from the volume
    profile that identified the tape), used for the hover rows; the raw
    records are re-read from ``data`` so every slot up to the terminator is
    shown, extensions included. ``reading`` is that profile's verdict
    (:class:`~cinnabar.model.VolumeReading`): the fields past byte 56 are
    drawn where it places them, and every entry's rows carry the profile,
    its score, its fingerprint, its parser and its warnings. Without one
    (a hand-built table) the fixed Rev G table stands and the rows say
    nothing about a profile.
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
        child = _vtbl_entry_span(rec, off, entry, reading)
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
        (("entries", str(used)), *(reading.rows if reading is not None else ())),
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


def _extended_directory_at(vtbl: VtblEntry) -> int | None:
    """Where the extended parser reads the directory: extract's ``directory_offset``, else the data section's size.

    Exactly :mod:`qiclib.fileset.qic113_extended`'s rule, so the lanes show
    the directory where ``qicsilver inspect`` lists it from. The Basic-DOS
    locator (``qiclib.qic113._directory_last_offset``) is no use here: it
    recognises Basic-DOS entries only, and read past an Extended directory
    every time. None when the record has no data section size; the
    parser's probe is fatal on such a volume too.
    """
    if vtbl.data_section_size is None:
        log.debug(
            "volume %r: no data section size; no Extended directory to place", vtbl.description
        )
        return None
    at = _directory_offset(vtbl)
    return vtbl.data_section_size if at is None else at


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


def _directory_offset(vtbl: VtblEntry) -> int | None:
    """Where ``qiclib.extract`` would record the directory's start for this volume (TWS-3 3.1).

    An uncompressed Directory-Last volume's directory sits on the first
    segment boundary at or after its data section (QIC-113 Rev G section
    7.1), and ``extract.build_volume`` writes that into the header's
    ``directory_offset``; any other volume gets null and the parser locates
    the directory itself (an Extended one at the data section's size: a
    compressed volume's stream, built from its extents' offsets, has no
    gap). The viewer's streams lay every segment at a whole-segment offset,
    so the boundary is the size rounded up to one.
    """
    if vtbl.compressed is False and vtbl.directory_last and vtbl.data_section_size is not None:
        return -(-vtbl.data_section_size // SEGMENT_BYTES) * SEGMENT_BYTES
    return None


def twvl_volume(stream: bytes, vtbl: VtblEntry) -> Volume:
    """``stream`` as the in-memory TWVL volume a file-set parser reads (TWS-3 section 3.1).

    The header carries what the parsers and their standard checks consult
    (:mod:`qiclib.fileset.common`): the VTBL record, its flag byte,
    compression and section sizes, and the ``directory_offset`` extract
    would record (:func:`_directory_offset`). No holes: a stream here has
    had its missing segments zero-filled already, so a parser sees zeros
    where the tape had nothing, as it would in a TWVL, minus the hole map.
    ``qiclib.extract.build_volume`` writes the same members from a tape
    image; this is the part of that a stream already in memory needs.
    """
    return Volume(
        header={
            "format": "TWVL",
            "vtbl": {
                "raw": vtbl.raw.hex(),
                "flags": vtbl.flags,
                "compressed": vtbl.compressed,
                "data_section_size": vtbl.data_section_size,
                "dir_section_size": vtbl.dir_section_size,
            },
            "data_section_size": vtbl.data_section_size,
            "dir_section_size": vtbl.dir_section_size,
            "directory_offset": _directory_offset(vtbl),
            "volume_size": len(stream),
            "holes": [],
            "lost_segments": [],
        },
        data=stream,
    )


# What a parser names in its messages: there is no file behind a stream.
_SOURCE = Path("<volume stream>")


def probe_parser(stream: bytes, vtbl: VtblEntry) -> str:
    """The parser whose probe reads ``stream`` best, for a volume no profile named one for.

    A hand-built stream, or a table read with no verdict behind it: every
    listable parser gets a go at the bytes (:func:`qiclib.fileset.probe_all`),
    as ``qicsilver inspect`` does with a TWVL whose header names no parser,
    and the best that passes names the layout. None passing is ``none``:
    nothing to draw but the fact.
    """
    results = fs.probe_all(twvl_volume(stream, vtbl), vtbl, _SOURCE)
    if not results or not results[0][1].passed:
        log.debug(
            "volume %r: no file-set parser passes its probe (%s); nothing to draw",
            vtbl.description,
            "; ".join(f"{p.name} {pr.fatal or pr.score}" for p, pr in results),
        )
        return fs.NONE
    log.debug("volume %r: the probe chose %s", vtbl.description, results[0][0].name)
    return results[0][0].name


def probe_rows(
    stream: bytes, vtbl: VtblEntry, parser: str
) -> tuple[tuple[tuple[str, str], ...], tuple[str, ...]]:
    """The named parser's probe of ``stream``: detail rows, and the warnings it raised.

    What ``qicsilver extract`` writes into a TWVL header's ``fileset.probe``
    and ``warnings`` (TWS-3 sections 3.4, 3.5) once the volume is built:
    whether the parser's reading passed, its score and entry count, each
    check it failed, and its caveats (``directory-damaged``,
    ``special-fs``...). A probe that passed with a failed check is
    ``low-confidence``, as extract marks it. When the probe fails, extract
    goes on to the other surviving profiles' parsers; the viewer shows the
    chosen profile's verdict and says so rather than re-deciding.
    """
    try:
        fileset = fs.get(parser)
    except ValueError as exc:
        log.debug("volume %r: %s; no probe", vtbl.description, exc)
        return ((("probe", f"no file-set parser named {parser!r}"),), ())
    probe = fileset.probe(twvl_volume(stream, vtbl), vtbl, _SOURCE)
    warnings = set(probe.warnings)
    failed = f"{len(probe.failures)} of {len(probe.checks)} checks failed"
    next_profile = "qicsilver extract would try the next profile"
    if probe.fatal is not None:
        verdict = f"FAILED: {probe.fatal} ({next_profile})"
    elif not probe.passed:
        verdict = f"FAILED: score {probe.score}, {failed} ({next_profile})"
    else:
        verdict = f"passed: score {probe.score}, {probe.entries} entries"
        if probe.failures:
            verdict += f", {failed}"
            warnings.add(VolumeWarning.LOW_CONFIDENCE)
    rows = (("probe", verdict), *((f"failed check: {c.name}", c.detail) for c in probe.failures))
    return rows, tuple(warning_names(warnings))


def _unparsed_span(stream: bytes, vtbl: VtblEntry, parser: str) -> Span:
    """One span over the whole stream for a volume nothing here can lay out, saying why."""
    if parser == fs.NONE:
        label = "No file-set parser"
        note = "the volume profile recognises this format, but no parser can list it yet"
    else:
        label = f"{parser}: not drawn"
        note = f"cinnabar has no annotator for the {parser} file-set parser"
    log.debug("volume %r: %s", vtbl.description, note)
    return Span(
        0,
        max(len(stream), 1),
        label,
        parser,
        "no-parser",
        Shape.BLOCK,
        (("file-set parser", parser), ("note", note)),
    )


def _volume_spans(
    stream: bytes, vtbl: VtblEntry, *, decompressed: bool = False, parser: str | None = None
) -> list[Span]:
    """Level-0 spans of an uncompressed volume's byte stream: its directory and data sections.

    ``parser`` is the file-set parser the volume profile names for the
    tape, and it decides the layout as it does for ``qicsilver extract``:
    ``qic113-extended`` is QIC-113 section 8, ``Data Section + gap +
    Directory Section`` placed from the directory; ``qic113-basic`` is
    section 7, ``Directory Section + Data Section``, or directory last
    when flag byte 56 bit 5 says so; ``none`` is a format the profile
    recognises but nothing can list, drawn as one span saying so, and a
    parser cinnabar has no annotator for is drawn the same way under its
    name. None (a hand-built stream, no verdict) probes every parser on
    the bytes (:func:`probe_parser`). Whether the record "is Extended-OS"
    is never read off its bytes here: ``qiclib.qic113.is_extended_os``
    once was the test, and the unk-co tape's record passes it with the
    vendor bit clear; the profile and its parser's probe are what settled
    it. A compressed volume has no structure in the stream (see
    :func:`extent_spans`), so this returns nothing for it.
    """
    if vtbl.compressed and not decompressed:
        return []
    if parser is None:
        parser = probe_parser(stream, vtbl)
    if parser not in (_BASIC, _EXTENDED):
        return [_unparsed_span(stream, vtbl, parser)]
    extended = parser == _EXTENDED
    if extended or vtbl.directory_last:
        if extended:
            placed = _extended_directory_at(vtbl)
            if placed is None:
                return []
            dir_at = placed
            data = _extended_data(stream, dir_at)
            directory = _extended_directory(stream, dir_at)
        else:
            try:
                dir_at = qic113._directory_last_offset(stream, vtbl)
            except Exception:  # noqa: BLE001 -- a hole or garbage: no directory to show
                dir_at = len(stream)
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


def volume_spans(
    stream: bytes, vtbl: VtblEntry, *, decompressed: bool = False, parser: str | None = None
) -> list[Span]:
    """:func:`_volume_spans` with rows assigned: sections, entries, entry parts.

    ``decompressed`` says ``stream`` is a compressed volume's bytes *after*
    QIC-122 (``cinnabar.load.volumes.Volume.decompressed``), which have the
    same structure as an uncompressed volume's; without it a compressed
    entry's stream is taken as still-compressed and has no structure to show.
    ``parser`` is the volume profile's file-set parser by name
    (:attr:`cinnabar.model.VolumeReading.parser`); None probes for one.
    """
    return [
        assign_rows(s, VOLUME_ROWS)
        for s in _volume_spans(stream, vtbl, decompressed=decompressed, parser=parser)
    ]


# ---------------------------------------------------------------------------
# Compressed volumes: extents and frames, per segment
# ---------------------------------------------------------------------------

_RAW_FRAME = 0x8000
_TAIL_FILL = 18  # QIC-113 section 9.1.4: a frame never starts in the last 18 bytes


def _extent_spans(
    data: bytes, *, spanning: bool, offset_bytes: int = 8, frame_size_bytes: int = 2
) -> list[Span]:
    """One segment of a compressed volume: its Compression Extent and frames (QIC-113 section 9).

    With segment spanning the first two bytes are the Next Extent Offset: 2
    means an extent starts right after, 0 means the whole segment continues
    the previous frame, more means that many bytes of continuation first.
    ``offset_bytes`` is the width of the extent's Uncompressed Volume Byte
    Offset: 8 per Rev G, 4 on the MTN tapes; ``frame_size_bytes`` the
    width of each frame's size field: 2 per Rev G section 9.1.4, 4 on
    NovaStor tapes, with the raw flag in bit 15 either way. Both are the
    volume profile's ``[extent]`` widths, the ones
    :func:`qiclib.qic122.decode_extent` decompresses with.
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
    if pos + offset_bytes + frame_size_bytes > len(data):
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
        size = int.from_bytes(data[p : p + frame_size_bytes], "little")
        n = size & (_RAW_FRAME - 1)
        if n == 0:
            frames.append(Span(p, len(data), "fill", "", "fill"))
            break
        raw = bool(size & _RAW_FRAME)
        end = min(p + frame_size_bytes + n, len(data))
        frames.append(
            Span(
                p,
                end,
                "raw" if raw else "frame",
                f"{n:,} B",
                "frame-raw" if raw else "frame",
                Shape.CHEVRON,
                (("size", str(n)), ("raw", "yes" if raw else "no (QIC-122)")),
                (Span(p, p + frame_size_bytes, "size", "", "frame-size", Shape.HEX),),
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


def extent_spans(
    data: bytes, *, spanning: bool, offset_bytes: int = 8, frame_size_bytes: int = 2
) -> list[Span]:
    """:func:`_extent_spans` with rows assigned: extent, frames, frame fields."""
    return [
        assign_rows(s, EXTENT_ROWS)
        for s in _extent_spans(
            data, spanning=spanning, offset_bytes=offset_bytes, frame_size_bytes=frame_size_bytes
        )
    ]

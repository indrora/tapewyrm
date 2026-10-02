"""`tw identify`: what is on a tape, read from its first few segments.

Everything that describes a QIC-40/80 tape sits at the very start of track 0
(DESIGN.md §7.3):

  * the **header segment** -- the first defect-free segment, with a duplicate
    right behind it -- whose sector 0 is the format parameter record (tape name,
    geometry, format/write dates, format count, lifetime segments written) and
    whose data area carries the bad-sector map;
  * the **volume table** -- segment ``first_data_seg``, the first segment of the
    logical area -- one 128-byte ``VTBL`` entry per backup set.

On the bench tape those are segments 0, 1 and 2: about two seconds of tape.
So this module answers "what is this tape?" without a full dump or a full
decode: it finds the header, RS-corrects only the header and the volume-table
segment, and reports. Nothing here touches the Reed-Solomon solve of any other
segment, nor the QIC-113 layer.

Sources, all offline:

  * a TWTI image (``tw convert`` output) -- already placed and corrected, so
    this is just two segment reads;
  * TWRF captures, legacy ``.raw`` streams, or a dump directory -- the flux is
    decoded to sectors (the slow part), then only the header and volume table
    are corrected. A capture of just the start of track 0 is enough.

The live-drive path (wind to BOT, capture a few seconds, identify) is built on
:func:`from_sectors`.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from tapewyrm.codec import merge
from tapewyrm.codec import segment as seg_mod
from tapewyrm.codec import volume as volume_mod
from tapewyrm.codec.volume import BadSectorMap, VolumeInfo, VtblEntry
from tapewyrm.image import twti
from tapewyrm.tape.geometry import Geometry
from tapewyrm.types import RawSector, SegmentStatus

# The placement used before the header tells us the real geometry. Only the
# header's own position has to come out right under it, and the header sits at
# the origin of track 0, which places the same under any geometry (twti.convert
# uses the same default).
FALLBACK_GEOMETRY = Geometry(tracks=28, segments_per_track=207)

DATA_BYTES_PER_SEGMENT = volume_mod.DATA_SECTORS_PER_SEGMENT * 1024


@dataclass
class TapeInfo:
    """What the header segment and volume table say about a tape.

    Segment states are lower-case strings shared by both sources: ``clean``,
    ``corrected``, ``uncorrectable``, ``missing`` (never read) and, for images,
    ``bad`` (the bad-sector map marks the whole segment unusable).
    """

    vol: VolumeInfo
    bsm: BadSectorMap
    volumes: list[VtblEntry]
    header_seg: int  # the copy we actually read (header_seg or dup_header_seg)
    header_state: str
    vtbl_seg: int | None  # None when no volume-table segment could be found
    vtbl_state: str
    notes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------


def identify(path: Path, *, log: Callable[[str], None] | None = None) -> TapeInfo:
    """Identify a tape from a TWTI image, TWRF/raw capture(s) or a dump directory.

    TWTI is recognised by its magic rather than its suffix, so a renamed image
    still takes the fast path.
    """
    if path.is_file():
        with path.open("rb") as f:
            magic = f.read(len(twti.MAGIC))
        if magic == twti.MAGIC:
            return from_image(twti.TapeImage.open(path))
    return from_captures([path], log=log)


def from_captures(sources: Iterable[Path], *, log: Callable[[str], None] | None = None) -> TapeInfo:
    """Decode capture flux to sectors (merging passes), then :func:`from_sectors`."""
    files = twti.capture_files(sources)
    if not files:
        raise ValueError("no track captures found")
    passes: list[list[RawSector]] = []
    for path in files:
        sectors, meta = twti.decode_capture(path)
        if log is not None:
            log(f"{path.name}: {meta['sectors']} sectors")
        passes.append(sectors)
    return from_sectors(merge.union(passes))


def from_sectors(sectors: Iterable[RawSector]) -> TapeInfo:
    """Identify from recovered sectors: locate the header, correct the volume table.

    Raises ``ValueError`` when there is no header segment among the sectors --
    without it there is no geometry, no bad-sector map and no volume table.
    """
    located = volume_mod.locate_header(sectors, FALLBACK_GEOMETRY)
    if located is None:
        raise ValueError("no header segment found: the capture must include the start of track 0")
    vol = located.vol
    notes: list[str] = []
    header_state = located.header_status.value
    _note_header_copy(notes, vol, located.header.seg)

    by_abs = {seg.seg: seg for seg in located.segs.values()}
    vtbl_seg = by_abs.get(vol.first_data_seg) if vol.first_data_seg else None
    if vtbl_seg is None:
        # The header should always name the first data segment; if it doesn't,
        # or that segment never made it into the capture, fall back to looking
        # for a raw VTBL signature anywhere we have data.
        vtbl_seg = volume_mod.find_volume_table_segment(located.segs)
        if vtbl_seg is not None and vol.first_data_seg:
            notes.append(
                f"volume table found by signature in segment {vtbl_seg.seg}, "
                f"not the header's first data segment {vol.first_data_seg}"
            )
    if vtbl_seg is None:
        notes.append(_missing_vtbl_note(vol))
        return TapeInfo(
            vol=vol,
            bsm=located.bsm,
            volumes=[],
            header_seg=located.header.seg,
            header_state=header_state,
            vtbl_seg=vol.first_data_seg or None,
            vtbl_state=SegmentStatus.MISSING.value,
            notes=notes,
        )

    res = seg_mod.correct_segment(vtbl_seg)
    volumes: list[VtblEntry] = []
    if res.status is SegmentStatus.UNCORRECTABLE:
        notes.append(
            f"volume table segment {vtbl_seg.seg} is uncorrectable "
            f"({res.erasure_count} sectors bad or missing); re-capture the start of track 0"
        )
    else:
        volumes = volume_mod.parse_volume_table_data(res.data)
        notes += _extension_notes(res.data)
    return TapeInfo(
        vol=vol,
        bsm=located.bsm,
        volumes=volumes,
        header_seg=located.header.seg,
        header_state=header_state,
        vtbl_seg=vtbl_seg.seg,
        vtbl_state=res.status.value,
        notes=notes,
    )


def from_image(img: twti.TapeImage) -> TapeInfo:
    """Identify from a TWTI image: re-read the header and volume table segments.

    The image header already carries the parsed format parameter record, but we
    re-parse the stored header segment instead: that yields the bad-sector map
    too (the JSON only has ``qic80_header``), and it keeps working if
    :class:`VolumeInfo` gains fields that older images never stored.
    """
    q80 = img.header.get("qic80_header", {})
    notes: list[str] = []
    header_seg = None
    for candidate in (q80.get("header_seg", 0), q80.get("dup_header_seg")):
        if candidate is None or candidate >= len(img.entries):
            continue
        if _image_state(img, candidate) in ("clean", "corrected"):
            header_seg = candidate
            break
    if header_seg is None:
        raise ValueError("neither copy of the header segment was recovered in this image")
    vol, bsm = volume_mod.parse_header_data(img.segment(header_seg))
    if not vol.valid_signature:
        raise ValueError(f"segment {header_seg} does not hold a format parameter record")
    _note_header_copy(notes, vol, header_seg)

    vt = vol.first_data_seg
    volumes: list[VtblEntry] = []
    if not vt or vt >= len(img.entries):
        notes.append(_missing_vtbl_note(vol))
        vtbl_state = SegmentStatus.MISSING.value
    else:
        vtbl_state = _image_state(img, vt)
        if vtbl_state in ("clean", "corrected"):
            data = img.segment(vt)
            volumes = volume_mod.parse_volume_table_data(data)
            notes += _extension_notes(data)
        else:
            notes.append(f"volume table segment {vt} is {vtbl_state} in this image")
    return TapeInfo(
        vol=vol,
        bsm=bsm,
        volumes=volumes,
        header_seg=header_seg,
        header_state=_image_state(img, header_seg),
        vtbl_seg=vt or None,
        vtbl_state=vtbl_state,
        notes=notes,
    )


def _image_state(img: twti.TapeImage, n: int) -> str:
    return img.entries[n].state.name.lower()


def _note_header_copy(notes: list[str], vol: VolumeInfo, read_from: int) -> None:
    """Say so when the header came from the duplicate (the first copy is damaged)."""
    if vol.header_seg != read_from:
        notes.append(
            f"header read from segment {read_from}; the first copy "
            f"(segment {vol.header_seg}) could not be read"
        )


def _missing_vtbl_note(vol: VolumeInfo) -> str:
    if not vol.first_data_seg:
        return "the header names no first data segment, and no volume table was found"
    return (
        f"volume table segment {vol.first_data_seg} was not captured; capture further into track 0"
    )


def _extension_notes(data: bytes) -> list[str]:
    """Notes for volume-table records that are not file sets (QIC-80-MC Rev N §8.1-8.3).

    ``parse_volume_table_data`` recognises these and skips them. ``EXVT`` matters
    to a reader: it means the table continues in another segment, so the
    volume list printed here is incomplete.
    """
    notes: list[str] = []
    for off in range(0, len(data) - volume_mod.VTBL_ENTRY_LEN + 1, volume_mod.VTBL_ENTRY_LEN):
        sig = data[off : off + 4]
        if sig == b"\x00\x00\x00\x00":
            break
        if sig == volume_mod.SIG_EXVT:
            child = int.from_bytes(data[off + 6 : off + 8], "little")
            # TODO: follow EXVT chains (same TODO as volume.parse_volume_table_data).
            notes.append(
                f"volume table continues in segment {child} (EXVT); "
                "volumes listed there are not shown"
            )
        elif sig == volume_mod.SIG_UTID:
            notes.append("tape also carries a unicode name (UTID), not decoded")
    return notes


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def _date(packed: int) -> str:
    when = volume_mod.decode_short_date(packed)
    if when is None:
        return "-"
    return "{:04d}-{:02d}-{:02d} {:02d}:{:02d}:{:02d}".format(*when)


def _size(entry: VtblEntry) -> str:
    """Volume size: exact from QIC-113 section sizes when known, else from segments."""
    if entry.data_section_size is not None or entry.dir_section_size is not None:
        exact = (entry.data_section_size or 0) + (entry.dir_section_size or 0)
        if exact:
            return _human(exact)
    span = max(0, entry.end_seg - entry.start_seg + 1) * DATA_BYTES_PER_SEGMENT
    return "~" + _human(span)  # on-tape space, compressed or not


def _human(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    raise AssertionError("unreachable")


def _yes_no(flag: bool | None) -> str:
    return "?" if flag is None else ("yes" if flag else "no")


def format_info(info: TapeInfo) -> list[str]:
    """Human-readable report, one line per string."""
    vol = info.vol
    total = vol.segments_per_track * vol.tracks
    lines = [
        f"tape name     {vol.tape_name or '-'}  (named {_date(vol.name_date)})",
        f"format        code {vol.format_code}, revision 0x{vol.revision:02x}; "
        f"{vol.tracks} tracks x {vol.segments_per_track} segments = {total} segments",
        f"formatted     {_date(vol.format_date)}  (first {_date(vol.initial_format_date)}, "
        f"{vol.format_count} times)",
        f"last written  {_date(vol.write_date)}",
        f"lifetime      {vol.segments_written} segments written",
        f"bad sectors   {len(info.bsm.bad_lsns)} sectors + "
        f"{len(info.bsm.bad_segments)} whole segments in the map",
        f"header        segment {info.header_seg} ({info.header_state}); "
        f"copies at {vol.header_seg} and {vol.dup_header_seg}",
        f"volume table  segment {info.vtbl_seg if info.vtbl_seg is not None else '-'} "
        f"({info.vtbl_state}); {len(info.volumes)} volume(s)",
    ]
    if vol.reformat_error:
        lines.append("WARNING       a re-format error lost some header fields (byte 128 = 0xFF)")
    if info.volumes:
        lines.append("")
        lines.append(f"  {'#':>2}  {'segments':<11}  {'date':<19}  {'size':>10}  comp  description")
        for k, entry in enumerate(info.volumes):
            desc = entry.description
            if entry.source_label:
                desc += f"  [{entry.source_label}]"
            if entry.vendor_specific:
                desc += "  (vendor-specific)"
            lines.append(
                f"  {k:>2}  {f'{entry.start_seg}-{entry.end_seg}':<11}  {_date(entry.date):<19}  "
                f"{_size(entry):>10}  {_yes_no(entry.compressed):<4}  {desc}"
            )
    for note in info.notes:
        lines.append(f"note: {note}")
    return lines


def to_dict(info: TapeInfo) -> dict[str, Any]:
    """JSON-ready form for ``tw identify --json``: raw fields plus decoded dates."""
    vol = asdict(info.vol)
    for key in ("format_date", "write_date", "name_date", "initial_format_date"):
        vol[key + "_decoded"] = _date(vol[key])
    volumes = []
    for entry in info.volumes:
        row = asdict(entry)
        row.pop("raw")  # bytes; not JSON, and the parsed fields cover it
        row["signature"] = entry.signature.decode("ascii", errors="replace")
        row["date_decoded"] = _date(entry.date)
        volumes.append(row)
    return {
        "header": vol,
        "bad_sectors": sorted(info.bsm.bad_lsns),
        "bad_segments": sorted(info.bsm.bad_segments),
        "header_seg": info.header_seg,
        "header_state": info.header_state,
        "vtbl_seg": info.vtbl_seg,
        "vtbl_state": info.vtbl_state,
        "volumes": volumes,
        "notes": info.notes,
    }

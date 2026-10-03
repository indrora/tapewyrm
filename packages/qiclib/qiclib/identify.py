"""`qicsilver identify`: what is on a tape, read from its first few segments.

Everything that describes a QIC-40/80 tape sits at the very start of track 0
(DESIGN.md §7.3):

  * the **header segment** -- the first defect-free segment, with a duplicate
    right behind it -- whose sector 0 is the format parameter record (tape name,
    geometry, format/write dates, format count, lifetime segments written, and
    for pre-formatted tapes the manufacturer's stamp and lot code) and whose
    data area carries the bad-sector map;
  * the **volume table** -- segment ``first_data_seg``, the first segment of the
    logical area -- one 128-byte ``VTBL`` entry per backup set.

On the bench tapes those are segments 0, 1 and 2: about two seconds of tape.
So this module answers "what is this tape?" without a full dump or a full
decode: it finds the header, RS-corrects only the header and the volume-table
segment, and reports. Nothing here touches the Reed-Solomon solve of any other
segment, nor the QIC-113 layer.

On top of the raw fields it makes two guesses:

  * **the cartridge** (:mod:`qiclib.cartridge`) from tracks and
    segments per track, cross-checked against what the drive itself reported
    when the source is a TWRF capture;
  * **the tape profile** (:mod:`qiclib.tape_profile`): which software's
    volume-table layout fits, since only bytes 0-56 of an entry are universal.

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

import logging
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from tapewyrm_archive import twti
from tapewyrm_archive.progress import NULL_PROGRESS, Progress
from tapewyrm_archive.qic117 import TAPE_TYPES, DriveConfig, TapeStatus, decode_vendor_id

from qiclib import cartridge
from qiclib import segment as seg_mod
from qiclib import tape_profile as tp
from qiclib import volume as volume_mod
from qiclib.geometry import Geometry
from qiclib.types import RawSector, SegmentStatus
from qiclib.volume import BadSectorMap, VolumeInfo, VtblEntry

log = logging.getLogger(__name__)

# The placement used before the header tells us the real geometry. Only the
# header's own position has to come out right under it, and the header sits at
# the origin of track 0, which places the same under any geometry (twti.convert
# uses the same default).
FALLBACK_GEOMETRY = Geometry(tracks=28, segments_per_track=207)

DATA_BYTES_PER_SEGMENT = volume_mod.DATA_SECTORS_PER_SEGMENT * 1024
FPR_LEN = 256  # the format parameter record is bytes 0-255 of sector 0 (Rev N §7.1)

# Bytes of the format parameter record that QIC-80-MC Rev N §7.1 calls
# "unused, set to zero". `-v` reports any that are not: on fixed-format tapes
# (codes 2, 3, 5) the older Rev K, which we do not have, may define them -- the
# 3M bench tape has byte 78 = 0x03 and bytes 144-145 = 0x0002.
UNUSED_FPR_RANGES = ((22, 24), (78, 128), (129, 130), (134, 138), (144, 146), (234, 256))

FORMAT_CODES = {
    2: "fixed format (QIC-80-MC Rev K)",
    3: "fixed format (QIC-80-MC Rev K)",
    4: "variable length (QIC-80-MC Rev N)",
    5: "fixed format (QIC-80-MC Rev K)",
}
# Rev N §7.1 byte 5: "Revision M = Hex '0D', Revision L = Hex '0C', etc.,
# Revisions prior to L = '00'." Only the values it names are named here.
REVISIONS = {0x00: "before Rev L", 0x0C: "Rev L", 0x0D: "Rev M"}


@dataclass
class TapeInfo:
    """What the header segment and volume table say about a tape.

    Segment states are lower-case strings shared by both sources: ``clean``,
    ``corrected``, ``uncorrectable``, ``missing`` (never read) and, for images,
    ``bad`` (the bad-sector map marks the whole segment unusable).

    ``verdicts`` holds every tape profile's reading of the volume table, best
    first; with ``--tape-profile NAME`` it holds just that one. ``volumes`` is
    the chosen reading.
    """

    vol: VolumeInfo
    bsm: BadSectorMap
    header_raw: bytes  # format parameter record, bytes 0-255 of sector 0
    header_seg: int  # the copy we actually read (header_seg or dup_header_seg)
    header_state: str
    vtbl_seg: int | None  # None when no volume-table segment could be found
    vtbl_state: str
    vtbl_records: list[bytes]
    verdicts: list[tp.Verdict]
    cartridge: cartridge.CartridgeGuess
    drive: dict | None = None  # TWRF header of the capture, when there is one
    notes: list[str] = field(default_factory=list)

    @property
    def volumes(self) -> list[VtblEntry]:
        return self.verdicts[0].entries if self.verdicts else []

    @property
    def profile(self) -> str:
        return self.verdicts[0].profile.name if self.verdicts else ""


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------


def identify(
    path: Path,
    *,
    tape_profile: str = tp.GUESS,
    progress: Progress = NULL_PROGRESS,
) -> TapeInfo:
    """Identify a tape from a TWTI image.

    TWTI is recognised by its magic rather than its suffix, so a renamed image
    still works. Raw captures need the physical decoder: ``tw convert`` them
    first .
    """
    if path.is_file():
        log.debug("%s: reading magic to check for a TWTI image", path)
        with path.open("rb") as f:
            magic = f.read(len(twti.MAGIC))
        if magic == twti.MAGIC:
            log.debug("%s: TWTI magic; identifying from the image", path)
            return from_image(twti.TapeImage.open(path), tape_profile=tape_profile)
        log.debug("%s: magic %r != %r; refusing", path, magic, twti.MAGIC)
    else:
        log.debug("%s: not a file; refusing", path)
    raise ValueError(f"{path} is not a TWTI tape image; run `tw convert` on captures first")


def from_sectors(
    sectors: Iterable[RawSector],
    *,
    tape_profile: str = tp.GUESS,
    drive: dict | None = None,
) -> TapeInfo:
    """Identify from recovered sectors: locate the header, correct the volume table.

    Raises ``ValueError`` when there is no header segment among the sectors --
    without it there is no geometry, no bad-sector map and no volume table.
    """
    log.debug("locating the header segment under fallback geometry %s", FALLBACK_GEOMETRY)
    located = volume_mod.locate_header(sectors, FALLBACK_GEOMETRY)
    if located is None:
        log.debug("locate_header found no header segment; refusing")
        raise ValueError("no header segment found: the capture must include the start of track 0")
    vol = located.vol
    notes: list[str] = []
    _note_header_copy(notes, vol, located.header.seg)
    header_raw = _header_raw(located.header)

    by_abs = {seg.seg: seg for seg in located.segs.values()}
    vtbl_seg = by_abs.get(vol.first_data_seg) if vol.first_data_seg else None
    log.debug(
        "header at segment %d (%s); first_data_seg %r %s",
        located.header.seg,
        located.header_status.value,
        vol.first_data_seg,
        "captured" if vtbl_seg is not None else "not captured",
    )
    if vtbl_seg is None:
        log.debug("searching %d segments for a VTBL signature", len(located.segs))
        # The header should always name the first data segment; if it doesn't,
        # or that segment never made it into the capture, fall back to looking
        # for a raw VTBL signature anywhere we have data.
        vtbl_seg = volume_mod.find_volume_table_segment(located.segs)
        if vtbl_seg is not None and vol.first_data_seg:
            notes.append(
                f"volume table found by signature in segment {vtbl_seg.seg}, "
                f"not the header's first data segment {vol.first_data_seg}"
            )

    vtbl_data: bytes | None = None
    if vtbl_seg is None:
        log.debug("no volume table segment found; continuing without volumes")
        notes.append(_missing_vtbl_note(vol))
        vtbl_at, vtbl_state = vol.first_data_seg or None, SegmentStatus.MISSING.value
    else:
        log.debug("correcting volume table segment %d", vtbl_seg.seg)
        res = seg_mod.correct_segment(vtbl_seg)
        vtbl_at, vtbl_state = vtbl_seg.seg, res.status.value
        if res.status is SegmentStatus.UNCORRECTABLE:
            log.debug(
                "volume table segment %d uncorrectable (%d erasures); continuing without volumes",
                vtbl_seg.seg,
                res.erasure_count,
            )
            notes.append(
                f"volume table segment {vtbl_seg.seg} is uncorrectable "
                f"({res.erasure_count} sectors bad or missing); re-capture the start of track 0"
            )
        else:
            vtbl_data = res.data
    return _assemble(
        vol,
        located.bsm,
        header_raw,
        located.header.seg,
        located.header_status.value,
        vtbl_at,
        vtbl_state,
        vtbl_data,
        notes,
        tape_profile,
        drive,
    )


def from_image(img: twti.TapeImage, *, tape_profile: str = tp.GUESS) -> TapeInfo:
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
            log.debug(
                "header candidate %r absent or past %d segments; skipping",
                candidate,
                len(img.entries),
            )
            continue
        if _image_state(img, candidate) in ("clean", "corrected"):
            header_seg = candidate
            break
        log.debug(
            "header candidate %d is %s; trying the next copy",
            candidate,
            _image_state(img, candidate),
        )
    if header_seg is None:
        log.debug("no header copy clean or corrected; refusing")
        raise ValueError("neither copy of the header segment was recovered in this image")
    log.debug("parsing header segment %d", header_seg)
    header_data = img.segment(header_seg)
    vol, bsm = volume_mod.parse_header_data(header_data)
    if not vol.valid_signature:
        log.debug(
            "segment %d starts %r, not the FPR signature; refusing", header_seg, header_data[:4]
        )
        raise ValueError(f"segment {header_seg} does not hold a format parameter record")
    _note_header_copy(notes, vol, header_seg)

    vt = vol.first_data_seg
    vtbl_data: bytes | None = None
    if not vt or vt >= len(img.entries):
        log.debug(
            "first_data_seg %r is zero or past %d segments; no volume table",
            vt,
            len(img.entries),
        )
        notes.append(_missing_vtbl_note(vol))
        vtbl_state = SegmentStatus.MISSING.value
    else:
        vtbl_state = _image_state(img, vt)
        if vtbl_state in ("clean", "corrected"):
            vtbl_data = img.segment(vt)
        else:
            log.debug("volume table segment %d is %s; continuing without volumes", vt, vtbl_state)
            notes.append(f"volume table segment {vt} is {vtbl_state} in this image")
    # TWTI keeps the drive's reports per source capture; the first one with
    # them speaks for the drive, as in from_captures.
    drive = next(
        (s["twrf"] for s in img.header.get("sources", []) if "tape_status" in s.get("twrf", {})),
        None,
    )
    return _assemble(
        vol,
        bsm,
        header_data[:FPR_LEN],
        header_seg,
        _image_state(img, header_seg),
        vt or None,
        vtbl_state,
        vtbl_data,
        notes,
        tape_profile,
        drive,
    )


def _assemble(
    vol: VolumeInfo,
    bsm: BadSectorMap,
    header_raw: bytes,
    header_seg: int,
    header_state: str,
    vtbl_seg: int | None,
    vtbl_state: str,
    vtbl_data: bytes | None,
    notes: list[str],
    tape_profile: str,
    drive: dict | None,
) -> TapeInfo:
    """The source-independent half: pick a profile, guess the cartridge, gather notes."""
    records = volume_mod.vtbl_records(vtbl_data) if vtbl_data is not None else []
    log.debug("volume table: %d records", len(records))
    if vtbl_data is not None:
        notes += _extension_notes(vtbl_data)
    if tape_profile == tp.GUESS:
        if not records:
            log.debug("no volume table records; no tape profile to guess")
        else:
            log.debug("guessing tape profile from %d records", len(records))
        verdicts = tp.guess(records, vol) if records else []
        if len(verdicts) > 1 and verdicts[0].score == verdicts[1].score:
            log.debug(
                "profiles %s and %s tie at score %d; using the first",
                verdicts[0].profile.name,
                verdicts[1].profile.name,
                verdicts[0].score,
            )
            notes.append(
                f"tape profiles {verdicts[0].profile.name} and {verdicts[1].profile.name} "
                "fit equally well; showing the first. Pick one with --tape-profile"
            )
    else:
        log.debug("loading forced tape profile %r", tape_profile)
        verdicts = [tp.evaluate(records, vol, tp.load(tape_profile))]
    guess = cartridge.guess(vol.tracks, vol.segments_per_track)
    return TapeInfo(
        vol=vol,
        bsm=bsm,
        header_raw=header_raw,
        header_seg=header_seg,
        header_state=header_state,
        vtbl_seg=vtbl_seg,
        vtbl_state=vtbl_state,
        vtbl_records=records,
        verdicts=verdicts,
        cartridge=guess,
        drive=drive,
        notes=notes,
    )


def _header_raw(seg) -> bytes:
    """The header segment's format parameter record (RS-corrected if needed)."""
    data = seg_mod.segment_data(seg)
    if data[:4] != volume_mod.FPR_SIGNATURE:
        log.debug(
            "header segment %d raw data starts %r, not the FPR signature; RS-correcting",
            seg.seg,
            data[:4],
        )
        data = seg_mod.correct_segment(seg).data
    return data[:FPR_LEN]


def _image_state(img: twti.TapeImage, n: int) -> str:
    return img.entries[n].state.name.lower()


def _note_header_copy(notes: list[str], vol: VolumeInfo, read_from: int) -> None:
    """Say so when the header came from the duplicate (the first copy is damaged)."""
    if vol.header_seg != read_from:
        log.debug("header read from segment %d, not first copy %d", read_from, vol.header_seg)
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

    ``vtbl_records`` recognises these and skips them. ``EXVT`` matters to a
    reader: it means the table continues in another segment, so the volume
    list printed here is incomplete.
    """
    notes: list[str] = []
    for off in range(0, len(data) - volume_mod.VTBL_ENTRY_LEN + 1, volume_mod.VTBL_ENTRY_LEN):
        sig = data[off : off + 4]
        if sig == b"\x00\x00\x00\x00":
            log.debug("volume table: zero signature at offset %d; end of records", off)
            break
        if sig == volume_mod.SIG_EXVT:
            child = int.from_bytes(data[off + 6 : off + 8], "little")
            log.debug("volume table: EXVT at offset %d -> segment %d (not followed)", off, child)
            # TODO: follow EXVT chains (same TODO as volume.vtbl_records).
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
    """Volume size: from the section sizes when the profile knows them, else on-tape span."""
    exact = (entry.data_section_size or 0) + (entry.dir_section_size or 0)
    if exact:
        return _human(exact)
    span = max(0, entry.end_seg - entry.start_seg + 1) * DATA_BYTES_PER_SEGMENT
    return "~" + _human(span)  # on-tape space, compressed or not


def _human(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    raise AssertionError("unreachable")


def _compression(entry: VtblEntry) -> str:
    if entry.compressed is None:
        return "?"
    if not entry.compressed:
        return "no"
    return "QIC-122" if entry.compression_code == 1 else f"code {entry.compression_code}"


def _hexdump(data: bytes, indent: str = "    ") -> list[str]:
    lines = []
    for off in range(0, len(data), 16):
        row = data[off : off + 16]
        text = "".join(chr(b) if 32 <= b < 127 else "." for b in row)
        lines.append(f"{indent}{off:3d}  {row.hex(' '):<47}  {text}")
    return lines


def _unused_bytes(header_raw: bytes) -> list[str]:
    out = []
    for start, end in UNUSED_FPR_RANGES:
        chunk = header_raw[start:end]
        if any(chunk):
            out.append(f"{start}-{end - 1}: {chunk.rstrip(bytes(1)).hex(' ')}")
    return out


def _drive_line(drive: dict) -> str | None:
    """What the drive said about the tape when it was captured (QIC-117 reports)."""
    parts = []
    if drive.get("tape_status") is not None:
        st = TapeStatus.decode(drive["tape_status"])
        kind = TAPE_TYPES.get(st.tape_type, f"type {st.tape_type}")
        width = ", wide (0.315 in)" if st.wide else ""
        parts.append(f"{st.format.name}, {kind}{width} [tape status 0x{st.raw:02x}]")
    if drive.get("drive_config") is not None:
        cfg = DriveConfig.decode(drive["drive_config"])
        extra = ", extra-length tape" if cfg.extra_length else ""
        parts.append(f"{cfg.rate_kbps} kbps{extra} [config 0x{cfg.raw:02x}]")
    if drive.get("drive_vendor_id") is not None:
        _, _, name = decode_vendor_id(drive["drive_vendor_id"])
        parts.append(f"drive {name}")
    return "; ".join(parts) or None


def format_info(info: TapeInfo, *, verbose: bool = False) -> list[str]:
    """Human-readable report, one line per string."""
    vol = info.vol
    total = vol.segments_per_track * vol.tracks
    revision = REVISIONS.get(vol.revision, "unknown")
    lines = [
        f"tape name     {vol.tape_name or '-'}  (named {_date(vol.name_date)})",
    ]
    if vol.manufacturer or vol.lot_code:
        lines.append(f"manufacturer  {vol.manufacturer or '-'}  (lot {vol.lot_code or '-'})")
        lines.append("              factory pre-formatted (QIC-80-MC Rev N §7.1 bytes 146-233)")
    else:
        lines.append("manufacturer  - (no factory stamp: formatted by its owner)")
    lines.append(f"cartridge     {info.cartridge.describe()}")
    if info.drive:
        drive_line = _drive_line(info.drive)
        if drive_line:
            lines.append(f"drive saw     {drive_line}")
    lines += [
        f"format        code {vol.format_code}, {FORMAT_CODES.get(vol.format_code, 'unknown')}; "
        f"header revision 0x{vol.revision:02x} ({revision})",
        f"geometry      {vol.tracks} tracks x {vol.segments_per_track} segments = {total} segments; "
        f"floppy sides 0-{vol.max_fsd}, tracks 0-{vol.max_ftk}, sectors 1-{vol.max_fsc}",
        f"data area     segments {vol.first_data_seg}-{vol.last_data_seg}",
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
    if info.verdicts:
        best = info.verdicts[0]
        others = ", ".join(f"{v.profile.name} {v.score}" for v in info.verdicts[1:])
        line = f"tape profile  {best.profile.name} (score {best.score})"
        lines.append(line + (f"; others: {others}" if others else ""))
    if vol.reformat_error:
        lines.append("WARNING       a re-format error lost some header fields (byte 128 = 0xFF)")

    if info.volumes:
        lines.append("")
        lines.append(
            f"  {'#':>2}  {'segments':<11}  {'date':<19}  {'size':>10}  {'compression':<11}  description"
        )
        for k, entry in enumerate(info.volumes):
            desc = entry.description
            if entry.source_label:
                desc += f"  [{entry.source_label}]"
            if entry.multi_cartridge_seq:
                desc += f"  (cartridge {entry.multi_cartridge_seq})"
            if entry.vendor_specific:
                desc += "  (vendor-specific)"
            lines.append(
                f"  {k:>2}  {f'{entry.start_seg}-{entry.end_seg}':<11}  {_date(entry.date):<19}  "
                f"{_size(entry):>10}  {_compression(entry):<11}  {desc}"
            )
    for note in info.notes:
        lines.append(f"note: {note}")
    if verbose:
        lines += _verbose(info)
    return lines


def _verbose(info: TapeInfo) -> list[str]:
    lines = ["", "format parameter record (header sector 0, bytes 0-255):"]
    lines += _hexdump(info.header_raw.rstrip(bytes(1)).ljust(16, b"\x00"))
    unused = _unused_bytes(info.header_raw)
    if unused:
        lines.append("  bytes Rev N calls unused that are not zero:")
        lines += [f"    {u}" for u in unused]
    for k, rec in enumerate(info.vtbl_records):
        lines.append("")
        lines.append(f"volume {k} raw VTBL record:")
        lines += _hexdump(rec)
    for verdict in info.verdicts:
        lines.append("")
        lines.append(
            f"profile {verdict.profile.name}: score {verdict.score} -- {verdict.profile.description}"
        )
        for check in verdict.checks:
            mark = "ok  " if check.ok else "FAIL"
            lines.append(f"    {mark} {check.points:+d}  {check.name}: {check.detail}")
    return lines


def to_dict(info: TapeInfo) -> dict[str, Any]:
    """JSON-ready form for ``qicsilver identify --json``: raw fields plus decoded values."""
    vol = asdict(info.vol)
    for key in ("format_date", "write_date", "name_date", "initial_format_date"):
        vol[key + "_decoded"] = _date(vol[key])
    volumes = []
    for entry, rec in zip(info.volumes, info.vtbl_records, strict=True):
        row = asdict(entry)
        row.pop("raw")  # bytes: emitted as hex below
        row["raw_hex"] = rec.hex()
        row["signature"] = entry.signature.decode("ascii", errors="replace")
        row["date_decoded"] = _date(entry.date)
        volumes.append(row)
    guess = info.cartridge
    return {
        "header": vol,
        "header_raw_hex": info.header_raw.hex(),
        "bad_sectors": sorted(info.bsm.bad_lsns),
        "bad_segments": sorted(info.bsm.bad_segments),
        "header_seg": info.header_seg,
        "header_state": info.header_state,
        "vtbl_seg": info.vtbl_seg,
        "vtbl_state": info.vtbl_state,
        "cartridge": {
            "description": guess.describe(),
            "catalogue": asdict(guess.cartridge) if guess.cartridge else None,
            "estimated_ft": guess.estimated_ft,
            "exact": guess.exact,
        },
        "drive": info.drive,
        "tape_profile": info.profile,
        "tape_profile_scores": {v.profile.name: v.score for v in info.verdicts},
        "volumes": volumes,
        "notes": info.notes,
    }

"""Which segment holds what, and each volume's byte stream, for both loaders.

A tape's logical layout (QIC-80-MC Rev N section 7, QIC-113 Rev G section
5) is the same whether the segments came from an image's slots or from a
flux capture's sectors: the header segment (and its duplicate) carry the
format parameter record and the bad-sector map; the first data segment
carries the volume table; every other segment belongs to the volume whose
VTBL range covers it. :class:`Layout` answers "what is segment N" from a
:class:`qiclib.identify.TapeInfo` and a way to fetch a segment's data, and
builds each volume's byte stream the way :func:`qiclib.volume.volume_streams`
does (data areas end to end, BSM-bad segments skipped, missing ones zero)
so the volume annotators see the bytes ``qicsilver extract`` would.

A flux capture's segments land one at a time, and the tape is identified
as soon as its header has (long before most of them), so the layout covers
every segment of a volume's range from the start, reads each through
``fetch`` when asked (nothing yet for one that has not landed), and
re-parses a volume's structure, throttled, as its segments arrive
(:meth:`Volume.spans`).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

from qiclib import identify, qic122
from qiclib import volume_profile as vp
from qiclib.verdict import warning_names
from qiclib.volume import VtblEntry
from tapewyrm_archive.twvl import SparseVolume

from cinnabar.annotate import header as header_mod
from cinnabar.annotate import volume as volume_ann
from cinnabar.model import (
    SEGMENT_BYTES,
    Axis,
    AxisKind,
    ByteMap,
    ByteRun,
    Segment,
    Shape,
    Span,
    Track,
    VolumeReading,
)

log = logging.getLogger(__name__)

SOURCE_ROW = "Volume: Source segment"
REPARSE_S = 2.0  # at most one re-parse of a volume's structure per this many seconds


@dataclass(frozen=True)
class VolumePiece:
    """One tape segment's contribution to a volume's byte stream, decompressed."""

    offset: int  # in the volume's (uncompressed) byte stream
    data: bytes
    seg_abs: int  # the tape segment it came from


class Volume:
    """One VTBL entry: its segments' stream offsets and, lazily, its spans."""

    def __init__(
        self,
        entry: VtblEntry,
        bad: set[int],
        fetch: Callable[[int], bytes],
        reading: VolumeReading,
    ) -> None:
        self.entry = entry
        # How the profile that read the table says this volume is read: its
        # QIC-122 extent widths and its file-set parser (cinnabar.model.VolumeReading).
        self.reading = reading
        self._fetch = fetch
        # Absolute segment -> offset in the stream, for every segment of the
        # range: one that has not landed yet still has its place (and its
        # length; ``fetch`` gives nothing for it until it does). This used to
        # hold only the segments present at identification, which was fine
        # while that came late and left every later landing unlabelled once
        # it came as soon as the header did. Only a BSM-bad segment has no
        # place: it contributes no bytes (TWS-2 section 5.2).
        self.base: dict[int, int] = {}
        off = 0
        for seg_abs in range(entry.start_seg, entry.end_seg + 1):
            if seg_abs in bad:
                continue
            self.base[seg_abs] = off
            off += SEGMENT_BYTES
        self.length = off
        self._spans: list[Span] | None = None
        self._dirty = False  # a segment landed since the structure was parsed
        self._parsed_at = 0.0

    def stream(self) -> bytes:
        out = bytearray(self.length)
        for seg_abs, off in self.base.items():
            data = self._fetch(seg_abs)
            out[off : off + min(len(data), SEGMENT_BYTES)] = data[:SEGMENT_BYTES]
        return bytes(out)

    # -- the decompressed stream ---------------------------------------------

    def pieces(self) -> list[VolumePiece]:
        """The volume's byte stream as pieces, one per readable tape segment, in stream order.

        Uncompressed: each segment's data at its running offset (BSM-bad
        segments take no room, missing ones leave a hole). Compressed: each
        segment is a QIC-122 Compression Extent whose leading field says
        where its bytes go in the uncompressed stream (QIC-113 section 9);
        it is decompressed the way ``qicsilver extract`` does it
        (:func:`qiclib.qic122.decode_extent`, with the volume profile's
        offset and frame-size widths), and one that will not decode is a hole.
        """
        out: list[VolumePiece] = []
        for seg_abs, off in sorted(self.base.items(), key=lambda kv: kv[1]):
            data = self._fetch(seg_abs)
            if not data:
                continue
            if not self.entry.compressed:
                out.append(VolumePiece(off, data[:SEGMENT_BYTES], seg_abs))
                continue
            try:
                ext = qic122.decode_extent(
                    data,
                    offset_bytes=self.reading.extent_offset_bytes,
                    frame_size_bytes=self.reading.extent_frame_size_bytes,
                )
            except qic122.Qic122Error as exc:
                log.debug("segment %d: extent did not decode: %s", seg_abs, exc)
                continue
            if ext.data:
                out.append(VolumePiece(ext.uncompressed_offset, ext.data, seg_abs))
        return sorted(out, key=lambda p: p.offset)

    def stream_size(self, pieces: list[VolumePiece]) -> int:
        """The volume's length: the table's section sizes, or the pieces' extent if larger."""
        e = self.entry
        table = (e.data_section_size or 0) + (e.dir_section_size or 0)
        seen = max((p.offset + len(p.data) for p in pieces), default=0)
        return max(table, seen) if e.compressed else max(self.length, seen)

    def decompressed(self) -> tuple[bytes, list[VolumePiece]]:
        """The whole stream (holes zero-filled) and its pieces."""
        pieces = self.pieces()
        size = self.stream_size(pieces)
        sparse = SparseVolume(size=size)
        for p in pieces:
            sparse.add(p.offset, p.data)
        body, _missing = sparse.read(0, size)
        return body, pieces

    def landed(self) -> None:
        """A segment of this volume just arrived: the structure is stale."""
        self._dirty = True

    def invalidate(self) -> None:
        """Parse the structure afresh on the next ``spans`` (the decode is complete)."""
        self._spans = None
        self._dirty = False

    def spans(self) -> list[Span]:
        """The volume's structure in stream offsets, parsed on first use.

        A flux capture's segments land one at a time, so the structure is
        re-parsed as they do (``landed``), but at most every ``REPARSE_S``:
        a parse rebuilds the whole stream, hundreds of megabytes for a long
        volume, and landings come many per second. Between parses a landing
        segment gets the structure as last parsed, clipped to it, and the
        loader re-annotates every segment once the decode is complete
        (``invalidate``), so what is on screen is right in the end.
        """
        if self._spans is not None:
            if not self._dirty or time.monotonic() - self._parsed_at < REPARSE_S:
                return self._spans
        self._dirty = False
        self._parsed_at = time.monotonic()
        if self.entry.compressed:
            self._spans = []
        else:
            log.debug("parsing volume %r (%d bytes)", self.entry.description, self.length)
            try:
                self._spans = volume_ann.volume_spans(
                    self.stream(), self.entry, parser=self.reading.parser
                )
            except Exception:  # noqa: BLE001 -- a damaged volume still shows its segments
                log.exception("volume %r: structure not parsed", self.entry.description)
                self._spans = []
        return self._spans


def chosen_verdict(verdicts: list[vp.Verdict]) -> vp.Verdict | None:
    """The verdict the tape is read with: the best survivor of the fingerprint gate, else the best loser.

    ``qiclib.identify`` ranks the verdicts survivors first, each group by
    score, and reads the volumes with the first either way, noting when
    every profile was eliminated (its reading "may be wrong"); the viewer
    reads with the same one so it labels what ``extract`` extracts.
    :func:`qiclib.volume_profile.survivors` is the test, so the order is
    not relied on.
    """
    alive = vp.survivors(verdicts)
    if alive:
        return alive[0]
    if verdicts:
        log.debug(
            "every volume profile was eliminated; reading with %s anyway", verdicts[0].profile.name
        )
        return verdicts[0]
    log.debug("no volume table was read; no verdict")
    return None


def reading_of(verdict: vp.Verdict | None) -> VolumeReading:
    """A verdict as the plain record the annotators and widgets read (:class:`cinnabar.model.VolumeReading`).

    The profile's three layers, by name and number: the VTBL fields it
    places past byte 56 (and the bytes its fingerprint compared, as
    ``"match"``), its extent widths and its parser; with the verdict's
    score, whether its gate failed, and its warnings (the profile's own and
    the selection's, as ``qiclib.volume_profile.guess`` adds them).
    """
    if verdict is None:
        return VolumeReading("(none)", "")
    profile = verdict.profile
    fields = [(name, spec.offset, spec.length) for name, spec in profile.fields.items()]
    fields += [("match", off, len(expected)) for off, expected in profile.vtbl_bytes]
    return VolumeReading(
        profile=profile.name,
        parser=profile.fileset_parser,
        status=profile.status,
        score=verdict.score,
        eliminated=verdict.eliminated,
        warnings=tuple(warning_names(verdict.warnings)),
        fields=tuple(sorted(fields, key=lambda f: f[1])),
        extent_offset_bytes=profile.extent_offset_bytes,
        extent_frame_size_bytes=profile.extent_frame_size_bytes,
    )


class Layout:
    """What each segment of a tape holds, from its identification.

    ``verdict`` is the volume profile's reading the tape is read with
    (:func:`chosen_verdict`) and ``reading`` the same as plain data for the
    annotators and the window (:func:`reading_of`); every volume carries it.
    """

    def __init__(
        self,
        info: identify.TapeInfo,
        fetch: Callable[[int], bytes],
        *,
        dup_header: int | None = None,
    ) -> None:
        self.info = info
        self.verdict = chosen_verdict(info.verdicts)
        self.reading = reading_of(self.verdict)
        self.entries: list[VtblEntry] = self.verdict.entries if self.verdict is not None else []
        bad = set(info.bsm.bad_segments)
        self.volumes = [Volume(e, bad, fetch, self.reading) for e in self.entries]
        self.by_segment: dict[int, Volume] = {}
        for vol in self.volumes:
            for seg_abs in vol.base:
                self.by_segment.setdefault(seg_abs, vol)
        # The header is wherever the record says it is (bytes 6-7, normally
        # segment 0) and at its duplicate, whichever copy the identification
        # happened to read: a flux capture's segments land in any order, and
        # when the duplicate landed first the layout used to know only it,
        # so the real header segment came up "(unknown)" and stayed so.
        self.header_segs = {info.header_seg, info.vol.header_seg}
        if dup_header is not None:
            self.header_segs.add(dup_header)
        elif info.vol.dup_header_seg:
            self.header_segs.add(info.vol.dup_header_seg)
        self.vtbl_seg = info.vtbl_seg

    def summary(self) -> tuple[tuple[str, str], ...]:
        """The identification as detail rows: what the header and volume table said.

        Shared by both loaders' tape rows and the window's details panel, so
        a flux capture and an image of the same tape read alike. The
        geometry rows are the header's own numbers (QIC-80-MC Rev N 7.1);
        the cartridge is :mod:`qiclib.cartridge`'s guess from them; the
        volume profile, its score, its parser and its warnings are the
        reading's (:attr:`cinnabar.model.VolumeReading.rows`).
        """
        info, vol = self.info, self.info.vol
        guess = info.cartridge
        cartridge = "(unknown)"
        if guess.cartridge is not None:
            cartridge = guess.cartridge.description
            if not guess.exact:
                cartridge += f" ({guess.basis})"
        vtbl = "(none)" if info.vtbl_seg is None else f"segment {info.vtbl_seg}"
        rows = (
            ("tape name", vol.tape_name.strip() or "(none)"),
            ("format code", str(vol.format_code)),
            ("header revision", f"0x{vol.revision:02x}"),
            ("standard", info.standard or "(unknown)"),
            ("cartridge", cartridge),
            ("geometry", f"{vol.tracks} tracks x {vol.segments_per_track} segments"),
            ("max fsd / ftk / fsc", f"{vol.max_fsd} / {vol.max_ftk} / {vol.max_fsc}"),
            ("header segment", f"{info.header_seg} ({info.header_state})"),
            ("duplicate header", str(vol.dup_header_seg)),
            ("data segments", f"{vol.first_data_seg} - {vol.last_data_seg}"),
            ("volume table", f"{vtbl} ({info.vtbl_state})"),
            ("volumes", str(len(self.entries))),
            *self.reading.rows,
        )
        return rows + tuple(("note", note) for note in info.notes)

    @property
    def warnings(self) -> tuple[str, ...]:
        """The caveats on the table's reading, for the tape's rows (:attr:`cinnabar.model.Tape.warnings`)."""
        return self.reading.warnings

    def landed(self, seg_abs: int) -> None:
        """A segment just arrived (a flux capture): its volume's structure is stale."""
        vol = self.by_segment.get(seg_abs)
        if vol is not None:
            vol.landed()

    def invalidate(self) -> None:
        """Every volume's structure is parsed afresh on its next use."""
        for vol in self.volumes:
            vol.invalidate()

    def holds(self, seg_abs: int) -> str:
        """A short name for the segment's content, for labels: Header, Volume Table, a volume."""
        if seg_abs in self.header_segs:
            return "Header"
        if seg_abs == self.vtbl_seg:
            return "Volume Table"
        vol = self.by_segment.get(seg_abs)
        if vol is not None:
            return vol.entry.description.strip() or "Volume"
        return ""

    def fields(self, seg_abs: int, data: bytes, bmap: ByteMap) -> list[Span]:
        """The segment's logical spans, mapped through ``bmap`` onto the axis."""
        if not data:
            return []
        padded = data + bytes(max(SEGMENT_BYTES - len(data), 0))
        if seg_abs in self.header_segs:
            return [s.mapped(bmap.to_axis) for s in header_mod.header_spans(padded)]
        if seg_abs == self.vtbl_seg:
            spans = volume_ann.vtbl_spans(padded, self.entries, self.reading)
            return [s.mapped(bmap.to_axis) for s in spans]
        vol = self.by_segment.get(seg_abs)
        if vol is None:
            return []
        if vol.entry.compressed:
            spans = volume_ann.extent_spans(
                padded,
                spanning=vol.entry.segment_spanning,
                offset_bytes=vol.reading.extent_offset_bytes,
                frame_size_bytes=vol.reading.extent_frame_size_bytes,
            )
            return [s.mapped(bmap.to_axis) for s in spans]
        lo = vol.base[seg_abs]
        hi = lo + SEGMENT_BYTES
        out: list[Span] = []

        def place(x: float) -> float:
            return bmap.to_axis(x - lo)

        for s in vol.spans():
            clipped = s.clipped(lo, hi)
            if clipped is not None:
                out.append(clipped.mapped(place))
        return out


def volume_track(vol: Volume, number: int, label: str) -> Track:
    """A volume's decompressed byte stream as a byte-axis track.

    Each readable tape segment's piece is a segment of the track, titled
    by the tape segment it came from, so a hole (a lost tape segment) is
    a gap you can see. The QIC-113 rows come from the stream (directory
    and data entries: the files, laid out by the parser the volume profile
    names), a "Source segment" row says which tape segment each stretch
    came from, and the Bytes lane is the decompressed data, so a click on
    a data entry's ``data`` child is that file's bytes and "Export these
    bytes" writes the file. The track's rows carry the profile's verdict
    and the parser's probe of the whole stream, what ``qicsilver extract``
    would stamp into the TWVL header, and its warnings are the probe's.
    """
    body, pieces = vol.decompressed()
    size = max(len(body), 1)
    parser = vol.reading.parser
    try:
        structure = volume_ann.volume_spans(body, vol.entry, decompressed=True, parser=parser)
    except Exception:  # noqa: BLE001 -- a damaged volume still shows its pieces
        log.exception("volume %r: decompressed structure not parsed", vol.entry.description)
        structure = []
    probe, warnings = volume_ann.probe_rows(body, vol.entry, parser)
    segments: list[Segment] = []
    for k, piece in enumerate(pieces):
        start = piece.offset
        end = pieces[k + 1].offset if k + 1 < len(pieces) else size
        if end <= start:
            end = start + len(piece.data)
        bmap = ByteMap.identity(float(start), end - start)
        fields = [c for s in structure if (c := s.clipped(start, end)) is not None]
        source = Span(
            start,
            start + len(piece.data),
            f"segment {piece.seg_abs}",
            f"{len(piece.data):,} B",
            "extent",
            Shape.CHEVRON,
            (("tape segment", str(piece.seg_abs)), ("bytes", str(len(piece.data)))),
            row=SOURCE_ROW,
        )
        hole = end - (start + len(piece.data))
        state = "clean" if hole <= 0 else "partial"
        segments.append(
            Segment(
                k,
                float(start),
                float(end),
                f"Seg {piece.seg_abs}",
                [source],
                fields,
                [ByteRun(float(start), float(start + len(piece.data)), piece.data, start)],
                state,
                (
                    ("tape segment", str(piece.seg_abs)),
                    ("stream offset", f"{start:,}"),
                    ("bytes", f"{len(piece.data):,}"),
                    ("hole after", f"{hole:,} B" if hole > 0 else "none"),
                ),
            )
        )
        del bmap
    return Track(
        label,
        number,
        Axis(AxisKind.BYTES),
        "Volume",
        segments,
        [s.state_token() for s in segments],
        (
            ("volume", vol.entry.description.strip()),
            ("compressed", str(bool(vol.entry.compressed))),
            ("stream bytes", f"{size:,}"),
            ("pieces", str(len(pieces))),
            ("tape segments", f"{vol.entry.start_seg}-{vol.entry.end_seg}"),
            *vol.reading.rows,
            *probe,
        ),
        warnings=warnings,
    )

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
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable

from qiclib import identify
from qiclib.volume import VtblEntry

from cinnabar.annotate import header as header_mod
from cinnabar.annotate import volume as volume_ann
from cinnabar.model import SEGMENT_BYTES, ByteMap, Span

log = logging.getLogger(__name__)


class Volume:
    """One VTBL entry: its segments' stream offsets and, lazily, its spans."""

    def __init__(
        self,
        entry: VtblEntry,
        present: Iterable[int],
        bad: set[int],
        fetch: Callable[[int], bytes],
        offset_bytes: int,
    ) -> None:
        self.entry = entry
        self.offset_bytes = offset_bytes
        self._fetch = fetch
        self.base: dict[int, int] = {}  # absolute segment -> offset in the stream
        have = set(present)
        off = 0
        for seg_abs in range(entry.start_seg, entry.end_seg + 1):
            if seg_abs in bad:
                continue  # a BSM-bad segment contributes no bytes (TWS-2 section 5.2)
            if seg_abs in have:
                self.base[seg_abs] = off
            off += SEGMENT_BYTES  # a missing segment still occupies its length
        self.length = off
        self._spans: list[Span] | None = None

    def stream(self) -> bytes:
        out = bytearray(self.length)
        for seg_abs, off in self.base.items():
            data = self._fetch(seg_abs)
            out[off : off + min(len(data), SEGMENT_BYTES)] = data[:SEGMENT_BYTES]
        return bytes(out)

    def spans(self) -> list[Span]:
        """The volume's structure in stream offsets, parsed on first use."""
        if self._spans is None:
            if self.entry.compressed:
                self._spans = []
            else:
                log.debug("parsing volume %r (%d bytes)", self.entry.description, self.length)
                try:
                    self._spans = volume_ann.volume_spans(self.stream(), self.entry)
                except Exception:  # noqa: BLE001 -- a damaged volume still shows its segments
                    log.exception("volume %r: structure not parsed", self.entry.description)
                    self._spans = []
        return self._spans


class Layout:
    """What each segment of a tape holds, from its identification."""

    def __init__(
        self,
        info: identify.TapeInfo,
        present: Iterable[int],
        fetch: Callable[[int], bytes],
        *,
        dup_header: int | None = None,
    ) -> None:
        self.info = info
        verdict = max(info.verdicts, key=lambda v: v.score) if info.verdicts else None
        self.entries: list[VtblEntry] = verdict.entries if verdict is not None else []
        self.profile = verdict.profile.name if verdict is not None else "(none)"
        offset_bytes = verdict.profile.extent_offset_bytes if verdict is not None else 8
        bad = set(info.bsm.bad_segments)
        present = list(present)
        self.volumes = [Volume(e, present, bad, fetch, offset_bytes) for e in self.entries]
        self.by_segment: dict[int, Volume] = {}
        for vol in self.volumes:
            for seg_abs in vol.base:
                self.by_segment.setdefault(seg_abs, vol)
        self.header_segs = {info.header_seg}
        if dup_header is not None:
            self.header_segs.add(dup_header)
        elif info.vol.dup_header_seg:
            self.header_segs.add(info.vol.dup_header_seg)
        self.vtbl_seg = info.vtbl_seg

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
            return [s.mapped(bmap.to_axis) for s in volume_ann.vtbl_spans(padded, self.entries)]
        vol = self.by_segment.get(seg_abs)
        if vol is None:
            return []
        if vol.entry.compressed:
            spans = volume_ann.extent_spans(
                padded, spanning=vol.entry.segment_spanning, offset_bytes=vol.offset_bytes
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

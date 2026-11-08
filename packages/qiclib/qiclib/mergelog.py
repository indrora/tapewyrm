"""How ``tw convert`` put each track together, told a track at a time.

A tape image is built from reads, and with ``tw dump --keep-all`` or
``--passes`` there can be eight of one track. What matters to someone
recovering a tape is not what each file held but what each *track* ended
up as, and where its sectors came from. :class:`MergeLog` is fed every
segment in order (:func:`qiclib.build.build_image`) and logs, at INFO:

- a line for every segment that took more than plain reading: sectors
  from more than one read, a voted sector, an RS-column rescue (or a
  failed attempt at one), an uncorrectable result, or spare parity that
  disagrees (a WARNING: that is a CRC-good sector that is wrong, the one
  kind of damage nothing else reports);
- except where the parity disagrees because there is no parity: a segment
  whose every CRC-good data sector is one byte repeated (:func:`format_fill`)
  is space the backup never wrote since the tape was formatted, and the
  format did not compute parity for its fill. On unk-sony-backup2 (QIC-80,
  format code 2) that is 0x6B, from mid track 18 to the end of the tape:
  1,300-odd segments, every one "inconsistent", none of them damage. They
  are counted in the track summary as "unwritten", with no line each;
- when a track ends, its summary: how many reads it had, what its
  segments came out as, and for each read how many sectors it held, how
  many were CRC-clean and how many the image used.

Segments are numbered track by track (DESIGN.md §2.3), so the segments of
one track arrive together and its summary can follow its last one.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass

from tapewyrm_archive.twti import SegmentState

from qiclib.geometry import Geometry
from qiclib.rs import REDUNDANCY, Parity
from qiclib.types import RawSector, Segment
from qiclib.vote import VoteStats, key_of

log = logging.getLogger(__name__)

# How a segment kind reads in a track summary, where it is not its own name.
_STATE_WORDS = {"bad": "bad (map)"}

# The two sources of a sector that are not a read.
VOTE = "vote"
RS_COLUMNS = "RS columns"


def format_fill(seg: Segment) -> int | None:
    """The byte every CRC-good data sector of ``seg`` is made of, or None.

    Data rows only (the participating slots but the last 3): the parity rows
    of an unwritten segment need not hold the fill. Run it before the
    segment is corrected, which writes solved rows (garbage, for a segment
    that was never a codeword) into its erased slots. The fill byte is not
    assumed (one bench tape's is 0x6B; the standards do not fix it), only
    that it is one byte, the same in every sector.
    """
    participating = [i for i in range(Segment.SECTORS) if i not in seg.excluded]
    rows = [
        sec.data
        for slot in participating[: len(participating) - REDUNDANCY]
        if (sec := seg.sectors[slot]) is not None and sec.data_crc_ok and not sec.deleted
    ]
    if not rows or not rows[0]:
        return None
    first = rows[0][:1]
    return first[0] if all(row == first * len(row) for row in rows) else None


@dataclass
class ReadInfo:
    """One read (capture) as the log names it: file, track, and what it held."""

    name: str  # the capture's file name, e.g. track-05.3.twrf
    track: int  # the track its TWRF header says it read; -1 if unknown
    sectors: int  # sectors decoded
    clean: int  # of them, data CRC good


class MergeLog:
    """The per-track and per-segment account of one image build (module docstring).

    ``origin`` maps a coordinate to the index of the read the union kept it
    from (:func:`qiclib.merge.union`), ``voted_index`` is the index the
    voted sectors were handed to the union under (None if there were none),
    and ``votes`` is what voting and rescue did.
    """

    def __init__(
        self,
        reads: list[ReadInfo],
        geometry: Geometry,
        *,
        origin: dict[tuple[int, int, int], int],
        votes: VoteStats,
        voted_index: int | None,
    ) -> None:
        self.reads = reads
        self.geometry = geometry
        self.origin = origin
        self.votes = votes
        self.voted_index = voted_index
        self.totals: Counter[str] = Counter()
        self._track: int | None = None
        self._states: Counter[str] = Counter()
        self._parity: Counter[Parity] = Counter()
        self._used: Counter[int | str] = Counter()
        self._segments = 0
        self._unwritten = 0

    # ----------------------------------------------------------------------

    def _source(self, sec: RawSector) -> int | str:
        """Where a placed sector came from: a read's index, VOTE or RS_COLUMNS."""
        key = key_of(sec)
        if key in self.votes.rebuilt:
            return RS_COLUMNS
        index = self.origin.get(key)
        if index is None or index == self.voted_index:
            # Every placed sector went through the union, so a missing
            # origin means the vote's pass (which the union may not have
            # been told about) or a sector made after it: the vote is the
            # only thing that makes sectors before correction.
            return VOTE
        return index

    def _label(self, source: int | str) -> str:
        return source if isinstance(source, str) else self.reads[source].name

    def segment(
        self,
        n: int,
        seg: Segment | None,
        state: SegmentState,
        *,
        parity: Parity | None = None,
        erasures: int = 0,
        fill: int | None = None,
    ) -> None:
        """Account for segment ``n`` (``seg`` None for one never read or mapped bad).

        ``fill`` is :func:`format_fill` of the segment before correction, for
        one whose parity is INCONSISTENT: then the segment is unwritten, not
        damaged.
        """
        tpt, tps = self.geometry.seg_to_tpt_tps(n)
        if tpt != self._track:
            self._flush()
            self._track = tpt
        self._segments += 1
        rescued = n in self.votes.rescued
        kind = "rescued" if rescued else state.name.lower()
        self._states[kind] += 1
        self.totals[kind] += 1
        unwritten = parity is Parity.INCONSISTENT and fill is not None
        if unwritten:
            self._unwritten += 1
            self.totals["unwritten"] += 1
        elif parity is not None:
            self._parity[parity] += 1
            if parity is Parity.INCONSISTENT:
                self.totals["inconsistent"] += 1
        if seg is None:
            return

        sources: Counter[int | str] = Counter(
            self._source(sec) for sec in seg.sectors if sec is not None
        )
        self._used.update(sources)
        from_reads = [s for s in sources if isinstance(s, int)]
        tried = self.votes.unrescued.get(n)
        notable = (
            len(from_reads) > 1
            or VOTE in sources
            or rescued
            or tried is not None
            or state is SegmentState.UNCORRECTABLE
            or (parity is Parity.INCONSISTENT and not unwritten)
        )
        if not notable:
            return
        what = state.name
        if rescued:
            what = f"rescued by RS columns ({self.votes.rescued[n]} sectors rebuilt), {what}"
        parts = [what]
        if erasures:
            parts.append(f"{erasures} erasures")
        if unwritten:
            parts.append(f"unwritten (format fill 0x{fill:02X}, no parity)")
        elif parity is Parity.INCONSISTENT:
            parts.append("spare parity DISAGREES: a CRC-good sector is wrong")
        elif parity is not None and state is not SegmentState.UNCORRECTABLE:
            parts.append(f"parity {parity.value}")
        if tried is not None:
            ok, width = tried
            parts.append(
                f"RS columns: {ok:,} of {width:,} checked"
                if ok
                else "RS columns: too few reads of its bad sectors to compare"
            )
        parts.append(
            "from " + ", ".join(f"{self._label(s)} {count}" for s, count in sources.most_common())
        )
        level = logging.WARNING if parity is Parity.INCONSISTENT and not unwritten else logging.INFO
        log.log(level, "track %2d segment %3d (#%d): %s", tpt, tps, n, "; ".join(parts))

    def finish(self) -> None:
        """Log the last track's summary."""
        self._flush()
        self._track = None

    # ----------------------------------------------------------------------

    def _flush(self) -> None:
        if self._track is None:
            return
        tpt = self._track
        here = [i for i, r in enumerate(self.reads) if r.track == tpt]
        # A read of another track that still supplied sectors here (sectors
        # self-locate) is listed too, after this track's own.
        others = sorted(s for s in self._used if isinstance(s, int) and s not in here)
        states = ", ".join(
            f"{self._states[k]:,} {_STATE_WORDS.get(k, k)}"
            for k in ("clean", "corrected", "rescued", "uncorrectable", "missing", "bad")
            if self._states[k] or k == "clean"
        )
        if not here and not self._used:
            log.info("track %2d: not read (%s segments missing)", tpt, f"{self._segments:,}")
        else:
            plural = "s" if len(here) != 1 else ""
            line = f"{len(here)} read{plural}; {self._segments:,} segments: {states}"
            odd = [
                f"{self._parity[p]:,} {p.value}"
                for p in (Parity.INCONSISTENT, Parity.UNVERIFIED)
                if self._parity[p]
            ]
            if self._unwritten:
                odd.append(f"{self._unwritten:,} unwritten (format fill)")
            if odd:
                line += f"; parity: {', '.join(odd)}"
            if self._used[VOTE]:
                line += f"; {self._used[VOTE]:,} sectors voted"
            if len(here) == 1 and not others:
                r = self.reads[here[0]]
                line += f"; from {r.name} ({r.sectors:,} sectors, {r.clean:,} CRC-clean)"
            log.info("track %2d: %s", tpt, line)
            if len(here) > 1 or others:
                for i in [*here, *others]:
                    r = self.reads[i]
                    log.info(
                        "track %2d:   %s: %s sectors read, %s CRC-clean, %s used",
                        tpt,
                        r.name,
                        f"{r.sectors:,}",
                        f"{r.clean:,}",
                        f"{self._used[i]:,}",
                    )
            if self._used[RS_COLUMNS]:
                log.info(
                    "track %2d:   RS columns: %s sectors rebuilt",
                    tpt,
                    f"{self._used[RS_COLUMNS]:,}",
                )
        self._states.clear()
        self._parity.clear()
        self._used.clear()
        self._segments = 0
        self._unwritten = 0

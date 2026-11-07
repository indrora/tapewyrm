"""What cinnabar draws: tracks of segments, each a stack of lanes of spans.

This is the whole contract between the readers (``cinnabar.load``), the
decoders that label things (``cinnabar.annotate``) and the Qt widgets
(``cinnabar.ui``). The widgets know nothing about QIC; the loaders and
annotators know nothing about Qt. Everything meets here, in plain data.

The picture, top to bottom, is the one in the design sketch:

    Flux transitions   raw transitions, PLL bitcells, decoded bits  (TWRF only)
    <format> Records   one span per physical record: an MFM sector on a
                       QIC-80 tape, a QIC-100-MC record on a QIC-100 tape
    <format> Fields    the pieces of each record: preamble, marks, ID, data,
                       CRC, postamble (the records' children)
    Fields             what the bytes mean: the header's FPR and bad-sector
                       map, volume-table entries, directory and data entries,
                       compression extents and frames (nested as deep as the
                       structure goes; one lane per level)
    Bytes              the segment's bytes as a heat strip

and underneath, the whole track as a strip with the viewed window on it.

Two axes. A raw flux capture (TWRF, TWS-1) is measured in **seconds** from
the start of the pass, because that is all a flux stream knows; a tape
image (TWTI, TWS-2) has lost time and is measured in **bytes** of the data
area, 29,696 per segment slot. Both are broken into segments, and the
segment is what you scrub by. Spans carry their position in axis units
already, so a widget never converts; the mapping from "byte 1,536 of this
segment's data" to an axis position is :class:`ByteMap`'s job and is done
while the segment is built.

Why spans are a tree: a QIC-100 record *contains* its ID field, which
contains a frame number; a volume-table entry *contains* its flags byte. A
``Span`` has children, and each lane of the display is one depth of that
tree. The records lane is depth 0 of ``Segment.records``, the "<format>
Fields" lane is depth 1; the "Fields" lanes are depths 0, 1, 2... of
``Segment.fields``. New decoders add spans, never widgets.
"""

from __future__ import annotations

import bisect
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import numpy as np

# Segment data slot, TWS-2 section 6: 29 data sectors of 1,024 bytes.
SEGMENT_BYTES = 29_696
SECTOR_BYTES = 1_024


# The zoom maximum, in bytes of view width (see Axis.min_window). Raise it
# to zoom closer; the span items draw ticks and the Bytes lane prints hex
# at any width, so nothing else needs to change.
ZOOM_MAX_BYTES = 8


class AxisKind(Enum):
    """What the x axis measures: time (a flux capture) or bytes (an image)."""

    TIME = "time"  # seconds from the start of the pass
    BYTES = "bytes"  # bytes from the start of the track's data area


@dataclass(frozen=True)
class Axis:
    kind: AxisKind

    @property
    def min_window(self) -> float:
        """The narrowest view the lanes allow: :data:`ZOOM_MAX_BYTES` bytes, or their time.

        THIS IS THE ZOOM MAXIMUM. The view boxes clamp their x range here
        (``ViewBox.setLimits(minXRange=...)`` in ``cinnabar.ui.lanes``),
        so the wheel, the zoom actions and the ruler all stop at it. On the
        time axis a byte is taken as QIC-80's 16 us (8 MFM data bits at 1 us
        a cell); 8 bytes is 128 us, which shows single bitcells at about
        ten pixels each on a wide window. float64 keeps 1e-14 s of
        precision at the far end of a long track, so the floor is about
        what is useful to see, not about arithmetic.
        """
        return float(ZOOM_MAX_BYTES) if self.kind is AxisKind.BYTES else ZOOM_MAX_BYTES * 16e-6

    def format(self, value: float) -> str:
        """A boundary label: ``340ms`` on the time axis, ``89,088 B`` on the byte axis."""
        if self.kind is AxisKind.TIME:
            if abs(value) < 1.0:
                return f"{value * 1000:.0f}ms"
            return f"{value:.3f}s"
        return f"{int(value):,} B"

    def format_tick(self, value: float, step: float) -> str:
        """A ruler label at a tick spacing of ``step``: enough decimals to tell neighbours apart."""
        if self.kind is AxisKind.BYTES:
            return f"{int(round(value)):,} B"
        import math

        decimals = max(3, min(9, int(math.ceil(-math.log10(step))) if step > 0 else 3))
        return f"{value:.{decimals}f}s"

    def format_delta(self, value: float) -> str:
        """A width label: the same units, with sub-millisecond precision when it matters."""
        if self.kind is AxisKind.TIME:
            if abs(value) < 0.001:
                return f"{value * 1_000_000:.0f}µs"
            if abs(value) < 1.0:
                return f"{value * 1000:.2f}ms"
            return f"{value:.3f}s"
        return f"{int(value):,} B"


class Shape(Enum):
    """How a span is drawn. The sketch uses all three."""

    BLOCK = "block"  # plain rectangle
    CHEVRON = "chevron"  # both ends pointed: a record, a long field
    HEX = "hex"  # a small pointed tag: a mark, a signature, a CRC


@dataclass(frozen=True)
class Span:
    """One labelled stretch of the axis, with the spans nested inside it.

    ``style`` is a token the UI maps to a colour (``cinnabar.ui.palette``);
    the model does not know colours. ``detail`` rows are shown on hover and
    hold everything that did not fit in the label: a CRC's stored and
    computed values, a VTBL entry's decoded date, a frame's raw bit.
    """

    start: float
    end: float
    label: str = ""
    sublabel: str = ""
    style: str = "neutral"
    shape: Shape = Shape.BLOCK
    detail: tuple[tuple[str, str], ...] = ()
    children: tuple[Span, ...] = ()
    # The display row this span belongs to, "<decoder>: <what>" ("MFM:
    # Sectors", "QIC-113: Volumes"), set by the annotator (assign_rows).
    # Rows, not depths, are what the lanes show, so two decoders can share a
    # depth without sharing a lane.
    row: str = ""

    @property
    def width(self) -> float:
        return self.end - self.start

    @property
    def short(self) -> str:
        """The label's first word: what a span shows when the whole label will not fit."""
        return self.label.split()[0] if self.label else ""

    def deepest_at(self, x: float) -> Span:
        """The innermost span of this subtree covering ``x`` (self if no child does)."""
        for child in self.children:
            if child.start <= x < child.end:
                return child.deepest_at(x)
        return self

    def mapped(self, fn: Callable[[float], float]) -> Span:
        """This span and its whole subtree with ``fn`` applied to every start and end."""
        return Span(
            fn(self.start),
            fn(self.end),
            self.label,
            self.sublabel,
            self.style,
            self.shape,
            self.detail,
            tuple(child.mapped(fn) for child in self.children),
            self.row,
        )

    def clipped(self, lo: float, hi: float) -> Span | None:
        """The part of this subtree inside ``[lo, hi)``, or None if none of it is.

        A span cut at the window edge keeps its label; a directory entry that
        straddles two segments shows in both, cut, which is what the tape did
        to it (QIC-113 section 7.2: "no sector or segment boundaries shall be
        observed").
        """
        if self.end <= lo or self.start >= hi:
            return None
        kids = tuple(c for c in (k.clipped(lo, hi) for k in self.children) if c is not None)
        return Span(
            max(self.start, lo),
            min(self.end, hi),
            self.label,
            self.sublabel,
            self.style,
            self.shape,
            self.detail,
            kids,
            self.row,
        )


def assign_rows(span: Span, rows: Sequence[str]) -> Span:
    """``span`` and its subtree with ``rows[depth]`` as each level's row (the last row repeats)."""
    row = rows[0] if rows else span.row
    rest = rows[1:] if len(rows) > 1 else rows
    return Span(
        span.start,
        span.end,
        span.label,
        span.sublabel,
        span.style,
        span.shape,
        span.detail,
        tuple(assign_rows(c, rest) for c in span.children),
        row,
    )


def rows_of(spans: Sequence[Span]) -> set[str]:
    """Every row name used anywhere in a forest of spans."""
    out: set[str] = set()
    for s in spans:
        out.add(s.row)
        out |= rows_of(s.children)
    return out


def in_row(spans: Sequence[Span], row: str) -> Iterator[Span]:
    """Every span of a forest whose row is ``row``, in order."""
    for s in spans:
        if s.row == row:
            yield s
        yield from in_row(s.children, row)


def depth(spans: Sequence[Span]) -> int:
    """Levels in a forest of spans: 0 for none, 1 for flat, 2 with one level of children..."""
    return max((1 + depth(s.children) for s in spans), default=0)


def level(spans: Sequence[Span], n: int) -> Iterator[Span]:
    """Every span at depth ``n`` (0 = the given spans themselves), in order."""
    if n == 0:
        yield from spans
        return
    for s in spans:
        yield from level(s.children, n - 1)


# ---------------------------------------------------------------------------
# Bytes and where they sit on the axis
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ByteRun:
    """``data`` spread evenly over ``[start, end)`` of the axis.

    On the byte axis a segment is one run. On the time axis each recovered
    sector is a run over its data field, so the Bytes lane has holes where
    sectors were not read and the field decoders' spans land on real time.
    """

    start: float
    end: float
    data: bytes
    offset: int = 0  # position of data[0] in the segment's data (slot * 1024 for a sector)

    def axis_of(self, k: int) -> float:
        """Axis position of byte ``k`` of this run."""
        return self.start + (self.end - self.start) * k / max(len(self.data), 1)

    def index_at(self, x: float) -> int:
        """Index of the byte under axis ``x`` (clamped)."""
        k = int((x - self.start) / (self.end - self.start) * len(self.data))
        return min(max(k, 0), len(self.data) - 1)


@dataclass(frozen=True)
class BytePiece:
    """A stretch of one run's bytes, as :meth:`Segment.bytes_between` returns them."""

    offset: int  # position in the segment's data
    data: bytes
    run: ByteRun
    first: int  # index of data[0] in the run


class ByteMap:
    """Segment data offset -> axis position, piecewise linear over the runs.

    Built from the sector (or slot) positions of one segment. An offset in a
    gap between runs (a sector that was never read) is placed by linear
    interpolation between the neighbouring runs, so a structure that spans a
    missing sector still has a width; before the first run or after the last
    it is extrapolated at the nearest run's rate.
    """

    def __init__(self, runs: Sequence[tuple[int, int, float, float]]) -> None:
        # (byte offset, byte length, axis start, axis end), sorted by offset.
        self._runs = sorted(runs)
        self._offsets = [r[0] for r in self._runs]

    @classmethod
    def identity(cls, base: float, length: int) -> ByteMap:
        """The byte axis: offset ``o`` of the segment is axis ``base + o``."""
        return cls([(0, length, base, base + length)])

    def to_axis(self, offset: float) -> float:
        if not self._runs:
            return float(offset)
        i = bisect.bisect_right(self._offsets, offset) - 1
        if i < 0:
            off, n, a0, a1 = self._runs[0]
            return a0 - (off - offset) * (a1 - a0) / max(n, 1)
        off, n, a0, a1 = self._runs[i]
        if offset < off + n or i == len(self._runs) - 1:
            return a0 + (offset - off) * (a1 - a0) / max(n, 1)
        # In the gap after run i: interpolate to the start of run i+1.
        off2, _n2, b0, _b1 = self._runs[i + 1]
        gap = off2 - (off + n)
        return a1 + (offset - (off + n)) * (b0 - a1) / max(gap, 1)


# ---------------------------------------------------------------------------
# Flux
# ---------------------------------------------------------------------------


class Flux:
    """The flux lane for a whole track, in seconds: transitions, and cells per decoded chunk.

    Transitions are kept as the capture's own int32 intervals (a 40 MB
    capture is 39 million of them; cumulative times as float64 would be
    300 MB) with a coarse cumulative index every ``BLOCK`` intervals, so
    the transitions in a window cost one block's cumsum
    (:meth:`transitions_between`).

    Bitcells arrive chunk by chunk as segments decode
    (:meth:`add_chunk`), each chunk carrying its PLL trace
    (:class:`tapewyrm.codec.gwpll.PllTrace`: per bit-producing
    transition, its time, the cumulative cell count after it and the
    clock) and its cells; cell ``k`` clocked out by transition ``i`` started
    ``(bits[i] - k)`` clocks before it. A 150-second track is 150 million
    cells, so their times are computed for the window being drawn only.

    The MFM picture a reader of QIC-80-MC section 5 expects is the rows side
    by side: transitions on top, clock cells under them, decoded bits
    under those, so a PLL slip shows as the rows drifting apart.
    """

    BLOCK = 1 << 16

    def __init__(self, intervals: np.ndarray, sample_hz: int) -> None:
        self.intervals = intervals  # int32 ticks between transitions
        self.sample_hz = sample_hz
        # cumulative ticks at the END of each block, so block b covers
        # intervals [b*BLOCK, (b+1)*BLOCK) and ends at _block_end[b].
        n_blocks = (len(intervals) + self.BLOCK - 1) // self.BLOCK
        ends = (
            np.add.reduceat(intervals.astype(np.int64), np.arange(0, len(intervals), self.BLOCK))
            if len(intervals)
            else np.zeros(0, dtype=np.int64)
        )
        self._block_end = np.cumsum(ends) if n_blocks else np.zeros(0, dtype=np.int64)
        self._chunks: list[tuple[float, float, np.ndarray, np.ndarray, np.ndarray, np.ndarray]] = []

    @property
    def duration(self) -> float:
        return float(self._block_end[-1]) / self.sample_hz if len(self._block_end) else 0.0

    def interval_index(self, t: float) -> int:
        """Index of the first interval ending at or after ``t`` seconds."""
        ticks = int(t * self.sample_hz)
        b = int(np.searchsorted(self._block_end, ticks))
        if b >= len(self._block_end):
            return len(self.intervals)
        base = int(self._block_end[b - 1]) if b > 0 else 0
        lo = b * self.BLOCK
        block = np.cumsum(self.intervals[lo : lo + self.BLOCK].astype(np.int64)) + base
        return lo + int(np.searchsorted(block, ticks))

    def ticks_before(self, index: int) -> int:
        """Sum of the intervals before ``index``."""
        b = index // self.BLOCK
        base = int(self._block_end[b - 1]) if b > 0 else 0
        return base + int(self.intervals[b * self.BLOCK : index].astype(np.int64).sum())

    def transitions_between(self, t0: float, t1: float) -> np.ndarray:
        """Transition times in ``[t0, t1]``, seconds."""
        i0 = self.interval_index(t0)
        i1 = self.interval_index(t1)
        if i1 <= i0:
            return np.zeros(0)
        base = self.ticks_before(i0)
        return (np.cumsum(self.intervals[i0:i1].astype(np.int64)) + base) / self.sample_hz

    def transition_count(self, t0: float, t1: float) -> int:
        return max(self.interval_index(t1) - self.interval_index(t0), 0)

    def intervals_between(self, t0: float, t1: float) -> tuple[np.ndarray, np.ndarray]:
        """(transition times, the interval that ended at each) in ``[t0, t1]``, seconds.

        The stream view's data: each transition as a point at (when, how
        long since the previous one).
        """
        i0 = self.interval_index(t0)
        i1 = self.interval_index(t1)
        if i1 <= i0:
            return np.zeros(0), np.zeros(0)
        ticks = self.intervals[i0:i1].astype(np.int64)
        base = self.ticks_before(i0)
        return (np.cumsum(ticks) + base) / self.sample_hz, ticks / self.sample_hz

    def interval_ceiling(self, sample: int = 1_000_000) -> float:
        """A y ceiling for the stream view: just above the 99.5th percentile of a sample, seconds."""
        if not len(self.intervals):
            return 1e-5
        head = self.intervals[:sample].astype(np.float64)
        return float(np.percentile(head, 99.5)) / self.sample_hz * 1.15

    def add_chunk(
        self,
        t0: float,
        t1: float,
        trace_times: np.ndarray,
        trace_bits: np.ndarray,
        trace_clock: np.ndarray,
        bits: np.ndarray,
    ) -> None:
        self._chunks = sorted(
            [c for c in self._chunks if c[0] != t0]
            + [(t0, t1, trace_times, trace_bits, trace_clock, bits)]
        )

    def _overlapping(self, t0: float, t1: float):
        return [c for c in self._chunks if c[1] > t0 and c[0] < t1]

    def cell_count_between(self, t0: float, t1: float) -> int:
        total = 0
        for _c0, _c1, times, cum, _clk, _bits in self._overlapping(t0, t1):
            i0 = int(np.searchsorted(times, t0))
            i1 = int(np.searchsorted(times, t1))
            k0 = int(cum[i0 - 1]) if i0 > 0 else 0
            k1 = int(cum[min(i1, len(cum) - 1)]) if len(cum) else 0
            total += max(k1 - k0, 0)
        return total

    def cells_between(self, t0: float, t1: float) -> tuple[np.ndarray, np.ndarray]:
        """(start times, values) of the cells whose transitions fall in ``[t0, t1]``."""
        times_out: list[np.ndarray] = []
        bits_out: list[np.ndarray] = []
        for _c0, _c1, times, cum, clk, bits in self._overlapping(t0, t1):
            if not len(cum):
                continue
            i0 = int(np.searchsorted(times, t0))
            i1 = int(np.searchsorted(times, t1))
            k0 = int(cum[i0 - 1]) if i0 > 0 else 0
            k1 = int(cum[min(i1, len(cum) - 1)])
            ks = np.arange(k0, min(k1, len(bits)), dtype=np.int64)
            i = np.minimum(np.searchsorted(cum, ks, side="right"), len(cum) - 1)
            times_out.append(times[i] - clk[i] * (cum[i] - ks))
            bits_out.append(bits[k0 : k0 + len(ks)])
        if not times_out:
            return np.zeros(0), np.zeros(0, dtype=np.uint8)
        return np.concatenate(times_out), np.concatenate(bits_out)


# ---------------------------------------------------------------------------
# Segments and tracks
# ---------------------------------------------------------------------------


@dataclass
class Segment:
    """One segment's worth of lanes, with every position in axis units."""

    index: int  # segment number within the track, 0-based
    start: float
    end: float
    title: str  # "Segment 3"
    records: list[Span] = field(default_factory=list)  # physical records (children = fields)
    fields: list[Span] = field(default_factory=list)  # logical structure (nested)
    byte_runs: list[ByteRun] = field(default_factory=list)
    state: str = ""  # TWTI table state ("CLEAN"...), or a flux segment's verdict
    detail: tuple[tuple[str, str], ...] = ()  # rows for the segment header popup

    @property
    def width(self) -> float:
        return self.end - self.start

    def bytes_between(self, x0: float, x1: float) -> list[BytePiece]:
        """The bytes whose positions fall in ``[x0, x1)``, run by run, in order."""
        out: list[BytePiece] = []
        for run in self.byte_runs:
            if not run.data or run.end <= x0 or run.start >= x1:
                continue
            k0 = run.index_at(x0) if x0 > run.start else 0
            k1 = run.index_at(x1) if x1 < run.end else len(run.data)
            if x1 < run.end and run.axis_of(k1) < x1:
                k1 += 1
            k1 = min(max(k1, k0 + 1), len(run.data))
            out.append(BytePiece(run.offset + k0, run.data[k0:k1], run, k0))
        return out

    def state_token(self) -> str:
        """The overview strip colour for this segment's state."""
        return {
            "clean": "clean",
            "partial": "corrected",
            "empty": "missing",
            "error": "uncorrectable",
            "decoding": "pending",
            "lead-in": "missing",
            "tail": "missing",
        }.get(self.state.lower(), self.state.lower())


@dataclass
class Track:
    label: str  # "Track 9"
    number: int
    axis: Axis
    physical: str  # "QIC-80", "QIC-100-MC": names the records lanes
    segments: list[Segment]
    # Per-segment overview colour tokens for the bottom strip (same style
    # vocabulary as spans: "clean", "corrected", "missing", ...).
    overview: list[str] = field(default_factory=list)
    detail: tuple[tuple[str, str], ...] = ()
    flux: Flux | None = None  # a capture's flux lane; None for an image
    # A capture's background decoder (cinnabar.load.flux.FluxTrack): segments
    # land one by one. None once everything is here, and always for an image.
    decoder: Any = None
    # Caveats on how the track was read, by their fixed names: a volume
    # track's parser probe raises them (``low-confidence``,
    # ``directory-damaged``...). The details panel shows them in warning
    # colour; see VolumeReading for where the names come from.
    warnings: tuple[str, ...] = ()

    @property
    def start(self) -> float:
        return self.segments[0].start if self.segments else 0.0

    @property
    def end(self) -> float:
        return self.segments[-1].end if self.segments else 1.0

    def segment_at(self, x: float) -> Segment | None:
        for s in self.segments:
            if s.start <= x < s.end:
                return s
        return None

    def heat(self, columns: int) -> np.ndarray:
        """Mean byte value per column across the whole track, for the overview strip.

        NaN where no bytes were read, so the strip shows holes as holes.
        """
        out = np.full(columns, np.nan)
        span = self.end - self.start
        if span <= 0:
            return out
        sums = np.zeros(columns)
        counts = np.zeros(columns)
        for seg in self.segments:
            for run in seg.byte_runs:
                if not run.data:
                    continue
                buf = np.frombuffer(run.data, dtype=np.uint8)
                c0 = int((run.start - self.start) / span * columns)
                c1 = max(c0 + 1, int((run.end - self.start) / span * columns))
                c0, c1 = min(c0, columns - 1), min(c1, columns)
                # Resample the run's bytes onto its columns.
                edges = np.linspace(0, len(buf), c1 - c0 + 1).astype(int)
                for k in range(c1 - c0):
                    piece = buf[edges[k] : max(edges[k + 1], edges[k] + 1)]
                    sums[c0 + k] += piece.mean()
                    counts[c0 + k] += 1
        mask = counts > 0
        out[mask] = sums[mask] / counts[mask]
        return out


@dataclass
class Tape:
    """A whole tape as the viewer sees it: which tracks exist and how to load each.

    Tracks are loaded on demand (``load(number, progress)``) because a flux
    track means reading and tokenizing tens of MB; the UI calls it off the
    main thread and shows the progress it reports.
    """

    source: str
    kind: str  # "TWRF" | "TWTI"
    track_numbers: list[int]
    load: Callable[[int, Any], Track]  # (track number, Progress) -> Track
    detail: tuple[tuple[str, str], ...] = ()
    # Display names for numbers that are not plain tape tracks: a volume's
    # decompressed stream is a "track" too (VOLUME_BASE + its table index),
    # named after the volume. A loader may add numbers after opening (a flux
    # tape's volumes appear once its header and volume table have decoded).
    labels: dict[int, str] = field(default_factory=dict)
    # Caveats on how the tape's volume table was read (VolumeReading.warnings),
    # shown in warning colour wherever the tape's rows are.
    warnings: tuple[str, ...] = ()

    def label(self, number: int) -> str:
        if number in self.labels:
            return self.labels[number]
        if number >= PASS_BASE:
            return f"Pass {number - PASS_BASE}"
        return f"Track {number}"


PASS_BASE = 1000  # a capture whose header names no usable track
VOLUME_BASE = 2000  # VOLUME_BASE + k: the decompressed stream of volume k


# ---------------------------------------------------------------------------
# How the volume table was read
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VolumeReading:
    """Which volume profile read the volume table, how sure it is, and what parses the volumes.

    A volume profile (``qiclib.volume.profile``, DESIGN.md 7.8) answers three
    questions about a tape, one per layer of a read: how the VTBL bytes past
    56 are laid out, how wide the QIC-122 extent fields are, and which
    file-set parser lists the volume's bytes. The loaders fill this record
    from the verdict the profile guess chose, the same one ``qicsilver
    extract`` reads the tape with, and the annotators and widgets read it
    as plain names and numbers, so the volume lanes are laid out by the
    parser the profile names and the details panel can show a
    ``low-confidence`` or ``no-parser`` read for what it is. The UI never
    sees a qiclib object; what travels here is what the TWVL header carries
    (TWS-3 sections 3.4, 3.5).

    ``profile`` and ``status`` name the profile and how exercised it is
    (``spec``, ``bench``, ``untested``); ``score`` is its plausibility
    total and ``eliminated`` says its fingerprint gate failed (every
    profile's did, and this one scored best anyway: ``qiclib.identify``
    then reads the tape with it and says it may be wrong); ``warnings``
    are the caveats by their fixed names (``qiclib.volume.verdict.VolumeWarning``
    values); ``parser`` is the file-set parser (``qic113-basic``,
    ``qic113-extended``, or ``none`` for a format that is recognised but
    cannot be listed; empty when no table was read); ``fields`` are what
    the profile placed in the VTBL record past byte 56, ``(name, offset,
    length)`` with its ``[vtbl]`` field names and ``"match"`` for the
    bytes its fingerprint compared, which is what the volume fields lane
    draws there; the two extent widths are the ``[extent]`` section.
    """

    profile: str
    parser: str
    status: str = ""
    score: int = 0
    eliminated: bool = False
    warnings: tuple[str, ...] = ()
    fields: tuple[tuple[str, int, int], ...] = ()
    extent_offset_bytes: int = 8
    extent_frame_size_bytes: int = 2

    @property
    def fingerprint(self) -> str:
        """``held`` or ``failed``: did the record carry what the profile's software writes?"""
        return "failed" if self.eliminated else "held"

    @property
    def rows(self) -> tuple[tuple[str, str], ...]:
        """Detail rows for a hover popup or the details panel: profile, score, parser, warnings."""
        profile = f"{self.profile} ({self.status})" if self.status else self.profile
        return (
            ("volume profile", profile),
            ("profile score", f"{self.score} (fingerprint {self.fingerprint})"),
            ("file-set parser", self.parser or "(none)"),
            ("warnings", ", ".join(self.warnings) or "none"),
        )

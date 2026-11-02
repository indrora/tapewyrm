"""One segment's decode, as a worker process runs it.

A :class:`SegmentJob` names a slice of the track's intervals (sitting in
shared memory) and the segment's time window inside it; the slice starts
``warmup`` intervals early so the PLL is locked by the time the segment
begins. :func:`run` does the whole decode for that slice and returns a
:class:`SegmentResult` with everything in seconds from the start of the
pass, so the parent only has to place spans.

Two physical formats:

- ``qic80``: GW PLL at the MFM bitcell, :func:`tapewyrm.codec.mfm.frame_bitcells`,
  :func:`tapewyrm.codec.mfm.recover_sectors_from_bytes`; sectors with the
  time of their ID and data fields.
- ``qic100``: GW PLL at the GCR bitcell (NRZ1: the PLL's "zeros then a 1"
  per transition is exactly the cell stream), :func:`qiclib.qic100.frame_records`;
  records with the time of every piece.

The PLL trace and the cells come back too, for the flux lane; the trace
is trimmed to the segment so neighbouring chunks never overlap.
"""

from __future__ import annotations

import logging
from array import array
from dataclasses import dataclass, field
from multiprocessing import shared_memory

import numpy as np
from qiclib import qic100
from tapewyrm.codec import gwpll, mfm

log = logging.getLogger(__name__)

WARMUP_S = 0.004  # PLL lock-in run before a segment; GW's PLL locks in well under a millisecond
CELLS_PER_MFM_BYTE = 16
ID_FIELD_BYTES = 3 + 1 + mfm.ID_FIELD_LEN + mfm.CRC_LEN
DATA_FIELD_BYTES = 3 + 1 + mfm.SECTOR_SIZE + mfm.CRC_LEN


@dataclass(frozen=True)
class SegmentJob:
    index: int  # segment index in the track
    shm_name: str  # shared memory holding the track's intervals, int32
    count: int  # intervals in the shared block
    first: int  # first interval of the slice (warm-up included)
    last: int  # one past the last interval of the slice
    ticks_before: int  # sum of intervals before ``first``: the slice's start time, in ticks
    sample_hz: int
    physical: str  # "qic80" | "qic100"
    bitcell_s: float  # PLL clock: MFM bitcell for qic80, estimated GCR bitcell for qic100
    start_s: float  # the segment's window, in seconds from the start of the pass
    end_s: float


@dataclass
class PlacedSector:
    fsd: int
    ftk: int
    fsc: int
    data: bytes
    id_crc_ok: bool
    data_crc_ok: bool
    deleted: bool
    id_start: float
    id_end: float
    data_start: float
    data_end: float


@dataclass
class PlacedRecord:
    """A QIC-100-MC record with its pieces in seconds (see :class:`qiclib.qic100.Record`)."""

    record_type: int
    frame: int | None
    track: int | None
    data_field: bytes | None
    id_raw: bytes
    crcc_ok: bool
    notes: list[str]
    preamble: tuple[float, float]
    sod: tuple[float, float]
    type_marker: tuple[float, float]
    data: tuple[float, float] | None
    id_field: tuple[float, float]
    crcc: tuple[float, float]
    postamble: tuple[float, float]
    id_detail: tuple[tuple[str, str], ...] = ()


@dataclass
class SegmentResult:
    index: int
    physical: str
    # Flux lane chunk: trace entries from the segment's start, bits from that point.
    trace_times: np.ndarray  # float64 seconds
    trace_bits: np.ndarray  # int64, cumulative cells after each traced transition (chunk-local)
    trace_clock: np.ndarray  # float64 seconds per cell
    bits: np.ndarray  # uint8
    sectors: list[PlacedSector] = field(default_factory=list)
    records: list[PlacedRecord] = field(default_factory=list)
    error: str = ""


def _pll(job: SegmentJob, intervals: list[int]) -> tuple[bytearray, gwpll.PllTrace]:
    trace = gwpll.PllTrace()
    cells = gwpll.flux_to_bitcells(intervals, job.sample_hz, job.bitcell_s, trace=trace)
    return cells, trace


class _Clock:
    """Cell index -> seconds, from a PLL trace over a slice starting at ``ticks_before``."""

    def __init__(self, trace: gwpll.PllTrace, ticks_before: int, sample_hz: int) -> None:
        self.times = (np.asarray(trace.ticks, dtype=np.float64) + ticks_before) / sample_hz
        self.bits = np.asarray(trace.bits, dtype=np.int64)
        self.clock = np.asarray(trace.clock, dtype=np.float64)

    def at(self, cell: int) -> float:
        if not len(self.bits):
            return 0.0
        i = min(int(np.searchsorted(self.bits, cell, side="right")), len(self.bits) - 1)
        return float(self.times[i] - self.clock[i] * (self.bits[i] - cell))

    def chunk(
        self, start_s: float, cells: bytearray
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        """The trace and bits from ``start_s`` on, bit counts made chunk-local."""
        i0 = int(np.searchsorted(self.times, start_s))
        k0 = int(self.bits[i0 - 1]) if i0 > 0 else 0
        bits = np.frombuffer(bytes(cells[k0:]), dtype=np.uint8)
        return self.times[i0:], self.bits[i0:] - k0, self.clock[i0:], bits


def _qic80(job: SegmentJob, intervals: list[int]) -> SegmentResult:
    cells, trace = _pll(job, intervals)
    clock = _Clock(trace, job.ticks_before, job.sample_hz)
    positions: list[int] = []
    decoded, _runs = mfm.frame_bitcells(cells, positions=positions)
    byte_cell = np.asarray(positions, dtype=np.int64)

    def at(byte: int, extra: int = 0) -> float:
        if not len(byte_cell):
            return job.start_s
        cell = int(byte_cell[min(byte, len(byte_cell) - 1)]) + CELLS_PER_MFM_BYTE * extra
        return clock.at(cell)

    placed: list[PlacedSector] = []
    for sec in mfm.recover_sectors_from_bytes(decoded):
        data_at = sec.data_at if sec.data_at is not None else 0
        d0, d1 = at(data_at), at(data_at, DATA_FIELD_BYTES)
        if not (job.start_s <= d0 < job.end_s):
            continue  # a warm-up sector belongs to the previous segment
        if sec.id_at is not None:
            i0, i1 = at(sec.id_at), at(sec.id_at, ID_FIELD_BYTES)
        else:
            i0 = i1 = d0
        placed.append(
            PlacedSector(
                sec.fsd,
                sec.ftk,
                sec.fsc,
                sec.data,
                sec.id_crc_ok,
                sec.data_crc_ok,
                sec.deleted,
                i0,
                i1,
                d0,
                d1,
            )
        )
    times, bits_cum, clk, bits = clock.chunk(job.start_s, cells)
    return SegmentResult(job.index, "qic80", times, bits_cum, clk, bits, sectors=placed)


def _id_detail(f: qic100.IdField) -> tuple[tuple[str, str], ...]:
    rows: list[tuple[str, str]] = [
        ("record type", qic100.record_type_name(f.record_type)),
        ("track", str(f.track)),
        ("frame", str(f.frame)),
    ]
    if f.thread is not None:
        rows.append(("thread", str(f.thread)))
    if f.error_control is not None:
        rows += [
            ("error control", str(f.error_control)),
            ("ecc counter", f"0x{f.ecc_counter:02x}"),
            ("ecc method", f"0x{f.ecc_method:02x}"),
            (
                "data field length",
                f"{f.data_field_length} (= {32 * ((f.data_field_length or 0) + 3)} B)",
            ),
            ("format spec", f"0x{f.format_spec:02x}"),
        ]
    rows += [
        ("frames/track", str(f.frames_per_track)),
        ("tracks/tape", str(f.tracks_per_tape)),
        ("post-data gap", str(f.post_data_gap)),
        ("post-id gap", str(f.post_id_gap)),
        ("drive spec", f"0x{f.drive_spec:02x}"),
        ("date", str(f.date_tuple) if f.date_tuple else "(none)"),
        ("time", str(f.time_tuple) if f.time_tuple else "(none)"),
        ("lot", str(f.lot)),
        ("manufacturer", repr(f.manufacturer)),
    ]
    return tuple(rows)


def _qic100(job: SegmentJob, intervals: list[int]) -> SegmentResult:
    cells, trace = _pll(job, intervals)
    clock = _Clock(trace, job.ticks_before, job.sample_hz)
    placed: list[PlacedRecord] = []
    for rec in qic100.frame_records(cells):
        start = clock.at(rec.preamble_at)
        if not (job.start_s <= start < job.end_s):
            continue
        span = lambda a, b: (clock.at(a), clock.at(b))  # noqa: E731
        placed.append(
            PlacedRecord(
                rec.record_type,
                rec.frame,
                rec.id_field.track if rec.id_field else None,
                rec.data_field,
                rec.id_raw,
                rec.crcc_ok,
                rec.notes,
                span(rec.preamble_at, rec.sod_at),
                span(rec.sod_at, rec.type_at),
                span(rec.type_at, rec.type_at + qic100.CELLS_PER_BYTE),
                span(rec.data_at, rec.id_at) if rec.data_at is not None else None,
                span(rec.id_at, rec.crcc_at),
                span(rec.crcc_at, rec.postamble_at),
                span(rec.postamble_at, rec.end),
                _id_detail(rec.id_field) if rec.id_field else (),
            )
        )
    times, bits_cum, clk, bits = clock.chunk(job.start_s, cells)
    return SegmentResult(job.index, "qic100", times, bits_cum, clk, bits, records=placed)


def run(job: SegmentJob) -> SegmentResult:
    """Decode one segment; the worker process entry point."""
    shm = shared_memory.SharedMemory(name=job.shm_name)
    try:
        all_intervals = np.ndarray((job.count,), dtype=np.int32, buffer=shm.buf)
        intervals = all_intervals[job.first : job.last].tolist()
    finally:
        shm.close()
    try:
        if job.physical == "qic100":
            return _qic100(job, intervals)
        return _qic80(job, intervals)
    except Exception as exc:  # noqa: BLE001 -- reported in the lane, never kills the pool
        log.exception("segment %d failed", job.index)
        empty = np.zeros(0)
        return SegmentResult(
            job.index,
            job.physical,
            empty,
            empty.astype(np.int64),
            empty,
            empty.astype(np.uint8),
            error=str(exc),
        )


def detect_physical(intervals: list[int], sample_hz: int, rate_kbps: int) -> tuple[str, float]:
    """Which physical format a stretch of flux is, and the PLL clock to use for it.

    Try MFM at the capture's rate first (QIC-40/80, the common case): if the
    stretch frames into any sector, it is QIC-80. Otherwise estimate the
    GCR bitcell from the interval histogram (:func:`qiclib.qic100.estimate_bitcell_ticks`)
    and look for a QIC-100-MC record whose CRCC checks; failing both, call
    it QIC-80 so the flux lane at least shows at the capture's rate.
    """
    mfm_cell = mfm.bitcell_seconds(rate_kbps)
    cells = gwpll.flux_to_bitcells(intervals, sample_hz, mfm_cell)
    decoded, _ = mfm.frame_bitcells(cells)
    if any(True for _ in mfm.recover_sectors_from_bytes(decoded)):
        return "qic80", mfm_cell
    try:
        ticks = qic100.estimate_bitcell_ticks(intervals)
    except ValueError:
        return "qic80", mfm_cell
    gcr_cell = ticks / sample_hz
    cells = gwpll.flux_to_bitcells(intervals, sample_hz, gcr_cell)
    # A record with a good CRCC: MFM gap and sync patterns read at a 2-cell
    # clock can look like a preamble and a marker, never like 31 bytes that
    # check.
    if any(r.crcc_ok for r in qic100.frame_records(cells)):
        log.info("flux frames as QIC-100-MC GCR, bitcell %.3f us", gcr_cell * 1e6)
        return "qic100", gcr_cell
    return "qic80", mfm_cell


def noop() -> None:
    """A job that does nothing: submitted once per worker to make the pool spawn them early."""


def intervals_array(intervals: array | list[int]) -> np.ndarray:
    return np.asarray(intervals, dtype=np.int32)

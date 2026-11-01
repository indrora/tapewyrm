"""Flux -> MFM bitcells: Greaseweazle's software PLL, vendored.

Credit: the PLL below is Keir Fraser's, from the Greaseweazle host tools
(``greaseweazle/track.py``: ``PLL``, ``PLLTrack``, ``flux_to_bitcells``,
https://github.com/keirf/greaseweazle, v1.23). Greaseweazle is released into the
public domain under the Unlicense (http://unlicense.org); we vendor rather than
depend on it so the decode stack stays pure stdlib (GW's version needs
``bitarray`` and its compiled ``optimised`` extension).

The clock-recovery arithmetic in :func:`flux_to_bitcells` is GW's, line for
line. Our changes are only at the edges, each marked ``[tapewyrm]``:

* inputs are plain tick intervals + sample frequency instead of a GW ``Flux``;
* output is a ``bytearray`` of 0/1 cells instead of a ``bitarray``;
* no revolution/index bookkeeping (a tape is one long stream, not revolutions),
  and no per-bit time array. GW builds one to walk index marks; here a
  caller that wants to know *when* a bitcell happened (cinnabar, drawing
  sectors on the time axis) passes a :class:`PllTrace`, which records the
  stream time, the bit count and the clock at every transition that
  clocked bits out -- enough to place any cell, at one append per
  transition instead of one per cell.

For QIC-80 at 500 kbit/s the MFM bitcell is 1 us (data bit = 2 cells), so the
flux intervals cluster at 2, 3 and 4 cells -- exactly floppy HD timing.
"""

from __future__ import annotations

import logging
from array import array
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from tapewyrm_archive.progress import NULL_PROGRESS, Progress

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class PLL:
    """GW's PLL tuning (``period``/``phase`` adjust, percent)."""

    period_adj_pct: int = 5
    phase_adj_pct: int = 60


# GW's two stock PLLs (greaseweazle/track.py ``plls``):
# Default: an aggressive PLL which quickly syncs to extreme bit timings.
AGGRESSIVE = PLL(period_adj_pct=5, phase_adj_pct=60)
# Fallback: a conservative PLL that is good at ignoring noise on otherwise
# well-behaved media (e.g. dirt/mould) -- possibly the better fit for old tape.
CONSERVATIVE = PLL(period_adj_pct=1, phase_adj_pct=10)

# GW PLLTrack: the clock may drift at most +/-10% from nominal.
CLOCK_MAX_ADJ = 0.10

# [tapewyrm] Flux transitions between progress updates (see flux_to_bitcells).
_PROGRESS_EVERY = 1 << 20


@dataclass
class PllTrace:
    """[tapewyrm] Where the PLL was at each transition that clocked bits out.

    Three parallel arrays, one entry per flux transition that produced at
    least one bitcell (sub-half-cell transitions merge into the next and
    record nothing):

    - ``ticks``: the transition's time, cumulative sample ticks from the
      start of the stream;
    - ``bits``: how many bitcells had been emitted *after* it (so the cells
      it clocked out are ``bits[i-1] .. bits[i]-1``);
    - ``clock``: the PLL's bitcell period, in seconds, after it.

    Cell ``k`` clocked out by transition ``i`` ended about ``(bits[i] - k - 1)``
    clocks before ``ticks[i] / sample_freq``; the last of them (the 1) is
    the transition itself. That is good to a fraction of a cell, which is
    all a picture needs; the PLL's phase correction is not replayed.
    """

    ticks: array = field(default_factory=lambda: array("q"))
    bits: array = field(default_factory=lambda: array("q"))
    clock: array = field(default_factory=lambda: array("d"))


def flux_to_bitcells(
    intervals: Iterable[int],
    sample_freq: float,
    clock: float,
    pll: PLL = AGGRESSIVE,
    *,
    progress: Progress = NULL_PROGRESS,
    trace: PllTrace | None = None,
) -> bytearray:
    """Recover MFM bitcells (one 0/1 byte per cell) from flux tick intervals.

    ``clock`` is the nominal bitcell time in seconds (1e-6 for 500 kbit/s MFM).
    ``progress`` gets one task counting flux transitions (``[tapewyrm]``).
    ``trace``, when given, is filled with the PLL's state at every
    transition that clocked bits out (:class:`PllTrace`); it costs one
    integer add per transition and three appends per bit-producing one.
    """
    bits = bytearray()  # [tapewyrm] was bitarray(endian='big')
    freq = sample_freq
    clock_centre = clock
    clock_min = clock * (1 - CLOCK_MAX_ADJ)
    clock_max = clock * (1 + CLOCK_MAX_ADJ)
    pll_period_adj = pll.period_adj_pct / 100
    pll_phase_adj = pll.phase_adj_pct / 100

    # [tapewyrm] Guard tallies, logged once after the loop. This loop runs per
    # flux transition (millions per track): no per-iteration logging, and the
    # counters only cost anything on the rare branches that bump them.
    n_short = n_unsync = 0
    log.debug(
        "PLL: nominal cell %.3g s, sample %.6g Hz, period adj %d%%, phase adj %d%%",
        clock_centre,
        sample_freq,
        pll.period_adj_pct,
        pll.phase_adj_pct,
    )

    # ---- GW flux_to_bitcells, verbatim from here except where marked ----
    ticks = 0.0
    clock = clock_centre
    total_ticks = 0  # [tapewyrm] stream time of the current transition, for the trace

    # [tapewyrm] GW's loop below runs once per flux transition (hundreds of
    # millions on a long track), so progress is reported from an outer loop
    # over slices of _PROGRESS_EVERY transitions rather than from inside it.
    # GW's loop body is unchanged apart from the extra indent; its locals
    # (ticks, clock) carry across slices exactly as across iterations.
    seq = intervals if isinstance(intervals, Sequence) else list(intervals)
    with progress.task("PLL: flux -> bitcells", total=len(seq), unit="transitions") as bar:
        for chunk_at in range(0, len(seq), _PROGRESS_EVERY):
            chunk = seq[chunk_at : chunk_at + _PROGRESS_EVERY]
            for x in chunk:
                total_ticks += x  # [tapewyrm]
                # Gather enough ticks to generate at least one bitcell.
                ticks += x / freq
                if ticks < clock / 2:
                    n_short += 1  # [tapewyrm]
                    continue

                # Clock out zero or more 0s, followed by a 1.
                zeros = 0
                while True:
                    ticks -= clock
                    if ticks < clock / 2:
                        break
                    zeros += 1
                    bits.append(0)
                bits.append(1)

                # PLL: Adjust clock window position according to phase mismatch.
                new_ticks = ticks * (1 - pll_phase_adj)
                # [tapewyrm] GW distributes the adjusted clock over the emitted bits here
                # to build its time array and walk index marks; we keep neither.

                # PLL: Adjust clock frequency according to phase mismatch.
                if zeros <= 3:
                    # In sync: adjust clock by a fraction of the phase mismatch.
                    clock += ticks * pll_period_adj
                else:
                    # Out of sync: adjust clock towards centre.
                    n_unsync += 1  # [tapewyrm]
                    clock += (clock_centre - clock) * pll_period_adj
                # Clamp the clock's adjustment range.
                clock = min(max(clock, clock_min), clock_max)

                ticks = new_ticks
                if trace is not None:  # [tapewyrm]
                    trace.ticks.append(total_ticks)
                    trace.bits.append(len(bits))
                    trace.clock.append(clock)
            bar.advance(len(chunk))  # [tapewyrm]

    log.debug(
        "PLL done: %d bitcells; %d sub-half-cell intervals merged into the next, "
        "%d out-of-sync gaps (>3 zeros) pulled clock to centre; final clock %.3g s",
        len(bits),
        n_short,
        n_unsync,
        clock,
    )
    return bits

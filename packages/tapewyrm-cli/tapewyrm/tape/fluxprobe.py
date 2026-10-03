"""`tw drive flux`: run the tape and record whatever the head sees.

A diagnostic, not a dump. It answers "is anything coming off this head at
all?" for a drive that won't reference a tape -- and Logical Forward, the only
motion `tw dump` uses, is refused on an unreferenced tape (QIC-117 Rev J error
19). So this sends a plain motion command instead (Physical Forward/Reverse by
default; Logical Forward when the tape is referenced), records the flux into a
TWRF file for later study, and stops the tape after a set wall-clock time.

How to read the result:

* No transitions at all while the tape moved: nothing reached RDATA. Either the
  read channel is dead, or this drive gates RDATA off during high-speed
  motion. Run the same probe on a known-good drive (and tape) to tell which.
* Transitions with sharp histogram peaks: a recorded signal. MFM gives peaks at
  2, 3 and 4 half-bitcells; at Physical (high) speed they sit closer together
  than at the drive's data rate, so sectors won't decode, but the shape is
  still unmistakable.
* Transitions spread evenly from short to long: noise, i.e. an amplifier with
  nothing worth amplifying (blank tape, dead head).
* Sectors decoded: real data at that rate (expected only with --motion logical).

Physical motion runs at high speed, which can produce flux faster than the
Greaseweazle's full-speed USB link drains it. The firmware then ends the
capture on overflow and stops the tape; that is itself proof of signal.
"""

from __future__ import annotations

import logging
import time
from collections import Counter
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

from tapewyrm_archive.progress import NULL_PROGRESS, Progress
from tapewyrm_archive.twrf import read_header, write_preamble
from tapewyrm_archive.types import Direction

from tapewyrm.codec import gwstream, mfm
from tapewyrm.link.protocol import EndReason
from tapewyrm.qic117 import commands
from tapewyrm.qic117.drive import Qic117Drive
from tapewyrm.tape.dump import drive_identity
from tapewyrm.types import StopCond

log = logging.getLogger(__name__)

MOTIONS = {
    "fwd": commands.PHYSICAL_FORWARD,
    "rev": commands.PHYSICAL_REVERSE,
    "logical": commands.LOGICAL_FORWARD,
}
# Backstop only: the host's wall-clock abort is what normally ends a probe.
# 256 MB is minutes of flux even at full USB speed.
BYTE_BUDGET = 256 * 1024 * 1024
HIST_BUCKET_US = 0.25
HIST_MAX_US = 8.0
# Software PLL time is the slow part; a few million transitions is plenty to
# tell signal from noise.
DECODE_LIMIT = 2_000_000
DECODE_RATES_KBPS = (500, 1000)


@dataclass
class FluxReport:
    path: Path
    bytes: int
    wall_s: float
    transitions: int
    flux_s: float  # stream time, silence included
    index_pulses: int
    end_reason: str  # firmware END reason, or "aborted" (host stopped it)
    histogram: list[tuple[float, int]]  # (bucket start in us, count); last = overflow
    sectors: dict[int, tuple[int, int]]  # rate kbps -> (sectors, CRC-clean)


def probe(
    drive: Qic117Drive,
    motion: str,
    seconds: float,
    path: Path,
    *,
    progress: Progress = NULL_PROGRESS,
) -> FluxReport:
    """Run ``motion`` for ``seconds`` while capturing flux to ``path``; analyse it."""
    cmd = MOTIONS[motion]
    hdr = replace(
        drive_identity(drive),
        track=-1,  # unknown: a probe doesn't seek, the head stays where it is
        direction=Direction.REVERSE if motion == "rev" else Direction.FORWARD,
        physical_reverse=motion == "rev",
        pass_id=0,
        utc=datetime.now(UTC).isoformat(timespec="seconds"),
    )
    log.info(f"{cmd.name} for {seconds:g} s -> {path}")
    t0 = time.monotonic()
    cap = drive.link.capture(
        cmd.code,
        StopCond(byte_budget=BYTE_BUDGET),
        rate=hdr.rate_kbps,
        direction=1 if motion == "rev" else 0,  # SESSION_START: 0 fwd, 1 rev
    )
    nbytes = 0
    log.debug("streaming probe capture to %s", path)
    with path.open("wb") as f, progress.task(cmd.name, total=seconds, unit="s") as bar:
        write_preamble(f, hdr)
        for chunk in cap.chunks_for(seconds):
            f.write(chunk)
            nbytes += len(chunk)
            # Wall time, not bytes, bounds a probe; clamp the read-timeout overshoot.
            bar.update(min(time.monotonic() - t0, seconds))
    wall = time.monotonic() - t0
    log.debug("probe captured %d bytes in %.1f s; waiting for drive to settle", nbytes, wall)
    drive.wait_ready(30)  # the abort issued Stop Tape; let the drive settle
    log.info(f"analysing {path}...")
    return analyse(path, wall_s=wall)


def analyse(path: Path, *, wall_s: float = 0.0) -> FluxReport:
    """Summarise a probe (or any TWRF) capture: amount, shape, decodability."""
    hdr, flux_at = read_header(path)
    blob = path.read_bytes()[flux_at:]
    log.debug("parsing %d bytes of flux from %s", len(blob), path)
    ps = gwstream.parse(blob)
    ticks_per_us = ps.sample_clock_hz / 1e6
    nbuckets = int(HIST_MAX_US / HIST_BUCKET_US)
    counts = Counter(min(int(v / ticks_per_us / HIST_BUCKET_US), nbuckets) for v in ps.intervals)
    histogram = [(k * HIST_BUCKET_US, counts.get(k, 0)) for k in range(nbuckets + 1)]
    sample = ps.intervals[:DECODE_LIMIT]
    if len(ps.intervals) > DECODE_LIMIT:
        log.debug(
            "%d transitions > DECODE_LIMIT %d; decoding only the first %d",
            len(ps.intervals),
            DECODE_LIMIT,
            DECODE_LIMIT,
        )
    sectors: dict[int, tuple[int, int]] = {}
    for rate in sorted({hdr.rate_kbps, *DECODE_RATES_KBPS}):
        log.debug("decoding %d transitions at %d kbps", len(sample), rate)
        found = mfm.recover_sectors_from_flux(sample, ps.sample_clock_hz, rate)
        sectors[rate] = (len(found), sum(1 for s in found if s.id_crc_ok and s.data_crc_ok))
    return FluxReport(
        path=path,
        bytes=len(blob),
        wall_s=wall_s,
        transitions=len(ps.intervals),
        flux_s=ps.span_s,
        index_pulses=len(ps.index_ticks),
        end_reason=EndReason(ps.end.reason).name if ps.end else "aborted",
        histogram=histogram,
        sectors=sectors,
    )


def format_report(r: FluxReport) -> list[str]:
    """Human-readable lines for the CLI."""
    rate = r.transitions / r.flux_s if r.flux_s else 0.0
    lines = [
        f"captured  : {r.bytes:,} bytes in {r.wall_s:.1f} s wall, END {r.end_reason}",
        f"flux      : {r.transitions:,} transitions over {r.flux_s:.2f} s "
        f"({rate / 1e3:,.1f} k/s), {r.index_pulses} INDEX pulses",
    ]
    if not r.transitions:
        lines.append("verdict   : NOTHING on RDATA while the tape moved")
        return lines
    peak = max(c for _, c in r.histogram) or 1
    lines.append("intervals :")
    for i, (start, count) in enumerate(r.histogram):
        if not count:
            continue
        label = (
            f">= {start:4.2f}"
            if i == len(r.histogram) - 1
            else f"{start:4.2f}-{start + HIST_BUCKET_US:4.2f}"
        )
        lines.append(f"  {label} us {count:>10,} {'#' * max(1, round(40 * count / peak))}")
    for rate, (n, good) in r.sectors.items():
        lines.append(f"sectors   : {n:,} found at {rate} kbps ({good:,} CRC-clean)")
    return lines

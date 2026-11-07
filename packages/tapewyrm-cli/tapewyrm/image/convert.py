"""`tw convert`: TWRF captures -> TWTI tape image.

Decodes each capture's flux to sectors (MFM, the physical layer this package
owns), then hands the passes to :func:`qiclib.build.build_image`, which merges,
places and Reed-Solomon corrects every segment and writes the TWTI file.
This is the only command that needs qiclib's tape logic (STYLE.md §2); the rest
of tapewyrm-cli borrows only its data types (and the test-only
``codec.pipeline``).

The decode is pure Python and CPU-bound, one core per capture, and takes a
third of the tape's running time on a 2024 laptop core (33 s for a 213 s
QIC-3010 track). With ``jobs > 1`` the captures decode in that many worker
processes (:func:`decode_captures`), so a whole dump takes about the time of
its longest ``captures / jobs`` tracks. What bounds the count is memory: a
worker holds the parsed flux as Python ints, about 25 bytes per byte of
capture (:data:`_RSS_PER_FLUX_BYTE`), which is what :func:`worker_count`
sizes an automatic pool by.
"""

from __future__ import annotations

import logging
import multiprocessing
import os
import threading
import time
from collections import deque
from collections.abc import Iterable, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ProcessPoolExecutor, wait
from logging.handlers import QueueHandler
from multiprocessing.queues import Queue
from pathlib import Path
from typing import Any

from qiclib.build import build_image
from qiclib.types import RawSector
from tapewyrm_archive import qic117
from tapewyrm_archive.progress import NULL_PROGRESS, Progress, ProgressRelay, QueueProgress
from tapewyrm_archive.provenance import provenance_path
from tapewyrm_archive.twrf import header_to_dict, read_header
from tapewyrm_archive.twti import TapeImage
from tapewyrm_archive.types import CaptureHeader, TapeFormat

from tapewyrm.codec import gwpll, gwstream, mfm

log = logging.getLogger(__name__)

# Peak resident memory of one decode per byte of flux in the capture, plus a
# floor for the interpreter and its imports. Measured on
# captures/blank-colorado-qic3010mc/track-00.twrf: 96.8 MB of flux, 67.4
# million transitions, 2.36 GB peak RSS (/usr/bin/time -l), so 24.4 bytes per
# flux byte. It is the parsed stream: a Python int (28 bytes) and a list slot
# (8) per transition, at about 1.4 flux bytes per transition at 500 kbps.
_RSS_PER_FLUX_BYTE = 25
_RSS_FLOOR = 100_000_000
# The share of physical memory the workers may fill between them. The rest is
# for the OS, the parent (every pass's sectors, ~10 MB a track, then the image
# build) and whatever else is running.
_RAM_SHARE = 0.75
# Loggers whose records a worker relays to the parent: the packages the decode
# runs through (tapewyrm.console._OUR_LOGGERS has the same plus qiclib, which
# runs only in the parent).
_RELAYED_LOGGERS = ("tapewyrm", "tapewyrm_archive")

# The queue a worker sends its log records and progress on; set by _worker_init
# in each worker, None in the parent.
_worker_queue: Queue[Any] | None = None


def capture_files(sources: Iterable[Path]) -> list[Path]:
    """Expand dump directories into their track captures.

    A directory contributes its ``track-*.twrf`` files, the names ``tw dump``
    writes (TWS-1 8.1 rule 7). A file named on the command line is taken
    whatever its name; :func:`decode_capture` then accepts it only if it
    starts with the TWRF magic, so the name never decides the format.
    """
    out: list[Path] = []
    for src in sources:
        if src.is_dir():
            twrf = sorted(src.glob("track-*.twrf"))
            log.debug("%s: %d track-*.twrf captures", src, len(twrf))
            out += twrf
        else:
            log.debug("%s: not a directory; using it as a capture file", src)
            out.append(src)
    return out


def decode_capture(
    path: Path, *, progress: Progress = NULL_PROGRESS
) -> tuple[list[RawSector], dict]:
    """Decode one capture's flux to sectors, reporting each stage as it goes.

    The stages are the ones :func:`tapewyrm.codec.mfm.recover_sectors_from_flux`
    chains, called one by one here so each can report what it produced
    (bitcells, sync runs, sector tallies): one INFO line per stage, every line
    led by the capture's name so interleaved passes stay attributable.
    ``progress`` gets one bar per long pass (parse, PLL, framing, scan).
    """
    started = time.perf_counter()
    name = path.name
    log.debug("reading capture %s", path)
    # read_header checks the magic and every header member before the (slow)
    # read of the flux, and raises a ValueError naming the file for anything
    # that is not a well-formed TWRF capture, whatever the file is called.
    hdr, flux_at = read_header(path)
    blob = path.read_bytes()
    flux, rate, meta = blob[flux_at:], hdr.rate_kbps, header_to_dict(hdr)
    log.info("%s: %s, %d kbps, %s of flux", name, describe_header(hdr), rate, _mb(len(flux)))
    log.debug("%s: parsing %d bytes of flux stream", name, len(flux))
    ps = gwstream.parse(flux, progress=progress)
    del blob, flux  # the parsed intervals are all later stages need
    if ps.verified:
        checked = "verified against the END marker"
    elif ps.end is None:
        log.debug("%s: no END marker in the stream; parse left unverified", name)
        checked = "unverified (no END marker)"
    else:
        log.debug("%s: parse disagrees with END marker %r", name, ps.end)
        checked = "MISMATCHES the END marker"
    log.info(
        "%s: %.1f s of tape, %s transitions, %s",
        name,
        ps.duration_s,
        f"{len(ps.intervals):,}",
        checked,
    )

    log.debug("%s: running the PLL at %d kbps (sample clock %d Hz)", name, rate, ps.sample_clock_hz)
    cells = gwpll.flux_to_bitcells(
        ps.intervals, ps.sample_clock_hz, mfm.bitcell_seconds(rate), progress=progress
    )
    n_cells = len(cells)
    decoded, sync_runs = mfm.frame_bitcells(cells, progress=progress)
    del cells
    log.info("%s: %s bitcells, %s sync marks", name, f"{n_cells:,}", f"{sync_runs:,}")

    scan = mfm.ScanStats()
    sectors = list(mfm.recover_sectors_from_bytes(decoded, progress=progress, stats=scan))
    log.info(
        "%s: %s sectors: %s CRC-clean, %s data-CRC bad, %s ID-only (%.1f s)",
        name,
        f"{scan.sectors:,}",
        f"{scan.data_crc_ok:,}",
        f"{scan.data_bad:,}",
        f"{scan.id_only:,}",
        time.perf_counter() - started,
    )
    return sectors, {
        # The capture's directory name and file name only, never the full
        # path: that would record the user's home directory (TWS-2 4.5).
        "file": provenance_path(path),
        "verified": ps.verified,
        "sectors": len(sectors),
        "twrf": meta,
    }


def describe_header(hdr: CaptureHeader) -> str:
    """One phrase for a TWRF header: track, direction, format and tape type.

    The format comes from the drive's Report Tape Status byte when the capture
    recorded one (it also names the tape type and width); otherwise from the
    header's own ``tape_format``, which the dump filled in from the same report
    or its default.
    """
    where = f"track {hdr.track} {hdr.direction.value}"
    if hdr.tape_status is None:
        log.debug("track %d: no tape status byte recorded; using header format", hdr.track)
        return f"{where}, {_format_name(hdr.tape_format)}"
    status = qic117.TapeStatus.decode(hdr.tape_status)
    text = f"{where}, {_format_name(status.format)}"
    tape_type = qic117.TAPE_TYPES.get(status.tape_type)
    if tape_type is None:
        log.debug("tape type code %d is not in TAPE_TYPES", status.tape_type)
        tape_type = f"tape type {status.tape_type}"
    text += f" on {tape_type}"
    if status.wide:
        text += ", wide"
    return text


def _format_name(fmt: TapeFormat) -> str:
    """``TapeFormat.QIC80`` -> ``"QIC-80"`` (the name the standards use)."""
    return fmt.name.replace("QIC", "QIC-") if fmt is not TapeFormat.UNKNOWN else "unknown format"


def _mb(size: int) -> str:
    """A byte count as decimal megabytes, the unit rich's file-size bars use."""
    return f"{size / 1e6:,.1f} MB"


def _size(size: int) -> str:
    """A byte count in decimal MB or GB, whichever reads better (STYLE.md §2: SI units)."""
    return f"{size / 1e9:,.1f} GB" if size >= 1e9 else _mb(size)


# ---------------------------------------------------------------------------
# Decoding in worker processes
# ---------------------------------------------------------------------------


def physical_memory() -> int | None:
    """Bytes of RAM in the machine, or None where the stdlib cannot say (Windows)."""
    try:
        return os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
    except (AttributeError, ValueError, OSError) as exc:
        log.debug("physical memory size unavailable (%r); sizing workers by cores alone", exc)
        return None


def worker_count(jobs: int, files: Sequence[Path]) -> int:
    """The number of decode processes for ``jobs`` over ``files``: ``jobs``, or a pick for 0.

    A pick is the cores, bounded by the memory the workers would need together
    (:data:`_RSS_PER_FLUX_BYTE` times the largest capture each, within
    :data:`_RAM_SHARE` of physical memory) -- fourteen workers at 2.4 GB on a
    36 GB machine would swap -- and by the number of captures. Where the
    memory size is unknown the cores alone decide. Never more workers than
    files, never fewer than one.
    """
    if jobs < 0:
        log.debug("jobs=%d is negative; refusing", jobs)
        raise ValueError(f"jobs must be 0 (automatic) or a positive count, not {jobs}")
    if jobs == 0:
        cores = os.cpu_count() or 1
        largest = max(path.stat().st_size for path in files)
        need = largest * _RSS_PER_FLUX_BYTE + _RSS_FLOOR
        ram = physical_memory()
        by_ram = cores if ram is None else max(1, int(ram * _RAM_SHARE) // need)
        jobs = min(cores, by_ram, len(files))
        log.info(
            "%d decode workers: %d cores, %s of RAM, about %s per worker for the largest capture (%s)",
            jobs,
            cores,
            "an unknown amount" if ram is None else _size(ram),
            _size(need),
            _size(largest),
        )
    return max(1, min(jobs, len(files)))


def _worker_init(queue: Queue[Any], levels: dict[str, int]) -> None:
    """Runs once in each worker: route its logs and progress to the parent's queue.

    A spawned worker starts with a bare logging setup (none of the parent's
    handlers survive spawn), so without this every INFO line of the decode
    would vanish. ``levels`` are the parent's effective levels per logger, so
    -v/-q hold in the worker too and DEBUG records are not shipped when nobody
    would show them. Progress goes on the same queue (:class:`QueueProgress`
    in :func:`_decode_job`).
    """
    global _worker_queue
    _worker_queue = queue
    handler = QueueHandler(queue)
    for name, level in levels.items():
        logger = logging.getLogger(name)
        logger.handlers[:] = [handler]
        logger.setLevel(level)
        logger.propagate = False


def _decode_job(index: int, path: Path) -> tuple[int, list[RawSector], dict]:
    """One capture in a worker: :func:`decode_capture`, its bars prefixed by the file name."""
    if _worker_queue is None:
        log.debug("_decode_job called with no worker queue (not in a pool); refusing")
        raise RuntimeError("_decode_job runs only in a pool started by decode_captures")
    progress = QueueProgress(_worker_queue, origin=index, prefix=f"{path.name}: ")
    sectors, meta = decode_capture(path, progress=progress)
    return index, sectors, meta


def _drain(queue: Queue[Any], relay: ProgressRelay) -> None:
    """The parent's relay thread: records to this process's loggers, progress to ``relay``."""
    while True:
        item = queue.get()
        if item is None:
            return
        if isinstance(item, logging.LogRecord):
            logging.getLogger(item.name).handle(item)
        else:
            relay.handle(item)


def decode_captures(
    files: Sequence[Path], workers: int, *, progress: Progress = NULL_PROGRESS
) -> list[tuple[list[RawSector], dict]]:
    """Decode ``files`` in ``workers`` processes: one (sectors, meta) per file, in order.

    Each worker runs :func:`decode_capture` on one file at a time and pickles
    its sectors back (about 10 MB a track). The workers are spawned, not
    forked (the only start method on macOS, and the only safe one with this
    process's threads around), so they start with a bare logging setup;
    :func:`_worker_init` routes each one's log records and progress onto one
    queue, which a thread here drains: records go to this process's loggers
    (the CLI's rich handler, a test's caplog), progress to a
    :class:`ProgressRelay` over ``progress``. The bars are one "decoding
    captures" bar counting finished files, plus every stage bar of each
    capture in flight, prefixed with its file name.

    The first failure stops the run: files not started yet never start, and
    the exception is raised again once the files in flight have finished
    (the pool waits for them; a Ctrl-C reaches the workers as well, so theirs
    end at once). A capture that is not TWRF raises the ValueError it would
    in the serial path.
    """
    ctx = multiprocessing.get_context("spawn")
    queue: Queue[Any] = ctx.Queue()
    levels = {name: logging.getLogger(name).getEffectiveLevel() for name in _RELAYED_LOGGERS}
    relay = ProgressRelay(progress)
    drain = threading.Thread(target=_drain, args=(queue, relay), name="convert-relay", daemon=True)
    drain.start()
    results: dict[int, tuple[list[RawSector], dict]] = {}
    log.debug("starting %d spawned decode workers for %d captures", workers, len(files))
    try:
        with (
            ProcessPoolExecutor(
                workers, mp_context=ctx, initializer=_worker_init, initargs=(queue, levels)
            ) as pool,
            progress.task("decoding captures", total=len(files), unit="captures") as overall,
        ):
            # Never more than ``workers`` captures submitted at once. Given
            # everything up front, the pool queues workers+1 extra items to
            # its processes ahead of time and marks their futures running,
            # which cancel_futures cannot take back: a Ctrl-C or a bad
            # capture then left the workers decoding five more tracks after
            # the command had said "Aborted".
            todo = deque(enumerate(files))
            running: set[Future[tuple[int, list[RawSector], dict]]] = {
                pool.submit(_decode_job, *todo.popleft()) for _ in range(min(workers, len(todo)))
            }
            try:
                while running:
                    done, running = wait(running, return_when=FIRST_COMPLETED)
                    for fut in done:
                        index, sectors, meta = fut.result()
                        results[index] = (sectors, meta)
                        overall.advance()
                    while todo and len(running) < workers:
                        running.add(pool.submit(_decode_job, *todo.popleft()))
            except BaseException as exc:
                log.debug(
                    "decode stopped by %r: %d captures not started, waiting for the %d running",
                    exc,
                    len(todo),
                    len(running),
                )
                # Waits for the running captures; an interrupted one ends at
                # once, the Ctrl-C having reached its worker too. Not
                # shutdown(wait=False): that drops the executor's thread
                # handle, so the pool's exit below would then wait for
                # nothing and the workers would run on past the command's end.
                pool.shutdown(cancel_futures=True)
                raise
    finally:
        # The pool has joined its workers by now (even on the way out of an
        # error), so everything they sent is ahead of this sentinel.
        queue.put(None)
        drain.join()
        relay.close()
    return [results[k] for k in range(len(files))]


# ---------------------------------------------------------------------------
# The conversion
# ---------------------------------------------------------------------------


def convert(
    sources: Iterable[Path], out: Path, *, progress: Progress = NULL_PROGRESS, jobs: int = 1
) -> TapeImage:
    """Build a TWTI image from one or more dumps (passes are merged).

    ``jobs`` captures decode at once, each in its own process, when it is more
    than one (0 lets :func:`worker_count` pick); the passes still merge in
    source order, so the image is the same whatever the count.

    Progress, serial: one "capture k/N <name>" bar per capture (total N,
    positioned at k-1 while that capture decodes, so it reads as a single
    outer bar whose label follows the current file), with the stage bars of
    :func:`decode_capture` under it. Parallel: :func:`decode_captures`'s bars.
    Then :func:`qiclib.build.build_image`'s.
    """
    started = time.perf_counter()
    sources = list(sources)
    files = capture_files(sources)
    if not files:
        log.debug("sources expanded to no captures (directories without track-*.twrf); refusing")
        named = ", ".join(str(src) for src in sources)
        raise ValueError(f"no track-*.twrf captures found in {named}")
    count = len(files)
    log.info("converting %d capture(s) into %s", count, out.name)
    workers = worker_count(jobs, files)
    passes: list[list[RawSector]] = []
    source_meta: list[dict] = []
    if workers > 1:
        log.info("decoding %d captures, %d at a time", count, workers)
        for sectors, meta in decode_captures(files, workers, progress=progress):
            passes.append(sectors)
            source_meta.append(meta)
    else:
        log.debug("decoding %d captures one after another in this process", count)
        for k, path in enumerate(files, 1):
            with progress.task(
                f"capture {k}/{count} {path.name}", total=count, unit="captures"
            ) as bar:
                bar.update(k - 1)
                sectors, meta = decode_capture(path, progress=progress)
                bar.update(k)
            passes.append(sectors)
            source_meta.append(meta)
    from tapewyrm.buildinfo import host_build

    img = build_image(
        passes, out, sources=source_meta, tw_commit=host_build().commit, progress=progress
    )
    log.info("converted %d capture(s) in %.1f s total", count, time.perf_counter() - started)
    return img

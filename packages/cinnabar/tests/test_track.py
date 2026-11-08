"""cinnabar.decode.track: the process-pool orchestrator."""

from __future__ import annotations

import gc
import time
import weakref
from multiprocessing import shared_memory
from pathlib import Path

from tapewyrm_archive.twrf import parse_body, read_header

from cinnabar.decode import jobs
from cinnabar.decode.track import TrackDecoder
from cinnabar.load.flux import CUE_WINDOW_S


def test_wait_returns_only_after_every_listener_has_run(qic80_capture: Path):
    """A result is stored before its listeners run; wait() must not return between the two.

    The window identifies the tape and rebuilds segments from the thread
    that calls wait(), so a listener still annotating the last segment on
    the pool's completion thread would race it (the header segment came up
    bare). A listener slow enough to be caught mid-run shows the guarantee.
    """
    hdr, flux_at = read_header(qic80_capture)
    ps = parse_body(qic80_capture.read_bytes()[flux_at:])
    intervals = jobs.intervals_array(ps.intervals)
    index_s = [
        t / ps.sample_clock_hz for t in ps.index_ticks if t / ps.sample_clock_hz >= CUE_WINDOW_S
    ]
    edges = [0.0, *index_s, max(ps.span_s, index_s[-1] + 1e-3)]
    windows = [(edges[k], edges[k + 1]) for k in range(len(edges) - 1)]
    physical, bitcell_s = jobs.detect_physical(
        intervals[:20000].tolist(), ps.sample_clock_hz, hdr.rate_kbps
    )
    decoder = TrackDecoder(intervals, ps.sample_clock_hz, physical, bitcell_s, windows)
    seen: list[int] = []

    def slow(res: jobs.SegmentResult) -> None:
        time.sleep(0.2)
        seen.append(res.index)

    decoder.on_result(slow)
    decoder.submit_all(0)
    decoder.wait()
    assert sorted(seen) == list(range(len(windows)))
    decoder.close()


def _decoder(capture: Path) -> TrackDecoder:
    """A decoder over ``capture``'s track, cut into segments at its index pulses as FluxTrack does."""
    hdr, flux_at = read_header(capture)
    ps = parse_body(capture.read_bytes()[flux_at:])
    intervals = jobs.intervals_array(ps.intervals)
    index_s = [
        t / ps.sample_clock_hz for t in ps.index_ticks if t / ps.sample_clock_hz >= CUE_WINDOW_S
    ]
    edges = [0.0, *index_s, max(ps.span_s, index_s[-1] + 1e-3)]
    windows = [(edges[k], edges[k + 1]) for k in range(len(edges) - 1)]
    physical, bitcell_s = jobs.detect_physical(
        intervals[:20000].tolist(), ps.sample_clock_hz, hdr.rate_kbps
    )
    return TrackDecoder(intervals, ps.sample_clock_hz, physical, bitcell_s, windows)


def _gone(name: str) -> bool:
    """Whether the shared-memory block ``name`` has been unlinked."""
    try:
        shared_memory.SharedMemory(name=name).close()
    except FileNotFoundError:
        return True
    return False


def test_a_paused_decoder_queues_nothing_until_resumed(qic80_capture: Path):
    decoder = _decoder(qic80_capture)
    try:
        decoder.pause()
        decoder.submit_all(0)
        decoder.wait()  # nothing queued, so nothing to wait for
        assert decoder.paused and decoder.pending() == len(decoder.windows)
        decoder.resume(0)
        decoder.wait()
        assert not decoder.paused and decoder.pending() == 0
    finally:
        decoder.close()


def test_close_frees_the_shared_memory_and_is_final(qic80_capture: Path):
    decoder = _decoder(qic80_capture)
    name = decoder._shm.name
    decoder.close()
    decoder.close()  # idempotent
    assert _gone(name)
    decoder.submit_all(0)  # queues nothing once closed
    decoder.wait()  # and returns at once
    assert decoder.closed and decoder.pending() == len(decoder.windows)


def test_a_dropped_decoder_is_collected_and_frees_its_shared_memory(qic80_capture: Path):
    """No exit handler keeps a decoder alive: dropping it frees its block (weakref.finalize).

    The atexit.register(self.close) this replaced held every decoder ever
    opened, with all its decoded segments, until the interpreter exited.
    """
    decoder = _decoder(qic80_capture)
    name = decoder._shm.name
    ref = weakref.ref(decoder)
    del decoder
    gc.collect()
    assert ref() is None
    assert _gone(name)

"""cinnabar.decode.track: the process-pool orchestrator."""

from __future__ import annotations

import time
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

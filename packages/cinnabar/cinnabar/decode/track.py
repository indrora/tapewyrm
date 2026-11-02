"""The orchestrator: a track's segments through a process pool, nearest first.

:class:`TrackDecoder` owns the track's intervals in shared memory and a
:class:`concurrent.futures.ProcessPoolExecutor` (spawned, so no Qt or
fork trouble). ``submit_all`` queues every segment ordered by distance
from a focus segment; ``focus`` re-orders what has not started yet
(pending futures are cancelled and resubmitted, which is cheap); each
result is handed to the listeners from the pool's completion thread, so a
Qt listener must hop to the main thread itself (``cinnabar.ui.app`` does,
through a signal). ``wait`` blocks until everything has landed, for
``--dump`` and the tests.

One pool is shared by every decoder in the process (one per track being
viewed), sized to the machine; the shared-memory blocks are unlinked when
a decoder closes or the interpreter exits.
"""

from __future__ import annotations

import atexit
import logging
import multiprocessing
import os
import threading
from collections.abc import Callable
from concurrent.futures import Future, ProcessPoolExecutor
from multiprocessing import shared_memory

import numpy as np

from cinnabar.decode import jobs

log = logging.getLogger(__name__)

_POOL: ProcessPoolExecutor | None = None
_POOL_LOCK = threading.Lock()
_WORKERS = max(1, (os.cpu_count() or 2) - 1)


def pool() -> ProcessPoolExecutor:
    global _POOL
    with _POOL_LOCK:
        if _POOL is None:
            workers = _WORKERS
            _POOL = ProcessPoolExecutor(workers, mp_context=multiprocessing.get_context("spawn"))
            atexit.register(_POOL.shutdown, wait=False, cancel_futures=True)
            log.debug("decode pool: %d workers", workers)
        return _POOL


def warm_up() -> None:
    """Spawn every worker process now, so the first track's decode does not pay for it.

    ``ProcessPoolExecutor`` starts a process per submitted job until it has
    its full count, and with the spawn start method each one takes a
    quarter of a second to come up and import numpy and the codec: the
    first track's ``submit_all`` used to block for three seconds right
    after its progress bar said done. Submitting one no-op per worker
    from a thread at start-up overlaps that with parsing the file.
    """
    executor = pool()
    futures = [executor.submit(jobs.noop) for _ in range(_WORKERS)]
    for f in futures:
        f.result()


class TrackDecoder:
    def __init__(
        self,
        intervals: np.ndarray,
        sample_hz: int,
        physical: str,
        bitcell_s: float,
        windows: list[tuple[float, float]],
    ) -> None:
        """``windows[i]`` is segment ``i``'s (start, end) in seconds."""
        self.physical = physical
        self.bitcell_s = bitcell_s
        self.sample_hz = sample_hz
        self.windows = windows
        self._count = len(intervals)
        self._shm = shared_memory.SharedMemory(create=True, size=max(intervals.nbytes, 4))
        np.ndarray(intervals.shape, dtype=np.int32, buffer=self._shm.buf)[:] = intervals
        atexit.register(self.close)
        # Interval index at each time: cumulative ticks, searched per window.
        self._cum = np.cumsum(intervals.astype(np.int64))
        self._futures: dict[int, Future] = {}
        self._results: dict[int, jobs.SegmentResult] = {}
        self._listeners: list[Callable[[jobs.SegmentResult], None]] = []
        self._lock = threading.Lock()
        self._closed = False

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for f in self._futures.values():
            f.cancel()
        try:
            self._shm.close()
            self._shm.unlink()
        except FileNotFoundError:
            pass

    # -- jobs --------------------------------------------------------------

    def _job(self, index: int) -> jobs.SegmentJob:
        start_s, end_s = self.windows[index]
        t0 = max(start_s - jobs.WARMUP_S, 0.0) * self.sample_hz
        first = int(np.searchsorted(self._cum, t0))
        last = int(np.searchsorted(self._cum, end_s * self.sample_hz, side="right"))
        ticks_before = int(self._cum[first - 1]) if first > 0 else 0
        return jobs.SegmentJob(
            index,
            self._shm.name,
            self._count,
            first,
            min(last + 1, self._count),
            ticks_before,
            self.sample_hz,
            self.physical,
            self.bitcell_s,
            start_s,
            end_s,
        )

    def on_result(self, listener: Callable[[jobs.SegmentResult], None]) -> None:
        self._listeners.append(listener)

    def result(self, index: int) -> jobs.SegmentResult | None:
        return self._results.get(index)

    def _done(self, fut: Future) -> None:
        if fut.cancelled():
            return
        try:
            res: jobs.SegmentResult = fut.result()
        except Exception:  # noqa: BLE001 -- a broken pool; the segment stays pending
            log.exception("decode job failed")
            return
        with self._lock:
            self._results[res.index] = res
        for listener in self._listeners:
            listener(res)

    def submit_all(self, focus: int = 0) -> None:
        order = sorted(range(len(self.windows)), key=lambda i: abs(i - focus))
        with self._lock:
            for i in order:
                if i in self._results or i in self._futures:
                    continue
                fut = pool().submit(jobs.run, self._job(i))
                fut.add_done_callback(self._done)
                self._futures[i] = fut

    def focus(self, index: int) -> None:
        """Decode the segments around ``index`` first: cancel what has not started, resubmit."""
        with self._lock:
            pending = [i for i, f in self._futures.items() if not f.running() and f.cancel()]
            for i in pending:
                del self._futures[i]
        if pending:
            self.submit_all(index)

    def pending(self) -> int:
        with self._lock:
            return sum(1 for i in range(len(self.windows)) if i not in self._results)

    def wait(self) -> None:
        while True:
            with self._lock:
                futs = [f for i, f in self._futures.items() if i not in self._results]
            if not futs:
                return
            for f in futs:
                try:
                    f.result()
                except Exception:  # noqa: BLE001 -- logged in _done
                    pass

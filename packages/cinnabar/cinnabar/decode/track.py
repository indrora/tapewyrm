"""The orchestrator: a track's segments through a process pool, nearest first.

:class:`TrackDecoder` owns the track's intervals in shared memory and a
:class:`concurrent.futures.ProcessPoolExecutor` (spawned, so no Qt or
fork trouble). ``submit_all`` queues every segment ordered by distance
from a focus segment; ``focus`` re-orders what has not started yet
(pending futures are cancelled and resubmitted, which is cheap); each
result is handed to the listeners from the pool's completion thread, so a
Qt listener must hop to the main thread itself (``cinnabar.ui.app`` does,
through a signal). ``wait`` blocks until everything has landed and every
listener has run, for ``--dump`` and the tests.

One pool is shared by every decoder in the process (one per track being
viewed), sized to the machine, and it runs jobs first come, first served.
So a decoder can be paused: ``pause`` cancels the segments it has queued
and not started (the ones decoding finish and land), so another track's
segments are not stuck behind them, and ``resume`` queues the rest again,
nearest a focus first. The window pauses a track it leaves, and its Stop
button pauses the track it shows.

``close`` cancels for good and frees the shared memory. A
:class:`weakref.finalize` frees it too when the decoder is garbage
collected or the interpreter exits, whichever comes first. That is the
standard library's tool for this: it runs once, however it is reached,
and unlike an ``atexit.register(self.close)`` it does not keep the
decoder alive. The atexit entry used to hold every decoder ever opened,
with all its decoded segments, until exit. It also ran as soon as it was
registered, half way through ``__init__``, when the window closed during
a load. ``close`` then failed on a missing attribute and the block leaked
(``resource_tracker: There appear to be 1 leaked shared_memory objects``).

Once closed, a decoder calls no listener and queues nothing. A result that
lands after ``close`` (its segment was already decoding) is dropped, so
nothing is emitted to a window that is going away.
"""

from __future__ import annotations

import atexit
import functools
import logging
import multiprocessing
import os
import threading
import weakref
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


def _release(shm: shared_memory.SharedMemory) -> None:
    """Free a decoder's shared memory: its :class:`weakref.finalize` callback, so it must not refer to the decoder."""
    shm.close()
    try:
        shm.unlink()
    except FileNotFoundError:  # already gone
        pass


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
        # Every attribute close() and the callbacks touch is set before the
        # shared memory exists, so there is never a half-built decoder
        # holding a block that nothing will free (see the module docstring).
        self._futures: dict[int, Future] = {}
        self._results: dict[int, jobs.SegmentResult] = {}
        self._listeners: list[Callable[[jobs.SegmentResult], None]] = []
        self._lock = threading.Lock()
        # Indexes whose job is over: landed and every listener has run with
        # it, or failed. A result is stored in ``_results`` before its
        # listeners run, so "every result is in" is not "every segment has
        # been handled". ``wait`` used to return on the first, and its caller
        # then raced the pool's completion thread over the last few segments.
        # (The window identified the tape on the main thread while the header
        # segment was still being annotated on the other, and that segment
        # came up with no logical lanes.) The condition shares ``_lock``.
        self._settled: set[int] = set()
        self._settled_cv = threading.Condition(self._lock)
        self._closed = False
        self._paused = False
        # Interval index at each time: cumulative ticks, searched per window.
        self._cum = np.cumsum(intervals.astype(np.int64))
        self._shm = shared_memory.SharedMemory(create=True, size=max(intervals.nbytes, 4))
        self._release = weakref.finalize(self, _release, self._shm)
        np.ndarray(intervals.shape, dtype=np.int32, buffer=self._shm.buf)[:] = intervals

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        """Cancel every queued segment, free the shared memory, call no listener again. Idempotent."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            futures = list(self._futures.values())
            self._settled_cv.notify_all()  # a wait() in progress returns
        for f in futures:
            f.cancel()
        # A segment already decoding attached the block by name in its own
        # process; unlinking here leaves that mapping valid (POSIX shm_unlink).
        self._release()

    @property
    def closed(self) -> bool:
        return self._closed

    def pause(self) -> None:
        """Cancel the segments not yet started; the ones decoding finish and land. ``resume`` restarts."""
        with self._lock:
            self._paused = True
            for i in [i for i, f in self._futures.items() if f.cancel()]:
                del self._futures[i]
            self._settled_cv.notify_all()  # a wait() no longer waits for those

    def resume(self, focus: int = 0) -> None:
        """Queue every segment that has not landed again, nearest ``focus`` first."""
        with self._lock:
            self._paused = False
        self.submit_all(focus)

    @property
    def paused(self) -> bool:
        return self._paused

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

    def _done(self, index: int, fut: Future) -> None:
        if fut.cancelled():
            return  # focus or pause took it out of _futures; close does not care
        res: jobs.SegmentResult | None = None
        try:
            res = fut.result()
        except Exception:  # noqa: BLE001 -- a broken pool; the segment stays pending
            if not self._closed:  # after close, a vanished block is expected
                log.exception("decode job for segment %d failed", index)
        try:
            if res is None:
                return
            with self._lock:
                if self._closed:
                    return
                self._results[index] = res
            for listener in self._listeners:
                listener(res)
        finally:
            with self._lock:
                self._settled.add(index)
                self._settled_cv.notify_all()

    def submit_all(self, focus: int = 0) -> None:
        order = sorted(range(len(self.windows)), key=lambda i: abs(i - focus))
        with self._lock:
            if self._closed or self._paused:
                return
            for i in order:
                if i in self._results or i in self._futures:
                    continue
                fut = pool().submit(jobs.run, self._job(i))
                fut.add_done_callback(functools.partial(self._done, i))
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
        """Block until every queued segment has been handled: landed with its listeners run, or failed.

        Returns at once when closed. Segments a pause cancelled are not
        waited for, since they are no longer queued. (This used to wait on
        each future in turn and loop until every index had a result, which
        spun for ever on a job that had failed.)
        """
        with self._settled_cv:
            self._settled_cv.wait_for(
                lambda: self._closed or all(i in self._settled for i in self._futures)
            )

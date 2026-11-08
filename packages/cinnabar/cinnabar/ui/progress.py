"""A :class:`tapewyrm_archive.progress.Progress` that reports through Qt signals.

The libraries report long work through the ``Progress`` protocol (one
``task`` per stage: "parsing flux", "PLL", "decompressing"); the CLIs draw
those as rich bars. Here the same calls become signals, emitted from
whatever worker thread is doing the work and delivered to the main thread
by Qt's queued connections, so the status bar can say what is going on
and how far it has got without the window ever blocking.

One task at a time is shown (the stages are sequential); nested tasks
show the innermost.

Cancelling
----------

The same calls are where a load can be stopped. :class:`Cancellable`
wraps any ``Progress`` with a :class:`threading.Event` (Python's
cancellation token: no library needed, and Qt's own
``QThread.requestInterruption`` exists only for ``QThread`` subclasses,
not for the pool's ``QRunnable`` jobs). Once ``cancel()`` sets the event,
the next ``task``, ``advance`` or ``update`` raises :class:`Cancelled`.
Every slow stage of a load already reports progress (the flux parser
once per slice of the stream), so that puts cancellation points all
through ``tapewyrm_archive`` and ``qiclib`` without passing a token
through either. A step that reports nothing (reading the file,
converting the interval list) runs to its end first; those take a
second or two at most.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager

from PySide6.QtCore import QObject, Signal
from tapewyrm_archive.progress import Progress, ProgressTask


class Cancelled(BaseException):  # noqa: N818 -- named for what happened, like KeyboardInterrupt
    """A load was cancelled: raised from a :class:`Cancellable`'s next progress report.

    A ``BaseException``, like ``asyncio.CancelledError`` (which became one
    in Python 3.8 for this reason) and ``KeyboardInterrupt``: the loaders
    catch ``Exception`` in places to carry on past a bad record, and a
    cancellation must not be carried on past.
    """


class Cancellable:
    """A ``Progress`` that raises :class:`Cancelled` at its next report once ``cancel()`` is called.

    Thread-safe: ``cancel`` is called on the main thread, the reports
    come from the worker doing the load.
    """

    def __init__(self, inner: Progress) -> None:
        self.inner = inner
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def check(self) -> None:
        if self._event.is_set():
            raise Cancelled

    @contextmanager
    def task(
        self, description: str, total: float | None = None, unit: str = ""
    ) -> Iterator[ProgressTask]:
        self.check()
        with self.inner.task(description, total, unit) as job:
            yield _CheckedTask(job, self)


class _CheckedTask:
    def __init__(self, inner: ProgressTask, owner: Cancellable) -> None:
        self._inner = inner
        self._owner = owner

    def advance(self, amount: float = 1) -> None:
        self._owner.check()
        self._inner.advance(amount)

    def update(self, completed: float) -> None:
        self._owner.check()
        self._inner.update(completed)


class QtProgress(QObject):
    started = Signal(str, float, str)  # description, total (0 = unknown), unit
    advanced = Signal(float)  # completed so far
    finished = Signal()

    def __init__(self) -> None:
        super().__init__()
        self._depth = 0

    @contextmanager
    def task(
        self, description: str, total: float | None = None, unit: str = ""
    ) -> Iterator[ProgressTask]:
        self._depth += 1
        self.started.emit(description, float(total or 0), unit)
        job = _Task(self)
        try:
            yield job
        finally:
            self._depth -= 1
            if self._depth == 0:
                self.finished.emit()


class _Task:
    def __init__(self, owner: QtProgress) -> None:
        self._owner = owner
        self._done = 0.0

    def advance(self, amount: float = 1) -> None:
        self._done += amount
        self._owner.advanced.emit(self._done)

    def update(self, completed: float) -> None:
        self._done = completed
        self._owner.advanced.emit(self._done)
